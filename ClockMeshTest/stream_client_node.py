"""DOT -> gateway -> node monotonic capture, with a shared transport clock model."""
from __future__ import annotations

# Support both `python -m ...` and the documented direct-script invocation.
if __package__ in (None, ""):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "ClockMeshTest"

from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
from uuid import uuid4

from MovellaDot.client import MovellaDotClient
from NexusBLESdk import CsvRowWriter, GatewayClient, open_gateway_serial

from .stream_client import build_parser as dot_parser, validate_args as validate_dot_args, software_info
from .provenance import timestamp_provenance


def build_parser():
    parser = dot_parser()
    parser.description = "ClockMesh: DOTs at 60 Hz mapped through one gateway clock to node monotonic time."
    parser.set_defaults(sensor_count=3, output_dir=Path("output-files/clock-mesh-node"))
    for action in parser._actions:
        if action.dest == "sensor_count":
            action.help = "Number of DOTs (default: 3)."
        elif action.dest == "capture_seconds":
            action.help = "Official measurement duration, excluding startup calibration (default: 30)."
    parser.add_argument("--startup-timeout-seconds", type=float, default=15.,
                        help="Maximum startup/readiness duration before official measurement (default: 15).")
    return parser


def validate_args(args):
    validate_dot_args(args)
    if not math.isfinite(args.startup_timeout_seconds) or args.startup_timeout_seconds <= 5:
        raise ValueError("startup_timeout_seconds must be finite and greater than five seconds")


def drain(gateway, event, addresses, *, max_seconds=2., quiet_seconds=.35):
    """Preserve cached stop tails without extending official statistics/models."""
    deadline = time.monotonic() + max_seconds
    quiet = time.monotonic() + quiet_seconds
    count = 0
    while time.monotonic() < min(deadline, quiet):
        for address in addresses:
            if gateway.is_disconnected(address):
                raise RuntimeError(f"Unexpected disconnect during drain: {address}")
        try:
            kind, item = gateway.read_item(timeout_s=.1)
        except TimeoutError:
            continue
        if kind == "stream_frame":
            event("post_stop_frame", **frame_evidence(item))
            count += 1
            quiet = time.monotonic() + quiet_seconds
        elif item.get("type") in ("sensor_disconnected", "error"):
            event("gateway_event", message=item)
            raise RuntimeError(f"Gateway event during drain: {item}")
    return count


def frame_evidence(frame):
    return {**asdict(frame), "payload": frame.payload.hex()}


