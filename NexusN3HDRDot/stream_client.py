#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from NexusBLESdk import (
    CsvRowWriter,
    DEFAULT_PORT,
    GatewayClient,
    GenericStreamMonitor,
    StartupGateConfig,
    build_output_path,
    open_gateway_serial,
)

from NexusN3HDRDot.client import NexusN3HDRDotClient
from NexusN3HDRDot.profile import (
    DEFAULT_LOCATIONS,
    DEFAULT_STARTUP_GATE,
    NEXUS_N3_HDR_DOT_STREAM_VALUES_PER_SAMPLE,
    NEXUS_N3_HDR_DOT_TIMESTAMP_BYTES,
    parse_sensor_timestamp,
)

IMU_OUTPUT_RATE_HZ = 2048
BLE_MAX_PAYLOAD_BYTES = 200


def samples_per_notification_for_mode(stream_mode: str) -> int:
    mode = stream_mode.upper()
    values_per_sample = NEXUS_N3_HDR_DOT_STREAM_VALUES_PER_SAMPLE[mode]
    bytes_per_sample = values_per_sample * 2
    sample_payload_bytes = BLE_MAX_PAYLOAD_BYTES - NEXUS_N3_HDR_DOT_TIMESTAMP_BYTES

    samples_per_notification = sample_payload_bytes // bytes_per_sample

    if samples_per_notification <= 0:
        raise ValueError(f"Invalid samples_per_notification for stream mode {mode}")

    return samples_per_notification


def expected_notification_rate_hz(stream_mode: str) -> float:
    samples_per_notification = samples_per_notification_for_mode(stream_mode)
    return IMU_OUTPUT_RATE_HZ / samples_per_notification


def build_parser():
    parser = argparse.ArgumentParser(description="Nexus N3 HDR Dot stream client built on NexusBLESdk.")
    parser.add_argument(
        "--port",
        default=DEFAULT_PORT,
        help=(
            "Gateway serial port path or alias. "
            "Examples: nexus_n3_gw, nordic_dev, auto, "
            "/dev/serial/by-id/usb-ZEPHYR_IFMCU_CMSIS-DAP_..."
        ),
    )
    parser.add_argument("--sensor-count", type=int, default=1)
    parser.add_argument("--scan-timeout-ms", type=int, default=5000)
    parser.add_argument("--connect-timeout-s", type=float, default=30.0)
    parser.add_argument("--subscribe-timeout-s", type=float, default=10.0)
    parser.add_argument("--write-timeout-s", type=float, default=10.0)
    parser.add_argument("--disconnect-timeout-s", type=float, default=5.0)
    parser.add_argument("--post-connect-settle-seconds", type=float, default=2.0)
    parser.add_argument("--stream-seconds", type=float, default=10.0)

    parser.add_argument(
        "--stream-mode",
        choices=["MAG", "X", "Y", "Z", "XYZ", "ALL"],
        default="MAG",
        help="HDR Dot stream mode. MAG/X/Y/Z are single-value streams; XYZ and ALL are higher bandwidth.",
    )

    parser.add_argument("--use-startup-gate", dest="use_startup_gate", action="store_true")
    parser.add_argument("--no-startup-gate", dest="use_startup_gate", action="store_false")
    parser.set_defaults(use_startup_gate=DEFAULT_STARTUP_GATE["enabled"])

    parser.add_argument(
        "--startup-stability-window-seconds",
        type=float,
        default=DEFAULT_STARTUP_GATE["stability_window_seconds"],
    )
    parser.add_argument(
        "--startup-packets-required",
        type=int,
        default=DEFAULT_STARTUP_GATE["packets_required"],
    )
    parser.add_argument(
        "--startup-min-rate-hz",
        type=float,
        default=None,
        help="Minimum notification rate for startup gate. Defaults to a mode-aware value.",
    )
    parser.add_argument(
        "--startup-min-observation-seconds",
        type=float,
        default=DEFAULT_STARTUP_GATE["min_observation_seconds"],
    )
    parser.add_argument(
        "--startup-max-gap-events",
        type=int,
        default=DEFAULT_STARTUP_GATE["max_gap_events"],
    )
    parser.add_argument(
        "--startup-gap-grace-seconds",
        type=float,
        default=DEFAULT_STARTUP_GATE["gap_grace_seconds"],
    )
    parser.add_argument("--without-response", action="store_true")
    parser.add_argument(
        "--write-to-file",
        action="store_true",
        help="Write parsed Nexus N3 HDR Dot stream values to output-files/ in the current working directory.",
    )
    return parser


