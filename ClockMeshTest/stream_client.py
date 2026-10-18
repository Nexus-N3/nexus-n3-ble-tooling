"""Bounded one-DOT 60 Hz experiment with durable observation evidence."""
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
    parser = argparse.ArgumentParser(description="ClockMesh: exactly one Movella DOT at 60 Hz.")
    parser.add_argument("--port", default=DEFAULT_PORT)
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
    if args.calibration_deadline_seconds < 2:
        raise ValueError("Calibration deadline must allow the library's two-second calibration span")
    if args.capture_seconds <= 5 + args.calibration_deadline_seconds:
        raise ValueError("Capture must exceed the five-second gate plus calibration deadline")


def run(args) -> int:
    from clock_mesh import ClockAlignmentConfig
    from .alignment import DotExperiment, ExperimentFailure, RECORD_FIELDS

    validate_args(args)
    config = ClockAlignmentConfig(calibration_deadline_ns=round(args.calibration_deadline_seconds * 1e9))
    output = args.output_dir.resolve() / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ_") + uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema_version": 2, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "running", "output_directory": str(output),
        "configuration": {**vars(args), "output_dir": str(args.output_dir),
                          "sensor_count": 1, "sampling_rate_hz": 60,
                          "clock_alignment": asdict(config)},
        "capture_duration_origin": "host monotonic time immediately before start_streams; includes acquisition and calibration",
        "calibration_duration_origin": "first observation supplied after stability gate; host elapsed and gateway reference span recorded separately",
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
        "continuity_policy": "No inferred rollover; backward/duplicate DOT, backward gateway, or delta disagreement >1s creates new epoch/model and ends run",
        "prediction_error_definition": "observed gateway reference minus accepted fixed mapping; BLE delivery delay included; no fit updates",
        "candidate_diagnostics": "candidate prediction/error use the pre-observe fit; candidate scale/count use the post-observe fit; no candidate acceptance",
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
    experiment = None
    gateway = None
    summary = {"status": "failed", "failure": None, "cleanup_errors": []}
    code = 1
    print(f"Capture output: {output}")
    try:
        writer = CsvRowWriter(output / "observations.csv", RECORD_FIELDS)
        with open_gateway_serial(args.port) as ser:
            gateway = GatewayClient(ser, client_name="clock_mesh_one_dot")
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
                selected = movella.discover(1, args.scan_timeout_ms)
                manifest["selected_addresses"] = selected
                gateway.phase = "connect"
                connect_attempted = True
                connections = movella.connect(selected, timeout_s=args.connect_timeout_s)
                if len(connections) != 1 or connections[0].sensor_id is None:
                    raise ExperimentFailure("Exactly one identified connection is required")
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
                experiment = DotExperiment(connections[0], started_at=started_at,
                                           calibration_config=config, no_data_seconds=args.no_data_seconds)
                manifest["stability_gate"] = asdict(experiment.monitor.startup_gate)
                stream_attempted = True
                starts = movella.start_streams(write_timeout_s=args.write_timeout_s,
                                               without_response=args.without_response)
                for address, stamp in starts.items():
                    experiment.monitor.mark_stream_started(address, stamp)
                print("Streaming: acquisition → stability gate → calibration → measurement")
                deadline = started_at + args.capture_seconds
                while time.monotonic() < deadline:
                    experiment.check_deadlines(time.monotonic())
                    gateway.phase = experiment.phase
                    try:
                        kind, item = gateway.read_item(timeout_s=0.1)
                    except TimeoutError:
                        continue
                    now = time.monotonic()
                    if kind == "stream_frame":
                        params = gateway.get_connection_params(
                            experiment.connection.address
                        )

                        row = experiment.process(item, now)
                        row["connection_interval_us"] = (
                            params["interval_units"] * 1_250
                            if params is not None
                            else None
                        )

                        writer.write_row(row)
                        experiment.check_deadlines(now)
                    elif item.get("type") == "sensor_disconnected":
                        raise ExperimentFailure(f"Unexpected disconnect: {item}")
                    else:
                        experiment.check_deadlines(now)
                experiment.check_deadlines(time.monotonic())
                if experiment.calibration_outcome != "aligned":
                    raise ExperimentFailure("Capture ended without an aligned model")
                summary["status"] = "completed"
                code = 0
            finally:
                manifest["connection_params_at_capture_end"] = {
                    address: gateway.get_connection_params(address)
                    for address in selected
                }
                if experiment is not None:
                    ended_at = time.monotonic()
                    summary["capture_elapsed_seconds"] = ended_at - experiment.started_at
                    if experiment.calibration_outcome == "pending":
                        experiment.calibration_outcome = "interrupted"
                        experiment.calibration_duration_seconds = experiment._calibration_elapsed(ended_at)
                    if experiment.stability_outcome == "pending":
                        experiment.stability_outcome = "interrupted"
                # Independent cleanup attempts: failed stop must not skip disconnect.
                if stream_attempted:
                    try:
                        gateway.phase = "stop_streams"
                        movella.stop_streams(write_timeout_s=args.write_timeout_s,
                                             without_response=args.without_response)
                    except (Exception, KeyboardInterrupt) as exc:
                        summary["cleanup_errors"].append(f"stop: {type(exc).__name__}: {exc}")
                if connect_attempted:
                    try:
                        gateway.phase = "disconnect"
                        if movella.connections:
                            movella.disconnect_all(timeout_s=args.disconnect_timeout_s)
                        else:
                            gateway.disconnect(selected, timeout_s=args.disconnect_timeout_s, allow_timeout=True)
                    except (Exception, KeyboardInterrupt) as exc:
                        summary["cleanup_errors"].append(f"disconnect: {type(exc).__name__}: {exc}")
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
            if experiment is not None:
                summary["experiment"] = experiment.summary()
                summary["ended_epochs"] = experiment.ended_epochs
                summary["elapsed_seconds_including_cleanup"] = time.monotonic() - experiment.started_at
                if gateway is not None:
                    summary["stream_summary_lines"] = experiment.monitor.summary_lines(gateway)
            try:
                if writer is not None:
                    summary["prediction_error_statistics"] = write_prediction_error_report(
                        output / "observations.csv", output / "prediction_error_intervals.json")
            except Exception as exc:
                summary["analysis_error"] = f"{type(exc).__name__}: {exc}"
                summary["status"] = "failed"
                if code == 0:
                    code = 1
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
