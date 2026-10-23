"""Bounded multi-DOT 60 Hz experiment with durable observation evidence."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from importlib import metadata
import json
import math
from pathlib import Path
import platform
import subprocess
import time
from uuid import uuid4

from MovellaDot.client import MovellaDotClient
from NexusBLESdk import CsvRowWriter, DEFAULT_PORT, GatewayClient, open_gateway_serial

from .evidence import console_summary, write_prediction_error_report
from .provenance import timestamp_provenance
from .motion import MOTION_UNITS, decode_motion


def repository_info(source: Path) -> dict:
    """Find the actual imported source checkout; never attribute a parent repo."""
    source = source.resolve()
    root = next((p for p in (source, *source.parents) if (p / ".git").exists()), None)
    result = {"source_path": str(source), "root": str(root) if root else None,
              "revision": None, "dirty": None}
    if root is None:
        return result
    try:
        def git(*args):
            return subprocess.run(["git", "-C", str(root), *args], check=True,
                                  capture_output=True, text=True, timeout=5).stdout.strip()
        result.update(revision=git("rev-parse", "HEAD"),
                      dirty=bool(git("status", "--porcelain", "--untracked-files=normal")))
    except (OSError, subprocess.SubprocessError) as exc:
        result["inspection_error"] = str(exc)
    return result


def software_info() -> dict:
    import clock_mesh
    versions = {}
    for name in ("nexus-n3-ble-tooling", "clock-mesh", "pyserial"):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return {"python": platform.python_version(), "platform": platform.platform(),
            "versions": versions,
            "repositories": {
                "tooling": repository_info(Path(__file__)),
                "clock_mesh": repository_info(Path(clock_mesh.__file__)),
            }}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ClockMesh: Movella DOTs at 60 Hz.")
    parser.add_argument("--port", default=DEFAULT_PORT)
    parser.add_argument("--sensor-count", type=int, default=1,
                        help="Number of DOTs to discover and connect (default: 1).")
    parser.add_argument("--gateway-firmware-id", help="User-supplied running build identifier; never labeled device-reported.")
    parser.add_argument("--inspected-gateway-tick-rate-hz", type=int, default=31_250,
                        help="Supplied inspected-build tick rate, not a runtime-verified capability (default: 31250 Hz / 32 us).")
    parser.add_argument("--capture-seconds", type=float, default=30.0,
                        help="Total stream budget from immediately before the start command; includes gate and calibration.")
    parser.add_argument("--calibration-deadline-seconds", type=float, default=5.0)
    parser.add_argument("--no-data-seconds", type=float, default=3.0)
    parser.add_argument("--output-dir", type=Path, default=Path("output-files/clock-mesh"),
                        help="Parent for a new unique capture directory.")
    parser.add_argument("--scan-timeout-ms", type=int, default=5000)
    parser.add_argument("--connect-timeout-s", type=float, default=30.0)
    parser.add_argument("--subscribe-timeout-s", type=float, default=10.0)
    parser.add_argument("--write-timeout-s", type=float, default=10.0)
    parser.add_argument("--disconnect-timeout-s", type=float, default=5.0)
    parser.add_argument("--post-connect-settle-seconds", type=float, default=2.0)
    parser.add_argument("--without-response", action="store_true")
    return parser


def validate_args(args) -> None:
    if args.sensor_count < 1:
        raise ValueError("sensor_count must be at least 1")
    for key in ("capture_seconds", "calibration_deadline_seconds", "no_data_seconds",
                "connect_timeout_s", "subscribe_timeout_s", "write_timeout_s", "disconnect_timeout_s"):
        value = getattr(args, key)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be finite and positive")
    if not math.isfinite(args.post_connect_settle_seconds) or args.post_connect_settle_seconds < 0:
        raise ValueError("post_connect_settle_seconds must be finite and nonnegative")
    if args.scan_timeout_ms <= 0:
        raise ValueError("scan_timeout_ms must be positive")
    if args.inspected_gateway_tick_rate_hz <= 0:
        raise ValueError("inspected_gateway_tick_rate_hz must be positive")
    if args.calibration_deadline_seconds < 1:
        raise ValueError("Calibration deadline must allow the provisional one-second calibration span")


def run(args) -> int:
    from clock_mesh import ClockAlignmentConfig
    from .alignment import DotExperiment, ExperimentFailure, RECORD_FIELDS, try_start_official

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
        "schema_version": 3, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "running", "output_directory": str(output),
        "configuration": {**vars(args), "output_dir": str(args.output_dir),
                          "sampling_rate_hz": 60,
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
                "DOT time on gateway reference timeline; not host/master time"
            ),
            "connection_interval_us": (
                "Latest connection interval reported to the host when processing "
                "the frame; interval_units * 1250; empty if unknown; "
                "not an exact association with the radio reception event"
            ),
        },
        "motion_units": MOTION_UNITS,
        "motion_payload": "MovellaDot.profile.parse_packet; configured start 01011A; <I10f prefix (44 bytes); quaternion w,x,y,z, acceleration x,y,z, angular velocity x,y,z; unchanged sensor values",
        "continuity_policy": "No inferred rollover; backward/duplicate DOT, backward gateway, or delta disagreement >1s creates new epoch/model and ends run",
        "prediction_error_definition": "observed gateway reference minus pre-update accepted mapping; BLE delivery delay included; official measurement only in error reports",
        "candidate_diagnostics": "candidate prediction/error use the pre-observe fit; candidate scale/count/span use the post-observe fit; full affine acceptance once at shared official start, then continuous-scale acceptance after each official observation",
        "startup_policy": "Observe during unchanged stability checks; provisional alignment at one reference second; reuse retained initial calibration pairs in the tracking window exactly once; startup candidate requires five reference seconds of that continuous history, not five additional seconds; no separate startup minimum; all selected DOTs must pass stability and library candidate eligibility before shared official start. Initial calibration deadline starts at first supplied observation; full readiness must occur within total capture budget.",
        "mapping_update_outputs": "mapping_updates.json: shared boundary after_startup_row_count and all per-DOT full-affine/continuous-scale updates; pivots are latest observed source timestamps; prior CSV rows unchanged",
        "software": software_info(),
        "timestamp_provenance": timestamp_provenance(
            supplied_firmware_id=args.gateway_firmware_id,
            inspected_tick_rate_hz=args.inspected_gateway_tick_rate_hz),
        "prediction_error_outputs": {
            "full_epoch_statistics": "summary.json: prediction_error_statistics.epochs",
            "five_second_intervals": "prediction_error_intervals.json",
            "source": "post-capture observations.csv; model retained window is separate",
        },
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    writer = None
    experiments = {}
    gateway = None
    summary = {"status": "failed", "failure": None, "cleanup_errors": []}
    code = 1
    official_start = None
    row_count = 0
    print(f"Capture output: {output}")
    try:
        writer = CsvRowWriter(output / "observations.csv", RECORD_FIELDS)
        with open_gateway_serial(args.port) as ser:
            gateway = GatewayClient(ser, client_name=(
                "clock_mesh_one_dot" if args.sensor_count == 1 else "clock_mesh_multi_dot"))
            movella = MovellaDotClient(gateway)
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
                selected = movella.discover(args.sensor_count, args.scan_timeout_ms)
                manifest["selected_addresses"] = selected
                if len(selected) != args.sensor_count or len(set(selected)) != args.sensor_count:
                    raise ExperimentFailure(f"Exactly {args.sensor_count} distinct DOTs are required")
                gateway.phase = "connect"
                connect_attempted = True
                connections = movella.connect(selected, timeout_s=args.connect_timeout_s)
                sensor_ids = [c.sensor_id for c in connections]
                if (len(connections) != args.sensor_count or None in sensor_ids or
                        len(set(sensor_ids)) != args.sensor_count or
                        {c.address for c in connections} != set(selected)):
                    raise ExperimentFailure(f"Exactly {args.sensor_count} uniquely identified connections are required")
                manifest["connections"] = [asdict(c) for c in connections]
                gateway.phase = "post_connect_settle"
                time.sleep(args.post_connect_settle_seconds)
                gateway.phase = "configure"
                movella.configure(sampling_rate_hz=60, subscribe_timeout_s=args.subscribe_timeout_s,
                                  write_timeout_s=args.write_timeout_s, without_response=args.without_response)
                gateway.phase = "post_config_settle"
                time.sleep(args.post_connect_settle_seconds)
                gateway.phase = "start_streams"
                started_at = time.monotonic()
                experiments = {
                    c.sensor_id: DotExperiment(c, started_at=started_at,
                                              calibration_config=config,
                                              no_data_seconds=args.no_data_seconds)
                    for c in connections
                }
                manifest["experiments"] = [
                    {"address": exp.connection.address, "sensor_id": sensor_id,
                     "stability_gate": asdict(exp.monitor.startup_gate)}
                    for sensor_id, exp in experiments.items()
                ]
                manifest["stability_gate"] = asdict(next(iter(experiments.values())).monitor.startup_gate)
                stream_attempted = True
                starts = movella.start_streams(write_timeout_s=args.write_timeout_s,
                                               without_response=args.without_response)
                for experiment in experiments.values():
                    address = experiment.connection.address
                    if address in starts:
                        experiment.monitor.mark_stream_started(address, starts[address])
                print("Streaming: startup (stability + calibration) → shared official measurement")
                deadline = started_at + args.capture_seconds
                while time.monotonic() < deadline:
                    for experiment in experiments.values():
                        experiment.check_deadlines(time.monotonic())
                    gateway.phase = (next(iter(experiments.values())).phase
                                     if args.sensor_count == 1 else "streaming")
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

                        motion = decode_motion(item.payload)
                        row = experiment.process(item, now)
                        row.update(motion)
                        row["connection_interval_us"] = (
                            params["interval_units"] * 1_250
                            if params is not None
                            else None
                        )

                        writer.write_row(row)
                        row_count += 1
                        if now < deadline and try_start_official(list(experiments.values()), now):
                            official_start = {
                                "host_elapsed_seconds": now - started_at,
                                "after_startup_row_count": row_count,
                                "dot_readiness_host_elapsed_seconds": {
                                    exp.connection.address: exp.ready_at - exp.started_at
                                    for exp in experiments.values()},
                                "definition": "Boundary after this many CSV data rows; all subsequent rows are official measurement",
                            }
                            print(f"Official measurement started at {now - started_at:.3f}s; "
                                  f"DOT readiness: {official_start['dot_readiness_host_elapsed_seconds']}")
                    elif item.get("type") == "sensor_disconnected":
                        raise ExperimentFailure(f"Unexpected disconnect: {item}")
                    for experiment in experiments.values():
                        experiment.check_deadlines(now)
                for experiment in experiments.values():
                    experiment.check_deadlines(time.monotonic())
                    if experiment.calibration_outcome != "aligned":
                        raise ExperimentFailure(f"Capture ended without an aligned model: {experiment.connection.address}")
                if official_start is None:
                    pending = [exp.connection.address for exp in experiments.values()
                               if not exp.ready_for_official_start]
                    raise ExperimentFailure(f"Capture budget exhausted before shared official readiness; pending DOTs: {pending}")
                summary["status"] = "completed"
                code = 0
            finally:
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
                    for client in stop_clients:
                        try:
                            gateway.phase = "stop_streams"
                            client.stop_streams(write_timeout_s=args.write_timeout_s,
                                                without_response=args.without_response)
                        except (Exception, KeyboardInterrupt) as exc:
                            addresses = [c.address for c in client.connections]
                            summary["cleanup_errors"].append(f"stop {addresses}: {type(exc).__name__}: {exc}")
                if connect_attempted:
                    for address in selected:
                        try:
                            gateway.phase = "disconnect"
                            if args.sensor_count == 1 and movella.connections:
                                movella.disconnect_all(timeout_s=args.disconnect_timeout_s)
                            else:
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
            if writer is not None:
                writer.close()
        finally:
            if summary["cleanup_errors"] and code == 0:
                summary["status"] = "failed"
                code = 1
            if experiments:
                summary["experiments"] = [exp.summary() for exp in experiments.values()]
                for entry, exp in zip(manifest["experiments"], experiments.values()):
                    entry["result"] = exp.summary()
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
            except Exception as exc:
                summary["analysis_error"] = f"{type(exc).__name__}: {exc}"
                summary["status"] = "failed"
                if code == 0:
                    code = 1
            summary["official_start"] = manifest["official_start"] = official_start
            (output / "mapping_updates.json").write_text(json.dumps({
                "official_start": official_start,
                "updates": sorted((update for exp in experiments.values()
                                   for update in exp.mapping_updates),
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
