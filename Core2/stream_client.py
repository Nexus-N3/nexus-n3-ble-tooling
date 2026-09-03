#!/usr/bin/env python3

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(
        0,
        str(Path(__file__).resolve().parents[1]),
    )


from NexusBLESdk import (
    CsvRowWriter,
    DEFAULT_PORT,
    GatewayClient,
    StartupGateConfig,
    build_output_path,
    open_gateway_serial,
)

from Core2.client import Core2Client
from Core2.monitor import Core2StreamMonitor
from Core2.profile import EXPECTED_NOTIFICATION_RATE_HZ


# ---------------------------------------------------------------------------
# CORE 2 startup gate defaults
# ---------------------------------------------------------------------------

# CORE currently emits one BLE measurement notification per second.
#
# At 1 Hz we need substantially different startup-gate values from the
# higher-rate motion and ECG sensors. The gate ignores the first two
# seconds, then requires three consecutive notifications spanning at
# least two seconds.
DEFAULT_STARTUP_GATE = {
    "enabled": True,
    "stability_window_seconds": 8.0,
    "packets_required": 3,
    "min_rate_hz": 0.8,
    "min_observation_seconds": 2.0,
    "max_gap_events": 0,
    "gap_grace_seconds": 2.0,
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description="CORE 2 stream client built on NexusBLESdk."
    )

    parser.add_argument(
        "--port",
        default=DEFAULT_PORT,
        help=(
            "Gateway serial port path or alias. "
            "Examples: nexus_n3_gw, nordic_dev, auto, "
            "/dev/serial/by-id/..."
        ),
    )

    parser.add_argument(
        "--sensor-count",
        type=int,
        default=1,
    )

    parser.add_argument(
        "--scan-timeout-ms",
        type=int,
        default=5000,
    )

    parser.add_argument(
        "--connect-timeout-s",
        type=float,
        default=30.0,
    )

    parser.add_argument(
        "--read-timeout-s",
        type=float,
        default=5.0,
    )

    parser.add_argument(
        "--subscribe-timeout-s",
        type=float,
        default=10.0,
    )

    parser.add_argument(
        "--unsubscribe-timeout-s",
        type=float,
        default=5.0,
    )

    parser.add_argument(
        "--disconnect-timeout-s",
        type=float,
        default=5.0,
    )

    parser.add_argument(
        "--post-connect-settle-seconds",
        type=float,
        default=2.0,
    )

    parser.add_argument(
        "--stream-seconds",
        type=float,
        default=15.0,
        help=(
            "Total stream budget in seconds, including the startup "
            "stability period."
        ),
    )

    # ------------------------------------------------------------------
    # Startup gate
    # ------------------------------------------------------------------

    parser.add_argument(
        "--use-startup-gate",
        dest="use_startup_gate",
        action="store_true",
    )

    parser.add_argument(
        "--no-startup-gate",
        dest="use_startup_gate",
        action="store_false",
    )

    parser.set_defaults(
        use_startup_gate=DEFAULT_STARTUP_GATE["enabled"]
    )

    parser.add_argument(
        "--startup-stability-window-seconds",
        type=float,
        default=DEFAULT_STARTUP_GATE[
            "stability_window_seconds"
        ],
    )

    parser.add_argument(
        "--startup-packets-required",
        type=int,
        default=DEFAULT_STARTUP_GATE[
            "packets_required"
        ],
    )

    parser.add_argument(
        "--startup-min-rate-hz",
        type=float,
        default=DEFAULT_STARTUP_GATE[
            "min_rate_hz"
        ],
    )

    parser.add_argument(
        "--startup-min-observation-seconds",
        type=float,
        default=DEFAULT_STARTUP_GATE[
            "min_observation_seconds"
        ],
    )

    parser.add_argument(
        "--startup-max-gap-events",
        type=int,
        default=DEFAULT_STARTUP_GATE[
            "max_gap_events"
        ],
    )

    parser.add_argument(
        "--startup-gap-grace-seconds",
        type=float,
        default=DEFAULT_STARTUP_GATE[
            "gap_grace_seconds"
        ],
    )

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    parser.add_argument(
        "--write-to-file",
        action="store_true",
        help=(
            "Write parsed CORE 2 measurements to output-files/ "
            "in the current working directory."
        ),
    )

    parser.add_argument(
        "--write-raw",
        action="store_true",
        help=(
            "Write raw CORE 2 notification frames to a JSONL file "
            "in output-files/."
        ),
    )

    return parser


# ---------------------------------------------------------------------------
# Main stream workflow
# ---------------------------------------------------------------------------

