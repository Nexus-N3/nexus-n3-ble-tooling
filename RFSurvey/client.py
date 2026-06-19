#!/usr/bin/env python3

from __future__ import annotations

import argparse
import time
from typing import Any

from MovellaDot.client import MovellaDotClient
from MovellaDot.profile import MOVELLA_NAME

from NexusBLESdk.client import GatewayClient
from NexusBLESdk.transport import DEFAULT_BAUD, DEFAULT_PORT, open_gateway_serial


def _format_variation(target: dict) -> str:
    return (
        f"score={target.get('score', 0)} "
        f"quality={target.get('quality', 'missing')} "
        f"trend={target.get('trend', 'unknown')} "
        f"best_score={target.get('best_score', target.get('score', 0))} "
        f"worst_score={target.get('worst_score', target.get('score', 0))} "
        f"mean_score={target.get('mean_score', target.get('score', 0))} "
        f"score_sample_count={target.get('score_sample_count', 0)}"
    )


def _format_live(target: dict) -> str:
    return (
        f"score={target.get('score', 0)} "
        f"quality={target.get('quality', 'missing')} "
        f"trend={target.get('trend', 'unknown')} "
        f"rssi_avg={target.get('rssi_avg', 0)} "
        f"observations={target.get('observations', 0)} "
        f"last_seen_age_ms={target.get('last_seen_age_ms', 0)}"
    )


def _rf_survey_request_timeout_s(window_ms: int) -> float:
    return max(10.0, (window_ms / 1000.0) + 4.0)


def _window_wait_timeout_s(window_ms: int) -> float:
    return max(10.0, (window_ms / 1000.0) + 5.0)


def _wait_for_window_status(
    gateway: GatewayClient,
    *,
    timeout_s: float,
) -> dict[str, Any] | None:
    try:
        return gateway.wait_for_rf_survey_window_status(timeout_s=timeout_s)
    except TimeoutError as exc:
        print(f"RF SURVEY STATUS TIMEOUT: {exc}")
        return None


def _request_stop(
    gateway: GatewayClient,
    *,
    timeout_s: float,
) -> dict[str, Any] | None:
    try:
        return gateway.rf_survey_stop(timeout_s=timeout_s)
    except TimeoutError as exc:
        print(f"RF SURVEY STOP TIMEOUT: {exc}")
        return None


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
    status_timeout_s = _rf_survey_request_timeout_s(args.window_ms)
    window_wait_timeout_s = _window_wait_timeout_s(args.window_ms)

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
            timeout_s=status_timeout_s,
        )
        print("STARTED:", started)

        print("Waiting for pushed RF survey window status...")

        survey_deadline = time.monotonic() + (args.duration_ms / 1000.0)
        grace_deadline = survey_deadline + max(5.0, args.window_ms / 1000.0)
        window_index = 0

        while time.monotonic() < grace_deadline:
            status = _wait_for_window_status(
                gateway,
                timeout_s=window_wait_timeout_s,
            )
            if status is None:
                if time.monotonic() >= survey_deadline:
                    break
                continue
            window_index += 1
            print(f"STATUS {window_index}:", status)

            for target in status.get("targets", []):
                print(
                    "RF TARGET:",
                    target["address"],
                    _format_live(target),
                    "window_elapsed_ms=", status.get("window_elapsed_ms", 0),
                )

            if status.get("state") == "stopping":
                break

            if status.get("active") is not True and time.monotonic() < survey_deadline:
                raise RuntimeError(f"Expected active=true or state=stopping, got: {status}")

        print("Stopping RF survey...")
        stopped = _request_stop(
            gateway,
            timeout_s=status_timeout_s,
        )
        if stopped is None:
            print("RF Survey stop did not return a response. Gateway may be stalled.")
            return

        print("STOPPED:", stopped)

        for target in stopped.get("targets", []):
            print(
                "RF FINAL:",
                target["address"],
                _format_variation(target),
                "seen_total=", target.get("seen_total", False),
                "total_obs=", target.get("observations_total", 0),
                "last_seen_age_ms=", target.get("last_seen_age_ms", 0),
                "best_score_elapsed_ms=", target.get("best_score_elapsed_ms", 0),
                "worst_score_elapsed_ms=", target.get("worst_score_elapsed_ms", 0),
            )

        print("RF Survey smoke test passed.")


if __name__ == "__main__":
    main()
