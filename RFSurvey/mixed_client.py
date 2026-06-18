#!/usr/bin/env python3

from __future__ import annotations

import argparse
import time
from typing import Callable

from MetaWear.profile import is_metawear_name
from MovellaDot.profile import MOVELLA_NAME
from Movesense.profile import is_movesense_match
from NexusBLESdk.client import GatewayClient
from NexusBLESdk.models import DiscoveredDevice
from NexusBLESdk.transport import DEFAULT_BAUD, DEFAULT_PORT, open_gateway_serial
from NexusN3Dot.profile import NEXUS_N3_DOT_NAME


Selector = Callable[[str], bool]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Mixed-sensor RF Survey client. Select counts per supported sensor type, "
            "run the survey, print window score/quality, then print the final report."
        )
    )
    parser.add_argument("--port", default=DEFAULT_PORT)
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--scan-timeout-ms", type=int, default=5000)
    parser.add_argument("--window-ms", type=int, default=5000)
    parser.add_argument("--duration-ms", type=int, default=30000)
    parser.add_argument("--poll-interval-s", type=float, default=1.0)
    parser.add_argument("--no-reset", action="store_true")

    parser.add_argument("--movella-count", type=int, default=0)
    parser.add_argument("--movesense-count", type=int, default=0)
    parser.add_argument("--metawear-count", type=int, default=0)
    parser.add_argument("--nexus-n3-dot-count", type=int, default=0)
    return parser


def _select_matches(
    devices: list[DiscoveredDevice],
    *,
    count: int,
    label: str,
    predicate: Selector,
    used_addresses: set[str],
) -> list[DiscoveredDevice]:
    if count <= 0:
        return []

    matches = [
        device
        for device in devices
        if device.address not in used_addresses and predicate(device.name or "")
    ]

    if len(matches) < count:
        raise RuntimeError(
            f"Requested {count} {label} sensor(s), found {len(matches)}"
        )

    chosen = matches[:count]
    used_addresses.update(device.address for device in chosen)
    return chosen


def _print_selection(label: str, devices: list[DiscoveredDevice]) -> None:
    for device in devices:
        print(
            f"SELECTED {label}: address={device.address} "
            f"name={device.name!r} rssi={device.rssi}"
        )


def _print_window(status: dict) -> None:
    elapsed_ms = status.get("elapsed_ms")
    window_elapsed_ms = status.get("window_elapsed_ms")
    print(
        f"WINDOW: elapsed_ms={elapsed_ms} "
        f"window_elapsed_ms={window_elapsed_ms}"
    )
    for target in status.get("targets", []):
        print(
            f"  {target['address']}: score={target.get('score')} "
            f"quality={target.get('quality')}"
        )


def _print_final(stopped: dict) -> None:
    print(
        f"FINAL: state={stopped.get('state')} "
        f"target_count={stopped.get('target_count')} "
        f"elapsed_ms={stopped.get('elapsed_ms')}"
    )
    for target in stopped.get("targets", []):
        print(
            f"  {target['address']}: score={target.get('score')} "
            f"quality={target.get('quality')} "
            f"observations_total={target.get('observations_total')} "
            f"last_seen_age_ms={target.get('last_seen_age_ms')}"
        )


def main() -> None:
    args = _build_parser().parse_args()

    requested_total = (
        args.movella_count
        + args.movesense_count
        + args.metawear_count
        + args.nexus_n3_dot_count
    )
    if requested_total <= 0:
        raise RuntimeError("Select at least one sensor with a --*-count argument.")

    with open_gateway_serial(args.port, args.baud) as ser:
        gateway = GatewayClient(
            ser,
            client_name="rf_survey_mixed",
            verbose=True,
        )

        print("Sending hello...")
        gateway.hello()

        if not args.no_reset:
            print("Resetting gateway session...")
            gateway.reset_session()

        print(f"Scanning for mixed sensor set for {args.scan_timeout_ms} ms...")
        devices = gateway.scan(args.scan_timeout_ms)

        used_addresses: set[str] = set()
        selected: list[DiscoveredDevice] = []

        movella = _select_matches(
            devices,
            count=args.movella_count,
            label="Movella DOT",
            predicate=lambda name: name == MOVELLA_NAME,
            used_addresses=used_addresses,
        )
        selected.extend(movella)
        _print_selection("Movella DOT", movella)

        movesense = _select_matches(
            devices,
            count=args.movesense_count,
            label="Movesense",
            predicate=is_movesense_match,
            used_addresses=used_addresses,
        )
        selected.extend(movesense)
        _print_selection("Movesense", movesense)

        metawear = _select_matches(
            devices,
            count=args.metawear_count,
            label="MetaWear",
            predicate=is_metawear_name,
            used_addresses=used_addresses,
        )
        selected.extend(metawear)
        _print_selection("MetaWear", metawear)

        nexus_n3_dot = _select_matches(
            devices,
            count=args.nexus_n3_dot_count,
            label="Nexus N3 Dot",
            predicate=lambda name: name == NEXUS_N3_DOT_NAME,
            used_addresses=used_addresses,
        )
        selected.extend(nexus_n3_dot)
        _print_selection("Nexus N3 Dot", nexus_n3_dot)

        targets = [device.address for device in selected]
        print(f"Starting RF survey with {len(targets)} target(s)...")
        started = gateway.rf_survey_start(
            targets,
            window_ms=args.window_ms,
            duration_ms=args.duration_ms,
        )
        print("STARTED:", started)

        poll_deadline = time.monotonic() + (args.duration_ms / 1000.0)
        stop_margin_s = max(0.25, args.poll_interval_s)
        while time.monotonic() + stop_margin_s < poll_deadline:
            time.sleep(max(0.1, args.poll_interval_s))
            status = gateway.rf_survey_status()
            _print_window(status)
            if status.get("active") is not True:
                break

        final_status = gateway.rf_survey_status()
        _print_window(final_status)

        if final_status.get("active") is not True:
            raise RuntimeError(
                "RF survey became inactive before explicit stop; "
                "increase duration_ms or lower poll_interval_s."
            )

        print("Stopping RF survey...")
        stopped = gateway.rf_survey_stop()
        _print_final(stopped)


if __name__ == "__main__":
    main()