def run(args):
    from clock_mesh import ClockAlignmentConfig
    from .alignment import ExperimentFailure
    from .node_alignment import NODE_FIELDS, NodeExperiment

    validate_args(args)
    config = ClockAlignmentConfig(target_calibration_span_ns=1_000_000_000,
                                  calibration_deadline_ns=round(args.calibration_deadline_seconds * 1e9),
                                  min_tracking_acceptance_span_ns=5_000_000_000,
                                  reuse_calibration_history=True)
    output = args.output_dir.resolve() / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ_") + uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": 1, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "output_directory": str(output), "status": "running",
        "configuration": {**vars(args), "output_dir": str(args.output_dir), "sampling_rate_hz": 60,
                          "dot_clock_alignment": asdict(config),
                          "gateway_node_clock_alignment": {**asdict(config), "nominal_scale_ns_per_source_unit": 1.0}},
        "clock_domains": {"dot_source": "uint32 microseconds; no inferred unwrap",
                          "gateway_callback": "uint64 microseconds; reference/source nanoseconds = gateway_timestamp_us * 1000",
                          "node_reference": "time.monotonic_ns(); not UTC or wall time"},
        "mapping_composition": "gateway_node_model.map(sensor_model.map(raw_dot_timestamp_us))",
        "receive_boundary": "time.monotonic_ns() immediately after serial.read(256) returns; provenance of the frame checksum byte identifies the read that made the full frame available; never replaced at decode/application time",
        "batch_policy": "Frames completed by one read share receive timestamp/read ID/size and have a zero-based batch index. A partial next frame gets the next completing read's timestamp. Equal callback and receive times are allowed, reversals terminate with a new epoch.",
        "model_topology": "One sensor-to-gateway model per DOT; exactly one shared gateway-to-node model observing every selected frame",
        "lifecycle": "One reference second provisional calibration, reused history, all DOT stability gates and all models' five-reference-second candidate eligibility; full-affine acceptance at shared official start; continuous scale-only acceptance at each model's latest observation pivot during measurement",
        "capture_duration_origin": "official_start_node_monotonic_ns; startup excluded, bounded separately by startup_timeout_seconds",
        "session_time_definition": "mapped_node_ns - official_start_node_monotonic_ns, official phase only with available mappings; may be negative for queued/transport-delayed frames; phase boundary is application processing order, not retrospective time filtering",
        "prediction_errors": "Observed reference minus pre-update accepted mapping. DOT residual includes BLE delivery, gateway-node residual/offset includes serial/USB/host transport latency; neither verifies physical one-way clock offset.",
        "continuity_policy": "Existing per-DOT continuity/reset policy; shared gateway or node reception reversal records new shared epoch and fails; equal timestamps retain original batching",
        "sequence_policy": "sequence is host decoded-frame order; sensor_sequence is per-DOT host order, not a device-provided counter",
        "software": software_info(),
        "timestamp_provenance": timestamp_provenance(supplied_firmware_id=args.gateway_firmware_id,
                                                      inspected_tick_rate_hz=args.inspected_gateway_tick_rate_hz),
        "outputs": {"observations.csv": "DOT fields plus early reception, both mappings, residuals and session timestamps",
                    "mapping_updates.json": "all successful DOT updates; shared acceptance attempts including rejected decisions",
                    "events.jsonl": "lifecycle/errors and raw frames outside measurement or rejected by parsing",
                    "summary.json": "per-model outcomes and phase-separated diagnostics"},
    }
    def save(name, data):
        (output / name).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    save("manifest.json", manifest)
    summary = {"status": "failed", "failure": None, "cleanup_errors": []}
    experiment = gateway = movella = writer = events = None
    selected = []
    connect_attempted = stream_attempted = False
    official = None
    rows = 0
    code = 1
    print(f"Capture output: {output}")
    def event(kind, **values):
        events.write(json.dumps({"event": kind, "processing_monotonic_ns": time.monotonic_ns(), **values}) + "\n")
        events.flush()
    try:
        writer = CsvRowWriter(output / "observations.csv", NODE_FIELDS)
        events = (output / "events.jsonl").open("w", encoding="utf-8")
        with open_gateway_serial(args.port) as serial:
            gateway = GatewayClient(serial, client_name="clock_mesh_node")
            movella = MovellaDotClient(gateway)
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
                ids = [c.sensor_id for c in connections]
                if (len(ids) != args.sensor_count or None in ids or len(set(ids)) != len(ids)
                        or {c.address for c in connections} != set(selected)):
                    raise ExperimentFailure("Connections require unique selected addresses and sensor IDs")
                manifest["connections"] = [asdict(c) for c in connections]
                gateway.phase = "post_connect_settle"
                time.sleep(args.post_connect_settle_seconds)
                gateway.phase = "configure"
                movella.configure(sampling_rate_hz=60, subscribe_timeout_s=args.subscribe_timeout_s,
                                  write_timeout_s=args.write_timeout_s, without_response=args.without_response)
                gateway.phase = "post_config_settle"
                time.sleep(args.post_connect_settle_seconds)
                started = time.monotonic()
                startup_deadline = started + args.startup_timeout_seconds
                experiment = NodeExperiment(connections, started_at=started, config=config,
                                            no_data_seconds=args.no_data_seconds)
                manifest["stability_gates"] = {sid: asdict(exp.monitor.startup_gate)
                                                for sid, exp in experiment.dots.items()}
                gateway.phase = "start_streams"
                stream_attempted = True
                starts = movella.start_streams(write_timeout_s=args.write_timeout_s,
                                               without_response=args.without_response)
                for exp in experiment.dots.values():
                    exp.monitor.mark_stream_started(exp.connection.address, starts.get(exp.connection.address))
                deadline_ns = None
                while True:
                    now, now_ns = time.monotonic(), time.monotonic_ns()
                    if deadline_ns is not None and now_ns >= deadline_ns:
                        break
                    if deadline_ns is None and now >= startup_deadline:
                        pending = [sid for sid, exp in experiment.dots.items() if not exp.ready_for_official_start]
                        raise ExperimentFailure(f"Startup readiness timeout: pending DOTs={pending}; shared_ready={experiment.gateway_node.ready}")
                    experiment.check_deadlines(now, now_ns)
                    for address in selected:
                        if gateway.is_disconnected(address):
                            raise ExperimentFailure(f"Unexpected disconnect: {address}")
                    gateway.phase = "measurement" if official else "startup"
                    try:
                        kind, item = gateway.read_item(timeout_s=.1)
                    except TimeoutError:
                        continue
                    now, now_ns = time.monotonic(), time.monotonic_ns()
                    if kind != "stream_frame":
                        event("gateway_event", message=item)
                        if item.get("type") in ("sensor_disconnected", "error"):
                            raise ExperimentFailure(f"Gateway event: {item}")
                        continue
                    if deadline_ns is not None and now_ns >= deadline_ns:
                        event("after_measurement_frame", **frame_evidence(item))
                        break
                    try:
                        row = experiment.process(item, now, now_ns)
                    except Exception:
                        event("rejected_frame", **frame_evidence(item))
                        raise
                    exp = experiment.dots[item.sensor_id]
                    params = gateway.get_connection_params(exp.connection.address)
                    row["connection_interval_us"] = params["interval_units"] * 1250 if params else None
                    writer.write_row(row)
                    rows += 1
                    experiment.check_deadlines(now, now_ns)
                    boundary_ns = time.monotonic_ns()
                    if now < startup_deadline and experiment.try_start(now, boundary_ns):
                        official = {"official_start_node_monotonic_ns": boundary_ns, "after_startup_row_count": rows,
                                    "startup_elapsed_seconds": now - started}
                        deadline_ns = boundary_ns + round(args.capture_seconds * 1e9)
                        event("official_start", **official)
                        print(f"Official measurement started after {now - started:.3f}s startup")
                experiment.check_deadlines(time.monotonic(), time.monotonic_ns())
                for address in selected:
                    if gateway.is_disconnected(address):
                        raise ExperimentFailure(f"Unexpected disconnect: {address}")
                summary.update(status="completed", official_elapsed_seconds=(
                    time.monotonic_ns() - official["official_start_node_monotonic_ns"]) / 1e9)
                code = 0
            finally:
                # Each stop/disconnect is independent, including partial-start failures.
                if stream_attempted:
                    for connection in movella.connections:
                        try:
                            gateway.phase = "stop_streams"
                            stop_client = MovellaDotClient(gateway)
                            stop_client.connections = [connection]
                            stop_client.stop_streams(write_timeout_s=args.write_timeout_s,
                                                     without_response=args.without_response)
                        except (Exception, KeyboardInterrupt) as exc:
                            summary["cleanup_errors"].append(f"stop {connection.address}: {type(exc).__name__}: {exc}")
                    try:
                        gateway.phase = "post_stop_drain"
                        summary["post_stop_drain_frames"] = drain(gateway, event, selected)
                    except (Exception, KeyboardInterrupt) as exc:
                        summary["cleanup_errors"].append(f"drain: {type(exc).__name__}: {exc}")
                summary["ble_disconnections"] = [a for a in selected if gateway.is_disconnected(a)]
                manifest["connection_params_at_capture_end"] = {a: gateway.get_connection_params(a) for a in selected}
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
        if summary["cleanup_errors"] and code == 0:
            summary["status"], code = "failed", 1
        if events is not None:
            event("capture_end", status=summary["status"], failure=summary["failure"], cleanup_errors=summary["cleanup_errors"])
            events.close()
        if writer is not None:
            writer.close()
        updates = []
        if experiment is not None:
            summary.update(experiment.summary())
            updates = [{**update, "model_id": f"dot_{sid}_to_gateway", "accepted": True}
                       for sid, exp in experiment.dots.items() for update in exp.mapping_updates]
            updates += experiment.gateway_node.mapping_updates
            updates.sort(key=lambda update: (update.get("processing_monotonic_ns", 0), update["model_id"]))
        if gateway is not None:
            summary["gateway_transport"] = {
                "checksum_failures": gateway.stream_checksum_failures,
                "resync_drop_bytes": gateway.stream_resync_drop_bytes,
                "resync_events": gateway.stream_resync_events,
                "notification_drop_count": gateway.notification_drop_count,
                "reported_transport_stats": gateway.gateway_transport_stats,
            }
        summary.update(row_count=rows, official_start=official)
        manifest.update(status=summary["status"], official_start=official,
                        finished_at_utc=datetime.now(timezone.utc).isoformat())
        save("mapping_updates.json", {"official_start": official, "updates": updates})
        save("manifest.json", manifest)
        save("summary.json", summary)
        print(f"Capture {summary['status']}: {summary['failure'] or str(rows) + ' observations'}")
        print(f"Capture output: {output}")
    return code


def main():
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
        parser.exit(2, "Install the local clock-mesh checkout and tooling[clock-mesh]; see ClockMeshTest/README.md.\n")


if __name__ == "__main__":
    main()
