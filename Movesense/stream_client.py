#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from NexusBLESdk import (
    DEFAULT_PORT,
    GatewayClient,
    GenericStreamMonitor,
    StartupGateConfig,
    open_gateway_serial,
)

from Movesense.client import MovesenseClient
from Movesense.monitor import MovesenseMonitorAdapter
from Movesense.profile import (
    DEFAULT_LOCATIONS,
    DEFAULT_STARTUP_GATE,
    MOVESENSE_ECG_SAMPLES_PER_PACKET,
    MOVESENSE_SAMPLING_RATES_HZ,
    parse_ecg_packet_timestamp_us,
)


def build_parser():
    parser = argparse.ArgumentParser(description="Movesense sample client built on NexusBLESdk.")
    parser.add_argument("--port", default=DEFAULT_PORT)
    parser.add_argument("--sensor-count", type=int, default=1)
    parser.add_argument("--scan-timeout-ms", type=int, default=5000)
    parser.add_argument("--connect-timeout-s", type=float, default=30.0)
    parser.add_argument("--subscribe-timeout-s", type=float, default=10.0)
    parser.add_argument("--write-timeout-s", type=float, default=10.0)
    parser.add_argument("--disconnect-timeout-s", type=float, default=5.0)
    parser.add_argument("--post-connect-settle-seconds", type=float, default=2.0)
    parser.add_argument("--stream-seconds", type=float, default=10.0)
    parser.add_argument(
        "--sampling-rate-hz",
        type=int,
        choices=sorted(MOVESENSE_SAMPLING_RATES_HZ),
        default=MOVESENSE_SAMPLING_RATES_HZ[0],
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
        default=DEFAULT_STARTUP_GATE["min_rate_hz"],
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
        "--dump-raw-file",
        help="Optional JSONL file for raw Movesense packet capture.",
    )
    return parser


def run(args) -> int:
    with open_gateway_serial(args.port) as ser:
        client = GatewayClient(ser, client_name="movesense_stream_client")
        movesense = MovesenseClient(client)
        raw_dump_file = None
        if args.dump_raw_file:
            raw_dump_file = open(args.dump_raw_file, "w", encoding="utf-8")
            movesense.set_raw_dump_file(raw_dump_file)
        client.phase = "reset_session"
        client.reset_session()
        client.phase = "hello"
        client.hello()

        client.phase = "scan"
        if(args.sensor_count == 1):
            selected = movesense.discover(args.sensor_count, args.scan_timeout_ms)
        else:
            print(f"Only 1 Movesense sensor can be used at a time")
            return 1
        print(f"Selected addresses: {selected}")

        client.phase = "connect"
        connections = movesense.connect(selected, timeout_s=args.connect_timeout_s)
        labels_by_address = {
            connection.address: DEFAULT_LOCATIONS[index] if index < len(DEFAULT_LOCATIONS) else None
            for index, connection in enumerate(connections)
        }
        packet_rate_hz = args.sampling_rate_hz / MOVESENSE_ECG_SAMPLES_PER_PACKET
        base_monitor = GenericStreamMonitor(
            connections=connections,
            labels_by_address=labels_by_address,
            expected_rate_hz=int(packet_rate_hz) if packet_rate_hz.is_integer() else packet_rate_hz,
            timestamp_parser=parse_ecg_packet_timestamp_us,
            startup_gate=StartupGateConfig(
                enabled=args.use_startup_gate,
                stability_window_seconds=args.startup_stability_window_seconds,
                packets_required=args.startup_packets_required,
                min_rate_hz=args.startup_min_rate_hz,
                min_observation_seconds=args.startup_min_observation_seconds,
                max_gap_events=args.startup_max_gap_events,
                gap_grace_seconds=args.startup_gap_grace_seconds,
            ),
            verbose=True,
        )
        monitor = MovesenseMonitorAdapter(base_monitor)

        if args.post_connect_settle_seconds > 0:
            client.phase = "post_connect_settle"
            print(
                "All sensors connected. "
                f"Waiting {args.post_connect_settle_seconds:.1f}s for BLE links/params to settle."
            )
            time.sleep(args.post_connect_settle_seconds)

        try:
            client.phase = "configure"
            movesense.configure(
                sampling_rate_hz=args.sampling_rate_hz,
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
            print(f"Starting stream. Total stream budget: {args.stream_seconds}s.")
            started_at = movesense.start_streams(
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
                if args.use_startup_gate and not monitor.measurement_active and time.monotonic() >= startup_deadline:
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
                            f"Unexpected disconnect during stream: {item.get('address')} reason={item.get('reason')}"
                        )
                    continue

                movesense.handle_stream_frame(item, monitor, time.monotonic())

            client.phase = "stop_streams"
            movesense.stop_streams(
                write_timeout_s=args.write_timeout_s,
                without_response=args.without_response,
            )

            client.phase = "post_stop_drain"
            monitor.drain_after_stop(
                client,
                frame_handler=lambda frame, wall_time: movesense.handle_stream_frame(frame, monitor, wall_time),
            )

            try:
                client.phase = "get_status"
                client.get_status_snapshot(timeout_s=10.0)
            except TimeoutError:
                pass

            client.phase = "disconnect"
            movesense.disconnect_all(timeout_s=args.disconnect_timeout_s)
        finally:
            client.phase = "idle"
            if raw_dump_file is not None:
                raw_dump_file.close()

    print("")
    for line in monitor.summary_lines(client):
        print(line)

    return 0


def main():
    args = build_parser().parse_args()
    if args.sensor_count < 1:
        raise SystemExit("--sensor-count must be at least 1")
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
