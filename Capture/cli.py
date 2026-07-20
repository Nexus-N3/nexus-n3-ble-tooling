#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import contextmanager
import sys
from pathlib import Path

try:
    import termios
except ImportError:  # pragma: no cover
    termios = None

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from NexusBLESdk import DEFAULT_PORT

from Capture.adapters import SENSOR_SPECS, get_sensor_spec
from Capture.workflow import CaptureCancelled, CaptureConfig, run_capture_session


def _sensor_choices() -> list[str]:
    return [
        "movelladot",
        "nexusn3dot",
        "movesense",
        "metawear",
    ]


@contextmanager
def suppress_ctrl_c_echo():
    if termios is None or not sys.stdin.isatty():
        yield
        return

    try:
        fd = sys.stdin.fileno()
        original = termios.tcgetattr(fd)
    except (termios.error, ValueError):
        yield
        return

    if not hasattr(termios, "ECHOCTL"):
        yield
        return

    updated = original[:]
    updated[3] &= ~termios.ECHOCTL
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, updated)
        yield
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, original)
        except termios.error:
            pass


def prompt_text(prompt: str) -> str:
    try:
        with suppress_ctrl_c_echo():
            return input(prompt)
    except KeyboardInterrupt as exc:
        sys.stdout.write("\n")
        sys.stdout.flush()
        raise CaptureCancelled("Capture cancelled by operator.") from exc


def build_parser():
    parser = argparse.ArgumentParser(description="Interactive capture client built on nexus-n3-ble-tooling.")
    parser.add_argument("--sensor-type", choices=_sensor_choices())
    parser.add_argument("--sensor-count", type=int)
    parser.add_argument("--tag")
    parser.add_argument("--location", action="append", default=[])
    parser.add_argument("--identify", action="store_true")
    parser.add_argument(
        "--port",
        default=DEFAULT_PORT,
        help=(
            "Gateway serial port path or alias. "
            "Examples: nexus_n3_gw, nordic_dev, auto, "
            "/dev/serial/by-id/usb-ZEPHYR_IFMCU_CMSIS-DAP_..."
        ),
    )
    parser.add_argument("--scan-timeout-ms", type=int, default=5000)
    parser.add_argument("--connect-timeout-s", type=float, default=30.0)
    parser.add_argument("--subscribe-timeout-s", type=float, default=10.0)
    parser.add_argument("--write-timeout-s", type=float, default=10.0)
    parser.add_argument("--disconnect-timeout-s", type=float, default=5.0)
    parser.add_argument("--post-connect-settle-seconds", type=float, default=2.0)
    parser.add_argument("--read-timeout-s", type=float, default=10.0)
    parser.add_argument("--sampling-rate-hz", type=int)
    parser.add_argument("--duration-seconds", type=float)
    parser.add_argument("--use-startup-gate", dest="use_startup_gate", action="store_true")
    parser.add_argument("--no-startup-gate", dest="use_startup_gate", action="store_false")
    parser.set_defaults(use_startup_gate=None)
    parser.add_argument("--startup-stability-window-seconds", type=float)
    parser.add_argument("--startup-packets-required", type=int)
    parser.add_argument("--startup-min-rate-hz", type=float)
    parser.add_argument("--startup-min-observation-seconds", type=float)
    parser.add_argument("--startup-max-gap-events", type=int)
    parser.add_argument("--startup-gap-grace-seconds", type=float)
    parser.add_argument("--without-response", action="store_true")
    return parser


def prompt_choice(prompt: str, options: list[str]) -> str:
    for index, option in enumerate(options, start=1):
        print(f"  {index}. {option}")
    while True:
        raw = prompt_text(prompt).strip()
        if raw.lower() in {"q", "quit", "exit", "cancel"}:
            raise CaptureCancelled("Capture cancelled by operator.")
        try:
            choice = int(raw)
        except ValueError:
            print("Enter a valid number.")
            continue
        if 1 <= choice <= len(options):
            return options[choice - 1]
        print("Choice out of range.")


def prompt_bool(prompt: str, *, default: bool = False) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    raw = prompt_text(prompt + suffix).strip().lower()
    if raw in {"q", "quit", "exit", "cancel"}:
        raise CaptureCancelled("Capture cancelled by operator.")
    if not raw:
        return default
    return raw in {"y", "yes"}


def resolve_sensor_type(sensor_type: str | None) -> str:
    if sensor_type:
        return sensor_type
    print("Choose the sensor family to capture:")
    return prompt_choice("Sensor type: ", _sensor_choices())


