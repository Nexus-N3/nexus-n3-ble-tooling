

## this client allows users to mark locations to capture RF 
## button presses recieved close a mark and prompt the user to enter a new location
## events inbetween marks are ignored.

#!/usr/bin/env python3

from __future__ import annotations

import argparse
import queue
import threading
import time
from typing import Any, Callable

from MetaWear.profile import is_metawear_name
from MovellaDot.profile import MOVELLA_NAME
from Movesense.profile import is_movesense_match
from NexusN3Dot.profile import NEXUS_N3_DOT_NAME
from NexusBLESdk.client import GatewayClient
from NexusBLESdk.models import DiscoveredDevice
from NexusBLESdk.transport import DEFAULT_BAUD, DEFAULT_PORT, open_gateway_serial

Selector = Callable[[str], bool]


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
            f"  {target['address']}: {_format_live(target)}"
        )


def _print_final(stopped: dict) -> None:
    print(
        f"FINAL: state={stopped.get('state')} "
        f"target_count={stopped.get('target_count')} "
        f"elapsed_ms={stopped.get('elapsed_ms')}"
    )
    for target in stopped.get("targets", []):
        print(
            f"  {target['address']}: {_format_variation(target)} "
            f"observations_total={target.get('observations_total')} "
            f"last_seen_age_ms={target.get('last_seen_age_ms')} "
            f"best_score_elapsed_ms={target.get('best_score_elapsed_ms', 0)} "
            f"worst_score_elapsed_ms={target.get('worst_score_elapsed_ms', 0)}"
        )


def _print_segment(segment: dict[str, Any]) -> None:
    mark = segment["mark"]
    start_ms = segment.get("start_elapsed_ms")
    end_ms = segment.get("end_elapsed_ms")
    duration_ms = None
    if isinstance(start_ms, int) and isinstance(end_ms, int):
        duration_ms = max(0, end_ms - start_ms)

    print(
        f"SEGMENT: mark={mark!r} "
        f"close_reason={segment.get('close_reason')} "
        f"start_elapsed_ms={start_ms} "
        f"end_elapsed_ms={end_ms} "
        f"duration_ms={duration_ms}"
    )
    for target in segment.get("targets", []):
        print(f"  {target['address']}: {_format_live(target)}")


def _print_marked_final(segments: list[dict[str, Any]]) -> None:
    print(f"MARKED FINAL: segment_count={len(segments)}")
    for index, segment in enumerate(segments, start=1):
        print(f"MARK {index}:")
        _print_segment(segment)


def _start_prompt_thread(
    prompt_queue: "queue.Queue[str | None]",
    response_queue: "queue.Queue[str]",
) -> threading.Thread:
    def _worker() -> None:
        while True:
            prompt = prompt_queue.get()
            if prompt is None:
                return
            try:
                response = input(prompt)
            except EOFError:
                response = "stop"
            response_queue.put(response.strip())

    thread = threading.Thread(target=_worker, name="mark-input", daemon=True)
    thread.start()
    return thread


def _queue_mark_prompt(prompt_queue: "queue.Queue[str | None]") -> None:
    prompt_queue.put("Enter location mark or 'stop' to finish survey: ")


def _poll_mark_response(
    response_queue: "queue.Queue[str]",
) -> str | None:
    try:
        return response_queue.get_nowait()
    except queue.Empty:
        return None


def _consume_mark_response(response: str) -> tuple[str | None, bool]:
    value = response.strip()
    if not value:
        return None, False
    if value.lower() == "stop":
        return None, True
    return value, False


