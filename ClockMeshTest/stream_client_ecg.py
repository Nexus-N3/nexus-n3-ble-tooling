"""Bounded mixed DOT + chest ECG capture on one gateway reference timeline."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from uuid import uuid4

from MovellaDot.client import MovellaDotClient
from Movesense.client import MovesenseClient
from NexusBLESdk import CsvRowWriter, GatewayClient, open_gateway_serial

from .evidence import console_summary as dot_console_summary, write_prediction_error_report
from .provenance import timestamp_provenance
from .motion import MOTION_UNITS, decode_motion
from .stream_client import build_parser as dot_parser, software_info, validate_args


def console_summary(summary):
    combined = {**summary, "prediction_error_statistics": {
        "epochs": summary.get("prediction_error_statistics", {}).get("epochs", [])
        + summary.get("ecg_prediction_error_statistics", {}).get("epochs", []),
    }}
    for line in dot_console_summary(combined):
        for exp in summary.get("experiments", []):
            if exp.get("sensor_type") == "movesense_ecg":
                line = line.replace(f"DOT {exp['address']} ", f"ECG {exp['address']} ")
        yield line


def build_parser() -> argparse.ArgumentParser:
    parser = dot_parser()
    parser.description = "ClockMesh: DOTs at 60 Hz plus one Movesense ECG at 200 Hz."
    parser.set_defaults(sensor_count=3)
    next(action for action in parser._actions if action.dest == "sensor_count").help = (
        "Number of DOTs, plus one Movesense ECG (default: 3 DOTs).")
    parser.add_argument("--movesense-address", help="Explicit ECG BLE address; otherwise discover one Movesense.")
    parser.add_argument("--ecg-sampling-rate-hz", type=int, choices=[200], default=200)
    parser.set_defaults(output_dir=Path("output-files/clock-mesh-ecg"))
    return parser


def run(args) -> int:
    from clock_mesh import ClockAlignmentConfig
    from .alignment import DotExperiment, ExperimentFailure, RECORD_FIELDS, try_start_official

    from .ecg_alignment import EcgExperiment, PACKET_FIELDS, SAMPLE_FIELDS, TIMESTAMP_POLICY

    validate_args(args)

    config = ClockAlignmentConfig(
        target_calibration_span_ns=1_000_000_000,
        reuse_calibration_history=True,
        calibration_deadline_ns=round(args.calibration_deadline_seconds * 1e9),
        min_tracking_acceptance_span_ns=5_000_000_000,  # override the default 30s
    )
    output = args.output_dir.resolve() / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ_") + uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": 4, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "running", "output_directory": str(output),
        "configuration": {**vars(args), "output_dir": str(args.output_dir),
                          "sampling_rate_hz": 60, "ecg_sampling_rate_hz": 200,
                          "clock_alignment": asdict(config)},
        "capture_duration_origin": "host monotonic time immediately before start_streams; includes acquisition and calibration",
        "calibration_duration_origin": "first valid startup observation; initial alignment host elapsed and gateway reference span recorded separately",
        "timestamp_units": {
            "raw_dot_timestamp_us": (
                "uint32 microseconds, payload bytes 0..3 little-endian"
            ),
            "gateway_timestamp_us": (
                "uint64 microseconds, binary header bytes 4..11 little-endian"
            ),
            "reference_time_ns": "gateway_timestamp_us * 1000",
            "mapped_timestamp_ns": (
                "Sensor source time on gateway reference timeline; not host/master time"
            ),
            "connection_interval_us": (
                "Latest connection interval reported to the host when processing "
                "the frame; interval_units * 1250; empty if unknown; "
                "not an exact association with the radio reception event"
            ),
        },
        "ecg_timestamp_policy": TIMESTAMP_POLICY,
        "sensor_count_definition": "sensor_count DOTs plus one Movesense ECG",
        "motion_units": MOTION_UNITS,
        "motion_payload": "MovellaDot.profile.parse_packet; configured start 01011A; <I10f prefix (44 bytes); quaternion w,x,y,z, acceleration x,y,z, angular velocity x,y,z; unchanged sensor values",
        "continuity_policy": "ECG also rejects overlapping/off-grid samples, counts missing 5ms samples using the previous actual packet end, and records malformed packets without observing them. No inferred rollover; backward/duplicate DOT, backward gateway, or delta disagreement >1s creates new epoch/model and ends run",
        "prediction_error_definition": "observed gateway reference minus pre-update accepted mapping; BLE delivery delay included; official measurement only in error reports",
        "candidate_diagnostics": "candidate prediction/error use the pre-observe fit; candidate scale/count/span use the post-observe fit; full affine acceptance once at shared official start, then continuous-scale acceptance after each official observation",
        "startup_policy": "ECG uses Movesense 20-packet/10 Hz/1.5s gate with 0.5s grace, count-aware 5ms sample-gap checks, and the same five-reference-second candidate eligibility. Observe during unchanged stability checks; provisional alignment at one reference second; reuse retained initial calibration pairs in the tracking window exactly once; startup candidate requires five reference seconds of that continuous history, not five additional seconds; no separate startup minimum; all selected DOTs and the ECG packet model must pass stability and library candidate eligibility before shared official start. Initial calibration deadline starts at first supplied observation; full readiness must occur within total capture budget.",
        "mapping_update_outputs": "mapping_updates.json: shared boundary after_startup_row_count and all per-sensor full-affine/continuous-scale updates; pivots are latest observed source timestamps; prior CSV rows unchanged",
        "software": software_info(),
        "timestamp_provenance": timestamp_provenance(
            supplied_firmware_id=args.gateway_firmware_id,
            inspected_tick_rate_hz=args.inspected_gateway_tick_rate_hz),
        "prediction_error_outputs": {
            "full_epoch_statistics": "summary.json: prediction_error_statistics.epochs (DOT) and ecg_prediction_error_statistics.epochs (ECG packets)",
            "ecg_five_second_intervals": "ecg_prediction_error_intervals.json",
            "ecg_source": "ecg_packets.csv (one error per supplied packet observation)",
            "five_second_intervals": "prediction_error_intervals.json",
            "source": "post-capture observations.csv; model retained window is separate",
        },
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    writer = packet_writer = sample_writer = None
    experiments = {}
    sensor_types = {}
    gateway = None
    summary = {"status": "failed", "failure": None, "cleanup_errors": []}
    code = 1
    official_start = None
    row_count = ecg_packet_rows = ecg_sample_rows = 0
    print(f"Capture output: {output}")
    try:
        writer = CsvRowWriter(output / "observations.csv", RECORD_FIELDS)
        packet_writer = CsvRowWriter(output / "ecg_packets.csv", PACKET_FIELDS)
        sample_writer = CsvRowWriter(output / "ecg_samples.csv", SAMPLE_FIELDS)
        with open_gateway_serial(args.port) as ser:
            gateway = GatewayClient(ser, client_name="clock_mesh_dot_ecg")
            movella = MovellaDotClient(gateway)
            movesense = MovesenseClient(gateway, ecg_only=True)
            selected = []
            connect_attempted = False
            stream_attempted = False
            try:
                gateway.phase = "reset_session"
                gateway.reset_session()
                gateway.phase = "hello"
                manifest["gateway_hello"] = gateway.hello()
                manifest["timestamp_provenance"] = timestamp_provenance(
                    manifest["gateway_hello"], supplied_firmware_id=args.gateway_firmware_id,
                    inspected_tick_rate_hz=args.inspected_gateway_tick_rate_hz)
                gateway.phase = "scan"
                dot_addresses = movella.discover(args.sensor_count, args.scan_timeout_ms)
                ecg_addresses = ([args.movesense_address.strip().upper()] if args.movesense_address
                                 else movesense.discover(1, args.scan_timeout_ms))
                selected = dot_addresses + ecg_addresses
                manifest["selected_addresses"] = selected
                if (any(not address.strip() for address in selected) or
                        len(dot_addresses) != args.sensor_count or len(ecg_addresses) != 1 or
                        len({address.upper() for address in selected}) != args.sensor_count + 1):
                    raise ExperimentFailure("Distinct selected DOT and ECG addresses are required")
                gateway.phase = "connect"
                connect_attempted = True
                # One connect request also makes cross-type sensor-ID validation explicit.
                connections = gateway.connect(selected, timeout_s=args.connect_timeout_s)
                sensor_ids = [c.sensor_id for c in connections]
                if (len(connections) != args.sensor_count + 1 or None in sensor_ids or
                        len(set(sensor_ids)) != args.sensor_count + 1 or
                        {c.address for c in connections} != set(selected)):
                    raise ExperimentFailure("All DOT and ECG connections need unique addresses and sensor IDs")
                movella.connections = [c for c in connections if c.address in dot_addresses]
                movesense.connections = [c for c in connections if c.address in ecg_addresses]
                sensor_types = {c.sensor_id: ("movesense_ecg" if c.address in ecg_addresses else "movella_dot")
                                for c in connections}
                manifest["sensor_types_by_id"] = sensor_types
                manifest["connections"] = [asdict(c) for c in connections]
                gateway.phase = "post_connect_settle"
                time.sleep(args.post_connect_settle_seconds)
                gateway.phase = "configure"
                movella.configure(sampling_rate_hz=60, subscribe_timeout_s=args.subscribe_timeout_s,
                                  write_timeout_s=args.write_timeout_s, without_response=args.without_response)
                movesense.configure(sampling_rate_hz=args.ecg_sampling_rate_hz,
                                    subscribe_timeout_s=args.subscribe_timeout_s,
                                    write_timeout_s=args.write_timeout_s, without_response=args.without_response)
                gateway.phase = "post_config_settle"
                time.sleep(args.post_connect_settle_seconds)
                gateway.phase = "start_streams"
                started_at = time.monotonic()
                experiments = {
                    c.sensor_id: (EcgExperiment if sensor_types[c.sensor_id] == "movesense_ecg" else DotExperiment)(c, started_at=started_at,
                                              calibration_config=config,
                                              no_data_seconds=args.no_data_seconds)
                    for c in connections
                }
                manifest["experiments"] = [
                    {"address": exp.connection.address, "sensor_id": sensor_id,
                     "sensor_type": sensor_types[sensor_id],
                     "stability_gate": asdict(exp.monitor.startup_gate)}
                    for sensor_id, exp in experiments.items()
                ]
                manifest["stability_gate"] = asdict(next(iter(experiments.values())).monitor.startup_gate)
                stream_attempted = True
                starts = movella.start_streams(write_timeout_s=args.write_timeout_s,
                                               without_response=args.without_response)
                starts.update(movesense.start_streams(write_timeout_s=args.write_timeout_s,
                                                      without_response=args.without_response))
                for experiment in experiments.values():
                    address = experiment.connection.address
                    if address in starts:
                        experiment.monitor.mark_stream_started(address, starts[address])
                print("Streaming: startup (stability + calibration) → shared official measurement")
                deadline = started_at + args.capture_seconds
                while time.monotonic() < deadline:
                    for experiment in experiments.values():
                        experiment.check_deadlines(time.monotonic())
                    for connection in connections:
                        if gateway.is_disconnected(connection.address):
                            raise ExperimentFailure(f"Unexpected disconnect: {connection.address}")
                    gateway.phase = "streaming"
                    try:
                        kind, item = gateway.read_item(timeout_s=0.1)
                    except TimeoutError:
                        continue
                    now = time.monotonic()
                    if kind == "stream_frame":
                        experiment = experiments.get(item.sensor_id)
                        if experiment is None:
                            raise ExperimentFailure(f"Unexpected sensor_id {item.sensor_id}")
                        gateway.phase = experiment.phase
                        params = gateway.get_connection_params(
                            experiment.connection.address
                        )

                        if isinstance(experiment, EcgExperiment):
                            row, samples = experiment.process(item, now)
                            if row is not None:
                                row["connection_interval_us"] = params["interval_units"] * 1250 if params else None
                                packet_writer.write_row(row)
                                ecg_packet_rows += 1
                                for sample in samples:
                                    sample_writer.write_row(sample)
                                ecg_sample_rows += len(samples)
                        else:
                            motion = decode_motion(item.payload)
                            row = experiment.process(item, now)
                            row.update(motion)
                            row["connection_interval_us"] = params["interval_units"] * 1250 if params else None
                            writer.write_row(row)
                            row_count += 1
                        if now < deadline and try_start_official(list(experiments.values()), now):
                            official_start = {
                                "host_elapsed_seconds": now - started_at,
                                "after_startup_row_count": row_count,
                                "after_startup_ecg_packet_row_count": ecg_packet_rows,
                                "after_startup_ecg_sample_row_count": ecg_sample_rows,
                                "dot_readiness_host_elapsed_seconds": {
                                    exp.connection.address: exp.ready_at - exp.started_at
                                    for exp in experiments.values() if not isinstance(exp, EcgExperiment)},
                                "all_sensor_readiness_host_elapsed_seconds": {
                                    exp.connection.address: exp.ready_at - exp.started_at
                                    for exp in experiments.values()},
                                "definition": "Boundary after this many CSV data rows; subsequent rows in each CSV are official measurement; ECG boundaries are separate",
                            }
                            print(f"Official measurement started at {now - started_at:.3f}s; "
                                  f"DOT readiness: {official_start['dot_readiness_host_elapsed_seconds']}")
                    elif item.get("type") == "sensor_disconnected":
                        raise ExperimentFailure(f"Unexpected disconnect: {item}")
                    for experiment in experiments.values():
                        experiment.check_deadlines(now)
                for connection in connections:
                    if gateway.is_disconnected(connection.address):
                        raise ExperimentFailure(f"Unexpected disconnect: {connection.address}")
                for experiment in experiments.values():
                    experiment.check_deadlines(time.monotonic())
                    if experiment.calibration_outcome != "aligned":
                        raise ExperimentFailure(f"Capture ended without an aligned model: {experiment.connection.address}")
                if official_start is None:
                    pending = [exp.connection.address for exp in experiments.values()
                               if not exp.ready_for_official_start]
                    raise ExperimentFailure(f"Capture budget exhausted before shared official readiness; pending sensors: {pending}")
                summary["status"] = "completed"
                code = 0
            finally:
                summary["ble_disconnections"] = [address for address in selected
                                                  if gateway.is_disconnected(address)]
                manifest["connection_params_at_capture_end"] = {
                    address: gateway.get_connection_params(address)
                    for address in selected
                }
                ended_at = time.monotonic()
                for experiment in experiments.values():
                    summary["capture_elapsed_seconds"] = ended_at - experiment.started_at
                    if experiment.calibration_outcome == "pending":
                        experiment.calibration_outcome = "interrupted"
                        experiment.calibration_duration_seconds = experiment._calibration_elapsed(ended_at)
                    if experiment.stability_outcome == "pending":
                        experiment.stability_outcome = "interrupted"
                # Independent cleanup attempts: failed stop must not skip disconnect.
                if stream_attempted:
                    # Isolate each stop so one write failure cannot skip another DOT.
                    stop_clients = [movella]
                    if len(movella.connections) > 1:
                        stop_clients = []
                        for connection in movella.connections:
                            client = MovellaDotClient(gateway)
                            client.connections = [connection]
                            stop_clients.append(client)
                    stop_clients.append(movesense)
                    for client in stop_clients:
                        try:
                            gateway.phase = "stop_streams"
                            client.stop_streams(write_timeout_s=args.write_timeout_s,
                                                without_response=args.without_response)
                        except (Exception, KeyboardInterrupt) as exc:
                            addresses = [c.address for c in client.connections]
                            summary["cleanup_errors"].append(f"stop {addresses}: {type(exc).__name__}: {exc}")
                    # Commands may cache stream tails. Drain without extending the
                    # fixed capture budget or accepting more model observations.
                    try:
                        gateway.phase = "post_stop_drain"
                        summary["post_stop_drain"] = drain_after_stop(gateway, output, addresses=selected)
                    except (Exception, KeyboardInterrupt) as exc:
                        summary["cleanup_errors"].append(f"drain: {type(exc).__name__}: {exc}")
                summary["ble_disconnections"] = [address for address in selected
                                                  if gateway.is_disconnected(address)]
                if connect_attempted:
                    for address in selected:
                        try:
                            gateway.phase = "disconnect"
                            gateway.disconnect([address], timeout_s=args.disconnect_timeout_s, allow_timeout=True)
                        except (Exception, KeyboardInterrupt) as exc:
                            summary["cleanup_errors"].append(f"disconnect {address}: {type(exc).__name__}: {exc}")
                gateway.phase = "idle"
    except KeyboardInterrupt:
        summary.update(status="cancelled", failure="KeyboardInterrupt")
        code = 130
    except Exception as exc:
        summary.update(status="failed", failure=f"{type(exc).__name__}: {exc}")
        code = 1
    finally:
        try:
            for capture_writer in (writer, packet_writer, sample_writer):
                if capture_writer is not None:
                    capture_writer.close()
        finally:
            if summary["cleanup_errors"] and code == 0:
                summary["status"] = "failed"
                code = 1
            if experiments:
                summary["experiments"] = [{**exp.summary(), "sensor_type": sensor_types[exp.connection.sensor_id]} for exp in experiments.values()]
                for entry, exp in zip(manifest["experiments"], experiments.values()):
                    entry["result"] = {**exp.summary(), "sensor_type": entry["sensor_type"]}
                    entry["ended_epochs"] = exp.ended_epochs
                summary["ended_epochs"] = [epoch for exp in experiments.values() for epoch in exp.ended_epochs]
                if len(experiments) == 1:
                    summary["experiment"] = summary["experiments"][0]
                summary["elapsed_seconds_including_cleanup"] = time.monotonic() - started_at
                if gateway is not None:
                    summary["stream_summary_lines"] = [
                        line for exp in experiments.values() for line in exp.monitor.summary_lines(gateway)
                    ]
            try:
                if writer is not None:
                    summary["prediction_error_statistics"] = write_prediction_error_report(
                        output / "observations.csv", output / "prediction_error_intervals.json")
                    summary["ecg_prediction_error_statistics"] = write_prediction_error_report(
                        output / "ecg_packets.csv", output / "ecg_prediction_error_intervals.json")
                    for report in (summary["prediction_error_statistics"], summary["ecg_prediction_error_statistics"]):
                        for epoch in report["epochs"]:
                            epoch["sensor_type"] = sensor_types.get(epoch["sensor_id"])
            except Exception as exc:
                summary["analysis_error"] = f"{type(exc).__name__}: {exc}"
                summary["status"] = "failed"
                if code == 0:
                    code = 1
            summary["row_counts"] = manifest["row_counts"] = {
                "dot_observations": row_count, "ecg_packet_rows": ecg_packet_rows,
                "ecg_sample_rows": ecg_sample_rows}
            summary["official_start"] = manifest["official_start"] = official_start
            (output / "mapping_updates.json").write_text(json.dumps({
                "official_start": official_start,
                "updates": sorted(({**update, "sensor_type": sensor_types[exp.connection.sensor_id]}
                                   for exp in experiments.values() for update in exp.mapping_updates),
                                  key=lambda update: update["host_elapsed_seconds"]),
            }, indent=2) + "\n", encoding="utf-8")
            manifest.update(status=summary["status"], finished_at_utc=datetime.now(timezone.utc).isoformat())
            (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
            for line in console_summary(summary):
                print(line)
            if "prediction_error_statistics" in summary:
                print(f"Five-second prediction-error intervals: {output / 'prediction_error_intervals.json'}")
            print(f"Capture output: {output}")
    return code


def drain_after_stop(gateway, output, *, addresses=(), quiet_window_s=0.35, max_drain_s=2.0):
    """Preserve tail frames separately; they are outside the measurement budget."""
    deadline = time.monotonic() + max_drain_s
    quiet_deadline = time.monotonic() + quiet_window_s
    counts = {}
    with (output / "post_stop_frames.jsonl").open("w", encoding="utf-8") as handle:
        while time.monotonic() < min(deadline, quiet_deadline):
            for address in addresses:
                if gateway.is_disconnected(address):
                    raise RuntimeError(f"Unexpected disconnect during drain: {address}")
            try:
                kind, item = gateway.read_item(timeout_s=0.1)
            except TimeoutError:
                continue
            if kind == "stream_frame":
                counts[item.sensor_id] = counts.get(item.sensor_id, 0) + 1
                handle.write(json.dumps({**asdict(item), "payload": item.payload.hex(),
                                         "stream_phase": "post_stop_drain"}) + "\n")
                quiet_deadline = time.monotonic() + quiet_window_s
            elif item.get("type") == "sensor_disconnected":
                raise RuntimeError(f"Unexpected disconnect during drain: {item}")
    return counts


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        validate_args(args)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        raise SystemExit(run(args))
    except ModuleNotFoundError as exc:
        if exc.name != "clock_mesh":
            raise
        parser.exit(2, "Install the separate clock-mesh checkout and tooling[clock-mesh]; see ClockMeshTest/README.md.\n")


if __name__ == "__main__":
    main()