def resolve_sensor_count(sensor_count: int | None, spec) -> int:
    if sensor_count is not None:
        return sensor_count
    while True:
        raw = prompt_text("How many sensors should be used? ").strip()
        if raw.lower() in {"q", "quit", "exit", "cancel"}:
            raise CaptureCancelled("Capture cancelled by operator.")
        try:
            count = int(raw)
        except ValueError:
            print("Enter a valid integer.")
            continue
        if count < 1:
            print("Sensor count must be at least 1.")
            continue
        if spec.max_sensor_count is not None and count > spec.max_sensor_count:
            print(f"{spec.display_name} supports at most {spec.max_sensor_count} sensor(s).")
            continue
        return count


def resolve_tag(tag: str | None) -> str:
    if tag:
        return tag
    while True:
        value = prompt_text("Capture tag: ").strip()
        if value.lower() in {"q", "quit", "exit", "cancel"}:
            raise CaptureCancelled("Capture cancelled by operator.")
        if value:
            return value
        print("Capture tag cannot be empty.")


def resolve_sampling_rate(sampling_rate_hz: int | None, spec) -> int:
    if sampling_rate_hz is not None:
        return sampling_rate_hz
    if len(spec.sampling_rates_hz) == 1:
        return spec.default_sampling_rate_hz
    print(f"Choose sampling rate for {spec.display_name}:")
    selected = prompt_choice(
        "Sampling rate: ",
        [str(value) for value in spec.sampling_rates_hz],
    )
    return int(selected)


def maybe_enable_identify(initial_value: bool, spec) -> bool:
    if initial_value:
        return True
    if not spec.supports_identify:
        return False
    return prompt_bool("Use guided identify-and-assign placement?", default=False)


def build_config(args) -> CaptureConfig:
    sensor_type = resolve_sensor_type(args.sensor_type)
    spec = get_sensor_spec(sensor_type)
    return CaptureConfig(
        sensor_type=sensor_type,
        sensor_count=resolve_sensor_count(args.sensor_count, spec),
        tag=resolve_tag(args.tag),
        locations=list(args.location or []),
        identify=maybe_enable_identify(args.identify, spec),
        port=args.port,
        scan_timeout_ms=args.scan_timeout_ms,
        connect_timeout_s=args.connect_timeout_s,
        subscribe_timeout_s=args.subscribe_timeout_s,
        write_timeout_s=args.write_timeout_s,
        disconnect_timeout_s=args.disconnect_timeout_s,
        post_connect_settle_seconds=args.post_connect_settle_seconds,
        read_timeout_s=args.read_timeout_s,
        startup_stability_window_seconds=args.startup_stability_window_seconds,
        startup_packets_required=args.startup_packets_required,
        startup_min_rate_hz=args.startup_min_rate_hz,
        startup_min_observation_seconds=args.startup_min_observation_seconds,
        startup_max_gap_events=args.startup_max_gap_events,
        startup_gap_grace_seconds=args.startup_gap_grace_seconds,
        use_startup_gate=args.use_startup_gate,
        without_response=args.without_response,
        sampling_rate_hz=resolve_sampling_rate(args.sampling_rate_hz, spec),
        duration_seconds=args.duration_seconds,
    )


def main():
    args = build_parser().parse_args()
    try:
        config = build_config(args)
    except CaptureCancelled as exc:
        print(str(exc))
        return 1

    def await_start():
        print("")
        print("Capture setup complete.")
        print(f"Sensor family: {get_sensor_spec(config.sensor_type).display_name}")
        print(f"Requested sensors: {config.sensor_count}")
        print(f"Tag: {config.tag}")
        response = prompt_text("Press Enter to start capture, or type quit to cancel and disconnect. ")
        if response.strip().lower() in {"q", "quit", "exit", "cancel"}:
            raise CaptureCancelled("Capture cancelled by operator before stream start.")

    manifest = run_capture_session(
        config,
        await_start=await_start,
    )
    print("")
    print(f"Capture status: {manifest['status']}")
    print(f"Session directory: {manifest['session_dir']}")
    print(f"Manifest: {Path(manifest['session_dir']) / 'capture_manifest.json'}")
    for path in manifest.get("output_files", []):
        print(f"Output file: {path}")
    for line in manifest.get("stream_summary", []):
        print(line)
    return 0 if manifest["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