def run(args) -> int:
    stream_mode = args.stream_mode.upper()
    expected_rate_hz = expected_notification_rate_hz(stream_mode)

    startup_min_rate_hz = args.startup_min_rate_hz
    if startup_min_rate_hz is None:
        startup_min_rate_hz = expected_rate_hz * 0.85

    startup_packets_required = args.startup_packets_required
    if startup_packets_required == DEFAULT_STARTUP_GATE["packets_required"]:
        startup_packets_required = max(
            1,
            int(startup_min_rate_hz * args.startup_min_observation_seconds),
        )

    device_status_by_address: dict[str, dict] = {}

    with open_gateway_serial(args.port) as ser:
        client = GatewayClient(ser, client_name="nexus_n3_hdr_stream_client")
        nexus_n3_hdr = NexusN3HDRDotClient(client)
        parsed_row_writer = None
        parsed_output_path = None

        if args.write_to_file:
            parsed_output_path = build_output_path("nexus_n3_hdr_stream", "csv")
            parsed_row_writer = CsvRowWriter(
                parsed_output_path,
                [
                    "address",
                    "sensor_id",
                    "gateway_timestamp_us",
                    "stream_mode",
                    "sample_index",
                    "samples_in_notification",
                    "payload_bytes",
                    "timestamp_us",
                    "accel_x_mg",
                    "accel_y_mg",
                    "accel_z_mg",
                    "magnitude_mg",
                ],
            )
            nexus_n3_hdr.set_parsed_row_writer(parsed_row_writer)

        client.phase = "reset_session"
        client.reset_session()

        client.phase = "hello"
        client.hello()

        client.phase = "scan"
        selected = nexus_n3_hdr.discover(args.sensor_count, args.scan_timeout_ms)
        print(f"Selected addresses: {selected}")

        client.phase = "connect"
        connections = nexus_n3_hdr.connect(selected, timeout_s=args.connect_timeout_s)

        labels_by_address = {
            connection.address: DEFAULT_LOCATIONS[index] if index < len(DEFAULT_LOCATIONS) else None
            for index, connection in enumerate(connections)
        }

        print(
            "Startup gate config: "
            f"expected_notification_rate={expected_rate_hz:.2f}Hz "
            f"min_rate={startup_min_rate_hz:.2f}Hz "
            f"packets_required={startup_packets_required} "
            f"min_observation={args.startup_min_observation_seconds:.1f}s "
            f"window={args.startup_stability_window_seconds:.1f}s"
        )

        monitor = GenericStreamMonitor(
            connections=connections,
            labels_by_address=labels_by_address,
            expected_rate_hz=expected_rate_hz,
            timestamp_parser=parse_sensor_timestamp,
            startup_gate=StartupGateConfig(
                enabled=args.use_startup_gate,
                stability_window_seconds=args.startup_stability_window_seconds,
                packets_required=startup_packets_required,
                min_rate_hz=startup_min_rate_hz,
                min_observation_seconds=args.startup_min_observation_seconds,
                max_gap_events=args.startup_max_gap_events,
                gap_grace_seconds=args.startup_gap_grace_seconds,
            ),
            verbose=True,
        )

        if args.post_connect_settle_seconds > 0:
            client.phase = "post_connect_settle"
            print(
                "All sensors connected. "
                f"Waiting {args.post_connect_settle_seconds:.1f}s for BLE links/params to settle."
            )
            time.sleep(args.post_connect_settle_seconds)

        try:
            client.phase = "configure"
            nexus_n3_hdr.configure(
                stream_mode=stream_mode,
                subscribe_timeout_s=args.subscribe_timeout_s,
                write_timeout_s=args.write_timeout_s,
                without_response=args.without_response,
            )

            if args.post_connect_settle_seconds > 0:
                client.phase = "post_config_settle"
                print(
                    "All sensors configured. "
                    f"Waiting {args.post_connect_settle_seconds:.1f}s before stream start."
                )
                time.sleep(args.post_connect_settle_seconds)

            client.phase = "start_streams"
            print(
                f"Starting stream. "
                f"mode={stream_mode} "
                f"expected_notification_rate={expected_rate_hz:.2f}Hz "
                f"total stream budget: {args.stream_seconds}s."
            )

            started_at = nexus_n3_hdr.start_streams(
                write_timeout_s=args.write_timeout_s,
                without_response=args.without_response,
            )

            for address, command_time in started_at.items():
                monitor.mark_stream_started(address, command_time)

            monitor.announce_startup_state()

            client.phase = "monitor"
            startup_deadline = time.monotonic() + args.startup_stability_window_seconds
            deadline = time.monotonic() + args.stream_seconds

            while time.monotonic() < deadline:
                if (
                    args.use_startup_gate
                    and not monitor.measurement_active
                    and time.monotonic() >= startup_deadline
                ):
                    stable, unstable = monitor.evaluate_startup_stability()
                    if not stable:
                        raise RuntimeError(
                            "Startup stability gate failed: "
                            + (", ".join(unstable) if unstable else "unknown startup instability")
                        )
                    monitor.activate_measurement()

                try:
                    item_type, item = client.read_item(timeout_s=0.2)
                except TimeoutError:
                    continue

                if item_type != "stream_frame":
                    if item.get("type") == "sensor_disconnected":
                        raise RuntimeError(
                            f"Unexpected disconnect during stream: "
                            f"{item.get('address')} reason={item.get('reason')}"
                        )
                    continue

                nexus_n3_hdr.handle_stream_frame(
                    item,
                    measurement_active=monitor.measurement_active,
                )

                monitor.handle_stream_frame(item, time.monotonic())

            client.phase = "stop_streams"
            nexus_n3_hdr.stop_streams(
                write_timeout_s=args.write_timeout_s,
                without_response=args.without_response,
            )

            client.phase = "post_stop_drain"
            monitor.drain_after_stop(client)

            try:
                client.phase = "read_device_status"
                device_status_by_address = nexus_n3_hdr.read_device_status_all(timeout_s=5.0)
            except (TimeoutError, RuntimeError, ValueError) as exc:
                print(f"DEVICE STATUS WARNING: {exc}")

            try:
                client.phase = "get_status"
                client.get_status_snapshot(timeout_s=10.0)
            except TimeoutError:
                pass

            client.phase = "disconnect"
            nexus_n3_hdr.disconnect_all(timeout_s=args.disconnect_timeout_s)

        finally:
            client.phase = "idle"
            if parsed_row_writer is not None:
                parsed_row_writer.close()

    print("")

    if parsed_output_path is not None:
        print(f"Parsed output file: {parsed_output_path}")

    for line in monitor.summary_lines(client):
        print(line)

    for address, status in sorted(device_status_by_address.items()):
        print(
            "Device status "
            f"address={address} "
            f"running={status.get('running')} "
            f"stream_mode={status.get('stream_mode_name')} "
            f"output_rate_hz={status.get('output_rate_hz')} "
            f"max_payload_bytes={status.get('max_payload_bytes')} "
            f"samples_per_notification={status.get('samples_per_notification')} "
            f"packets_sent={status.get('packets_sent')} "
            f"packets_dropped={status.get('packets_dropped')}"
        )

    return 0


def main():
    args = build_parser().parse_args()

    if args.sensor_count < 1:
        raise SystemExit("--sensor-count must be at least 1")

    raise SystemExit(run(args))


if __name__ == "__main__":
    main()