def run(args) -> int:
    parsed_row_writer = None
    parsed_output_path = None

    raw_dump_file = None
    raw_output_path = None

    monitor = None
    client = None

    with open_gateway_serial(args.port) as ser:
        client = GatewayClient(
            ser,
            client_name="core2_stream_client",
        )

        core2 = Core2Client(client)

        stream_started = False
        stream_stopped = False
        disconnected = False

        # --------------------------------------------------------------
        # Output setup
        # --------------------------------------------------------------

        if args.write_to_file:
            parsed_output_path = build_output_path(
                "core2_stream",
                "csv",
            )

            parsed_row_writer = CsvRowWriter(
                parsed_output_path,
                [
                    "wall_time_s",
                    "gateway_timestamp_us",
                    "sensor_id",
                    "address",
                    "flags",
                    "core_temperature",
                    "skin_temperature",
                    "core_reserved",
                    "core_data_quality",
                    "heart_rate_state",
                    "heart_rate",
                    "heat_strain_index",
                ],
            )

            core2.set_parsed_row_writer(
                parsed_row_writer
            )

        if args.write_raw:
            raw_output_path = build_output_path(
                "core2_stream_raw",
                "jsonl",
            )

            raw_dump_file = open(
                raw_output_path,
                "w",
                encoding="utf-8",
            )

            core2.set_raw_dump_file(
                raw_dump_file
            )

        try:
            # ----------------------------------------------------------
            # Gateway handshake
            # ----------------------------------------------------------

            client.phase = "reset_session"
            client.reset_session()

            client.phase = "hello"
            client.hello()

            # ----------------------------------------------------------
            # Discovery
            # ----------------------------------------------------------

            client.phase = "scan"

            selected = core2.discover(
                args.sensor_count,
                args.scan_timeout_ms,
            )

            print(
                f"Selected addresses: {selected}"
            )

            if len(selected) < args.sensor_count:
                raise RuntimeError(
                    f"Requested {args.sensor_count} CORE 2 sensor(s), "
                    f"found {len(selected)}"
                )

            # ----------------------------------------------------------
            # Connection
            # ----------------------------------------------------------

            client.phase = "connect"

            connections = core2.connect(
                selected,
                timeout_s=args.connect_timeout_s,
            )

            # CORE placement is not inferred by the tooling. The sensor
            # address is therefore left unlabelled unless a higher-level
            # application provides placement information.
            labels_by_address = {
                connection.address: None
                for connection in connections
            }

            # ----------------------------------------------------------
            # Stream monitor
            # ----------------------------------------------------------

            startup_gate = StartupGateConfig(
                enabled=args.use_startup_gate,
                stability_window_seconds=(
                    args.startup_stability_window_seconds
                ),
                packets_required=(
                    args.startup_packets_required
                ),
                min_rate_hz=(
                    args.startup_min_rate_hz
                ),
                min_observation_seconds=(
                    args.startup_min_observation_seconds
                ),
                max_gap_events=(
                    args.startup_max_gap_events
                ),
                gap_grace_seconds=(
                    args.startup_gap_grace_seconds
                ),
            )

            monitor = Core2StreamMonitor(
                connections=connections,
                labels_by_address=labels_by_address,
                startup_gate=startup_gate,
                verbose=True,
            )

            print(
                "Startup gate config: "
                f"expected_notification_rate="
                f"{EXPECTED_NOTIFICATION_RATE_HZ:.2f}Hz "
                f"min_rate={args.startup_min_rate_hz:.2f}Hz "
                f"packets_required="
                f"{args.startup_packets_required} "
                f"min_observation="
                f"{args.startup_min_observation_seconds:.1f}s "
                f"window="
                f"{args.startup_stability_window_seconds:.1f}s"
            )

            # ----------------------------------------------------------
            # Post-connect settling
            # ----------------------------------------------------------

            if args.post_connect_settle_seconds > 0:
                client.phase = "post_connect_settle"

                print(
                    "All sensors connected. "
                    f"Waiting "
                    f"{args.post_connect_settle_seconds:.1f}s "
                    "for BLE links to settle."
                )

                time.sleep(
                    args.post_connect_settle_seconds
                )

            # ----------------------------------------------------------
            # CORE setup
            # ----------------------------------------------------------

            client.phase = "configure"

            core2.configure(
                read_timeout_s=args.read_timeout_s,
            )

            # ----------------------------------------------------------
            # Start stream
            # ----------------------------------------------------------

            client.phase = "start_streams"

            print(
                "Starting CORE 2 stream. "
                f"Expected notification rate: "
                f"{EXPECTED_NOTIFICATION_RATE_HZ:.2f}Hz. "
                f"Total stream budget: "
                f"{args.stream_seconds:.1f}s."
            )

            started_at = core2.start_streams(
                subscribe_timeout_s=(
                    args.subscribe_timeout_s
                ),
            )

            stream_started = True

            for address, command_time in (
                started_at.items()
            ):
                monitor.mark_stream_started(
                    address,
                    command_time,
                )

            monitor.announce_startup_state()

            # ----------------------------------------------------------
            # Main receive loop
            # ----------------------------------------------------------

            client.phase = "monitor"

            startup_deadline = (
                time.monotonic()
                + args.startup_stability_window_seconds
            )

            deadline = (
                time.monotonic()
                + args.stream_seconds
            )

            while time.monotonic() < deadline:

                # GenericStreamMonitor normally activates measurement
                # automatically as soon as the gate passes. This
                # deadline check handles the case where the configured
                # stability window expires without automatic activation.
                if (
                    args.use_startup_gate
                    and not monitor.measurement_active
                    and time.monotonic()
                    >= startup_deadline
                ):
                    stable, unstable = (
                        monitor.evaluate_startup_stability()
                    )

                    if not stable:
                        raise RuntimeError(
                            "Startup stability gate failed: "
                            + (
                                ", ".join(unstable)
                                if unstable
                                else "unknown startup instability"
                            )
                        )

                    monitor.activate_measurement()

                try:
                    item_type, item = client.read_item(
                        timeout_s=0.2
                    )

                except TimeoutError:
                    continue

                if item_type != "stream_frame":

                    if (
                        item.get("type")
                        == "sensor_disconnected"
                    ):
                        raise RuntimeError(
                            "Unexpected disconnect during stream: "
                            f"{item.get('address')} "
                            f"reason={item.get('reason')}"
                        )

                    continue

                wall_time = time.monotonic()

                # CORE-specific parsing and persistence.
                core2.handle_stream_frame(
                    item,
                    measurement_active=(
                        monitor.measurement_active
                    ),
                    wall_time=wall_time,
                )

                # Generic stream-health monitoring.
                monitor.handle_stream_frame(
                    item,
                    wall_time,
                )

            # ----------------------------------------------------------
            # Stop stream
            # ----------------------------------------------------------

            client.phase = "stop_streams"

            core2.stop_streams(
                unsubscribe_timeout_s=(
                    args.unsubscribe_timeout_s
                ),
            )

            stream_stopped = True

            # ----------------------------------------------------------
            # Drain any notifications already in transit
            # ----------------------------------------------------------

            client.phase = "post_stop_drain"

            monitor.drain_after_stop(
                client
            )

            # ----------------------------------------------------------
            # Gateway diagnostics
            # ----------------------------------------------------------

            try:
                client.phase = "get_status"

                client.get_status_snapshot(
                    timeout_s=10.0
                )

            except TimeoutError:
                pass

            # ----------------------------------------------------------
            # Disconnect
            # ----------------------------------------------------------

            client.phase = "disconnect"

            core2.disconnect_all(
                timeout_s=args.disconnect_timeout_s,
            )

            disconnected = True

        finally:
            # ----------------------------------------------------------
            # Best-effort cleanup
            # ----------------------------------------------------------

            if (
                stream_started
                and not stream_stopped
            ):
                try:
                    client.phase = "cleanup_stop_streams"

                    core2.stop_streams(
                        unsubscribe_timeout_s=(
                            args.unsubscribe_timeout_s
                        ),
                    )

                except Exception as exc:
                    print(
                        "STOP STREAM CLEANUP WARNING: "
                        f"{exc}"
                    )

            if (
                core2.connections
                and not disconnected
            ):
                try:
                    client.phase = "cleanup_disconnect"

                    core2.disconnect_all(
                        timeout_s=(
                            args.disconnect_timeout_s
                        ),
                    )

                except Exception as exc:
                    print(
                        "DISCONNECT CLEANUP WARNING: "
                        f"{exc}"
                    )

            client.phase = "idle"

            if parsed_row_writer is not None:
                parsed_row_writer.close()

            if raw_dump_file is not None:
                raw_dump_file.close()

    # ------------------------------------------------------------------
    # Final output
    # ------------------------------------------------------------------

    print("")

    if parsed_output_path is not None:
        print(
            f"Parsed output file: "
            f"{parsed_output_path}"
        )

    if raw_output_path is not None:
        print(
            f"Raw output file: "
            f"{raw_output_path}"
        )

    if monitor is not None and client is not None:
        for line in monitor.summary_lines(client):
            print(line)

    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = build_parser().parse_args()

    if args.sensor_count < 1:
        raise SystemExit(
            "--sensor-count must be at least 1"
        )

    if args.stream_seconds <= 0:
        raise SystemExit(
            "--stream-seconds must be greater than 0"
        )

    if (
        args.use_startup_gate
        and args.stream_seconds
        <= args.startup_stability_window_seconds
    ):
        raise SystemExit(
            "--stream-seconds must be greater than "
            "--startup-stability-window-seconds "
            "when the startup gate is enabled"
        )

    raise SystemExit(
        run(args)
    )


if __name__ == "__main__":
    main()