def _finalize_status_message(
    start_msg: dict[str, Any] | None,
    targets: list[dict[str, Any]],
    complete_msg: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if start_msg is None or complete_msg is None:
        return None

    expected_targets = int(start_msg.get("target_count", 0))
    if len(targets) < expected_targets:
        return None

    status = dict(start_msg)
    status["targets"] = list(targets)
    status["complete"] = complete_msg
    return status


def _close_segment(
    *,
    segments: list[dict[str, Any]],
    current_mark: str | None,
    segment_start_elapsed_ms: int | None,
    latest_status: dict[str, Any] | None,
    close_reason: str,
) -> None:
    if current_mark is None:
        return

    segment = {
        "mark": current_mark,
        "start_elapsed_ms": segment_start_elapsed_ms,
        "end_elapsed_ms": latest_status.get("elapsed_ms") if latest_status else None,
        "close_reason": close_reason,
        "targets": list(latest_status.get("targets", [])) if latest_status else [],
    }
    segments.append(segment)
    _print_segment(segment)

def main() -> None:
    args = _build_parser().parse_args()
    status_timeout_s = _rf_survey_request_timeout_s(args.window_ms)

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
            client_name="rf_survey_mixed_mark",
            verbose=True,
        )

        print("Sending hello...")
        hello = gateway.hello()
        print("Hello", hello)

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

        prompt_queue: queue.Queue[str | None] = queue.Queue()
        response_queue: queue.Queue[str] = queue.Queue()
        _start_prompt_thread(prompt_queue, response_queue)

        print("Enter the first mark before starting the RF survey.")
        _queue_mark_prompt(prompt_queue)

        first_mark: str | None = None
        while first_mark is None:
            response = response_queue.get()
            mark, should_stop = _consume_mark_response(response)
            if should_stop:
                print("Survey cancelled before start.")
                prompt_queue.put(None)
                return
            if mark is None:
                print("A mark is required to start the survey.")
                _queue_mark_prompt(prompt_queue)
                continue
            first_mark = mark

        print(f"Starting RF survey with {len(targets)} target(s)...")
        started = gateway.rf_survey_start(
            targets,
            window_ms=args.window_ms,
            duration_ms=args.duration_ms,
            timeout_s=status_timeout_s,
        )
        print("STARTED:", started)

        print("Waiting for pushed RF survey window status...")
        survey_deadline = time.monotonic() + (args.duration_ms / 1000.0)
        grace_deadline = survey_deadline + max(5.0, args.window_ms / 1000.0)
        current_mark = first_mark
        segment_start_elapsed_ms = 0
        segments: list[dict[str, Any]] = []
        latest_status: dict[str, Any] | None = None
        status_start: dict[str, Any] | None = None
        status_targets: list[dict[str, Any]] = []
        status_complete: dict[str, Any] | None = None
        awaiting_mark = False
        stop_requested = False

        print(f"MARK START: {current_mark!r}")
        while time.monotonic() < grace_deadline:
            response = _poll_mark_response(response_queue)
            if response is not None:
                next_mark, should_stop = _consume_mark_response(response)
                if should_stop:
                    stop_requested = True
                    break
                if next_mark is None:
                    if awaiting_mark:
                        print("Blank mark ignored. Enter another mark or 'stop'.")
                        _queue_mark_prompt(prompt_queue)
                    continue
                current_mark = next_mark
                segment_start_elapsed_ms = (
                    latest_status.get("elapsed_ms", 0) if latest_status else 0
                )
                awaiting_mark = False
                print(f"MARK START: {current_mark!r}")

            timeout_s = min(0.5, max(0.1, grace_deadline - time.monotonic()))
            try:
                msg = gateway.read_json(timeout_s=timeout_s)
            except TimeoutError:
                if time.monotonic() >= survey_deadline:
                    break
                continue

            msg_type = msg.get("type")
            if msg_type == "rf_survey_mark_button":
                print("RX: RF Survery Mark Button Pressed")
                if current_mark is None:
                    print("No active mark. Button press ignored.")
                    continue
                _close_segment(
                    segments=segments,
                    current_mark=current_mark,
                    segment_start_elapsed_ms=segment_start_elapsed_ms,
                    latest_status=latest_status,
                    close_reason="rf_survey_mark_button_pressed",
                )
                current_mark = None
                segment_start_elapsed_ms = None
                awaiting_mark = True
                print("Enter the next mark. RF status updates will be ignored until then.")
                _queue_mark_prompt(prompt_queue)
                continue

            if msg_type == "rf_survey_status":
                if msg.get("request_id") not in {None, ""}:
                    continue
                status_start = msg
                status_targets = []
                status_complete = None
                continue

            if msg_type == "rf_survey_target_status":
                if status_start is None:
                    continue
                status_targets.append(msg)
                continue

            if msg_type == "rf_survey_status_complete":
                if status_start is None:
                    continue
                status_complete = msg
                status = _finalize_status_message(
                    status_start,
                    status_targets,
                    status_complete,
                )
                if status is None:
                    continue
                latest_status = status
                status_start = None
                status_targets = []
                status_complete = None

                if current_mark is None:
                    continue

                print(f"MARK STATUS: {current_mark!r}")
                _print_window(status)

                if status.get("state") == "stopping":
                    break

                if (
                    status.get("active") is not True
                    and time.monotonic() < survey_deadline
                ):
                    raise RuntimeError(
                        "Expected active=true or state=stopping during survey, "
                        f"got: {status}"
                    )

                continue

        if stop_requested:
            print("Stop requested by user.")

        _close_segment(
            segments=segments,
            current_mark=current_mark,
            segment_start_elapsed_ms=segment_start_elapsed_ms,
            latest_status=latest_status,
            close_reason="stop" if stop_requested else "duration_complete",
        )
        prompt_queue.put(None)

        print("Stopping RF survey...")
        stopped = _request_stop(
            gateway,
            timeout_s=status_timeout_s,
        )
        if stopped is None:
            print("RF Survey stop did not return a response. Gateway may be stalled.")
            return

        print(
            f"Survey stopped: state={stopped.get('state')} "
            f"elapsed_ms={stopped.get('elapsed_ms')}"
        )
        _print_marked_final(segments)


if __name__ == "__main__":
    main()
