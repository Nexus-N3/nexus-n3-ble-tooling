from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Sequence

CLIENT_MODULES: dict[str, str] = {
    "MovellaDot/stream_client": "MovellaDot.stream_client",
    "MovellaDot.stream_client": "MovellaDot.stream_client",
    "movelladot": "MovellaDot.stream_client",
    "movella-dot": "MovellaDot.stream_client",
    # Common typo/backward-compatible alias from early setup examples.
    "MoveallDot/stream_client": "MovellaDot.stream_client",
    "MoveallDot.stream_client": "MovellaDot.stream_client",
    "NexusN3Dot/stream_client": "NexusN3Dot.stream_client",
    "NexusN3Dot.stream_client": "NexusN3Dot.stream_client",
    "nexusn3dot": "NexusN3Dot.stream_client",
    "nexus-n3-dot": "NexusN3Dot.stream_client",
    "Movesense/stream_client": "Movesense.stream_client",
    "Movesense.stream_client": "Movesense.stream_client",
    "movesense": "Movesense.stream_client",
    "MetaWear/stream_client": "MetaWear.stream_client",
    "MetaWear.stream_client": "MetaWear.stream_client",
    "metawear": "MetaWear.stream_client",
    "RFSurvey/client": "RFSurvey.client",
    "RFSurvey.client": "RFSurvey.client",
    "rf-survey": "RFSurvey.client",
    "RFSurvey/mixed_client": "RFSurvey.mixed_client",
    "RFSurvey.mixed_client": "RFSurvey.mixed_client",
    "rf-survey-mixed": "RFSurvey.mixed_client",
    "RFSurvey/mark_client": "RFSurvey.mark_client",
    "RFSurvey.mark_client": "RFSurvey.mark_client",
    "rf-survey-mark": "RFSurvey.mark_client",
    "Capture/cli": "Capture.cli",
    "Capture.cli": "Capture.cli",
    "capture": "Capture.cli",
}


def _client_list() -> str:
    canonical = [
        "MovellaDot/stream_client",
        "NexusN3Dot/stream_client",
        "Movesense/stream_client",
        "MetaWear/stream_client",
        "RFSurvey/client",
        "RFSurvey/mixed_client",
        "RFSurvey/mark_client",
        "Capture/cli",
    ]
    return "\n".join(f"  {name}" for name in canonical)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nexus-n3",
        description="Run Nexus BLE tooling clients installed from nexus-n3-tooling.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  nexus-n3 MovellaDot/stream_client --sensor-count 1 --stream-seconds 10\n"
            "  nexus-n3 NexusN3Dot/stream_client --sensor-count 1 --stream-seconds 10\n"
            "  nexus-n3 Movesense/stream_client --sensor-count 1 --stream-seconds 10\n"
            "  nexus-n3 rf-survey --window-ms 5000 --duration-ms 20000\n"
            "  nexus-n3 rf-survey-mark --movella-count 2 --window-ms 3000 --duration-ms 15000\n"
            "  nexus-n3 capture --sensor-type movelladot --sensor-count 2 --tag walk_trial\n"
            "\nAvailable clients:\n"
            f"{_client_list()}"
        ),
    )
    parser.add_argument(
        "client",
        nargs="?",
        help="Client to run, for example MovellaDot/stream_client or capture.",
    )
    parser.add_argument(
        "client_args",
        nargs=argparse.REMAINDER,
        help="Arguments passed through to the selected client.",
    )
    return parser


def resolve_client(client: str) -> str:
    if client in CLIENT_MODULES:
        return CLIENT_MODULES[client]

    normalized = client.replace("/", ".")
    if normalized in CLIENT_MODULES:
        return CLIENT_MODULES[normalized]

    if normalized.endswith(".py"):
        normalized = normalized[:-3]

    return normalized


def run_module_main(module_name: str, argv: Sequence[str]) -> int:
    module = importlib.import_module(module_name)
    main = getattr(module, "main", None)
    if main is None:
        raise SystemExit(f"Module {module_name!r} does not expose a main() function")

    old_argv = sys.argv[:]
    sys.argv = [module_name, *argv]
    try:
        result = main()
    finally:
        sys.argv = old_argv

    return 0 if result is None else int(result)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.client:
        parser.print_help()
        return 0

    if args.client in {"list", "clients"}:
        print(_client_list())
        return 0

    module_name = resolve_client(args.client)
    try:
        return run_module_main(module_name, args.client_args)
    except ModuleNotFoundError as exc:
        if exc.name == module_name or module_name.startswith(f"{exc.name}."):
            raise SystemExit(
                f"Unknown Nexus BLE client {args.client!r}.\n"
                "Run `nexus-n3 clients` to see the built-in clients."
            ) from exc
        raise


if __name__ == "__main__":
    raise SystemExit(main())
