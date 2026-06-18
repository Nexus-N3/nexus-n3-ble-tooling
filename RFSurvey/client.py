#!/usr/bin/env python3

from __future__ import annotations

import argparse
import time

from MovellaDot.client import MovellaDotClient
from MovellaDot.profile import MOVELLA_NAME

from NexusBLESdk.client import GatewayClient
from NexusBLESdk.transport import DEFAULT_BAUD, DEFAULT_PORT, open_gateway_serial


def main() -> None:
    parser = argparse.ArgumentParser(
        description="RF Survey smoke-test client for the Nexus BLE gateway."
    )

    parser.add_argument("--port", default=DEFAULT_PORT)
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)

    parser.add_argument(
        "--address",
        default=None,
        help="Optional target BLE address. If omitted, the client scans and selects one Movella DOT.",
    )

    parser.add_argument("--scan-timeout-ms", type=int, default=5000)
    parser.add_argument("--window-ms", type=int, default=5000)
    parser.add_argument("--duration-ms", type=int, default=60000)
    parser.add_argument("--no-reset", action="store_true")

    args = parser.parse_args()

    with open_gateway_serial(args.port, args.baud) as ser:
        gateway = GatewayClient(
            ser,
            client_name="rf_survey",
            verbose=True,
        )

        print("Sending hello...")
        gateway.hello()

        if not args.no_reset:
            print("Resetting gateway session...")
            gateway.reset_session()

        if args.address:
            selected = [args.address]
        else:
            print(
                f"Scanning for 1 Movella DOT "
                f"name={MOVELLA_NAME!r} for {args.scan_timeout_ms} ms..."
            )
            movella = MovellaDotClient(gateway)
            selected = movella.discover(
                sensor_count=1,
                scan_timeout_ms=args.scan_timeout_ms,
            )

        target_address = selected[0]

        print(f"Using RF Survey target address: {target_address}")

        print("Starting RF survey...")
        started = gateway.rf_survey_start(
            [target_address],
            window_ms=args.window_ms,
            duration_ms=args.duration_ms,
        )
        print("STARTED:", started)

        time.sleep(1.0)

        print("Polling RF survey status...")

        for i in range(8):
            status = gateway.rf_survey_status()
            print(f"STATUS {i + 1}:", status)

            if status.get("active") is not True:
                raise RuntimeError(f"Expected active=true, got: {status}")

            for target in status.get("targets", []):
                print(
                    "RF TARGET:",
                    target["address"],
                    "window_obs=", target["observations"],
                    "total_obs=", target.get("observations_total"),
                    "rssi_avg=", target["rssi_avg"],
                    "rssi_avg_total=", target.get("rssi_avg_total"),
                    "last_seen_age_ms=", target["last_seen_age_ms"],
                    "window_elapsed_ms=", status.get("window_elapsed_ms"),
                )

            time.sleep(1.0)

        print("Stopping RF survey...")
        stopped = gateway.rf_survey_stop()
        print("STOPPED:", stopped)

        for target in stopped.get("targets", []):
            print(
                "RF FINAL:",
                target["address"],
                "seen_total=", target.get("seen_total"),
                "total_obs=", target.get("observations_total"),
                "rssi_avg_total=", target.get("rssi_avg_total"),
                "last_seen_age_ms=", target.get("last_seen_age_ms"),
                "score=", target.get("score"),
                "quality=", target.get("quality"),
            )

        print("RF Survey smoke test passed.")


if __name__ == "__main__":
    main()
