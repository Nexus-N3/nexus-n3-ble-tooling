from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import serial
from serial.tools import list_ports


DEFAULT_PORT = "nexus_n3_gw"
DEFAULT_BAUD = 1_000_000
STREAM_FRAME_MAGIC = b"\xA5\x5A"


PORT_ALIASES = {
    "nexus_n3_gw": {
        "description": "Makerdiary nRF54L15 Connect Kit / Nexus N3 Gateway",
        "by_id_required": ("ZEPHYR_IFMCU_CMSIS-DAP",),
        "by_id_preferred": ("if01",),
        "keywords": (
            "ZEPHYR_IFMCU_CMSIS-DAP",
            "ZEPHYR IFMCU CMSIS-DAP",
            "Makerdiary",
            "CMSIS-DAP",
            "2fe3",
        ),
        "preferred_interfaces": ("if01",),
    },
    "nordic_dev": {
        "description": "Nordic nRF54L15 DK with onboard SEGGER J-Link",
        "by_id_required": ("SEGGER", "J-Link"),
        "by_id_preferred": ("if02",),
        "keywords": (
            "SEGGER",
            "J-Link",
            "Nordic",
        ),
        "preferred_interfaces": ("if02",),
    },
}


def json_objects_from_line(line: str):
    decoder = json.JSONDecoder()

    for index, character in enumerate(line):
        if character != "{":
            continue

        try:
            obj, _ = decoder.raw_decode(line[index:])
            yield obj
        except json.JSONDecodeError:
            continue


def _find_by_id_port(
    *,
    required: tuple[str, ...],
    preferred: tuple[str, ...] = (),
) -> str | None:
    by_id_dir = Path("/dev/serial/by-id")

    if not by_id_dir.exists():
        return None

    candidates: list[tuple[int, str]] = []

    for path in by_id_dir.iterdir():
        name = path.name.lower()

        if not all(token.lower() in name for token in required):
            continue

        score = 100

        for token in preferred:
            if token.lower() in name:
                score += 50

        candidates.append((score, str(path)))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _by_id_path_for_device(device: str) -> str | None:
    by_id_dir = Path("/dev/serial/by-id")

    if not by_id_dir.exists():
        return None

    try:
        device_real = Path(device).resolve()
    except OSError:
        return None

    for candidate in by_id_dir.iterdir():
        try:
            if candidate.resolve() == device_real:
                return str(candidate)
        except OSError:
            continue

    return None


def _serial_port_text(port_info) -> str:
    return " ".join(
        str(value or "")
        for value in (
            port_info.device,
            port_info.name,
            port_info.description,
            port_info.hwid,
            getattr(port_info, "manufacturer", ""),
            getattr(port_info, "product", ""),
            getattr(port_info, "serial_number", ""),
        )
    )


def _score_serial_port(
    port_info,
    *,
    keywords: tuple[str, ...],
    preferred_interfaces: tuple[str, ...],
) -> int:
    text = _serial_port_text(port_info).lower()

    score = 0

    for keyword in keywords:
        if keyword.lower() in text:
            score += 50

    for interface_name in preferred_interfaces:
        if interface_name.lower() in text:
            score += 25

    by_id_path = _by_id_path_for_device(port_info.device)
    if by_id_path is not None:
        score += 10
        by_id_lower = by_id_path.lower()

        for interface_name in preferred_interfaces:
            if interface_name.lower() in by_id_lower:
                score += 25

    return score


def list_gateway_serial_ports() -> list[str]:
    return [
        f"{p.device}: {p.description} [{p.hwid}]"
        for p in list_ports.comports()
    ]


def _known_aliases_text() -> str:
    return ", ".join(["auto", *sorted(PORT_ALIASES)])


def _find_port_for_alias(alias: str) -> str | None:
    config = PORT_ALIASES[alias]

    by_id_port = _find_by_id_port(
        required=config["by_id_required"],
        preferred=config["by_id_preferred"],
    )
    if by_id_port is not None:
        return by_id_port

    scored: list[tuple[int, str]] = []

    for port_info in list_ports.comports():
        score = _score_serial_port(
            port_info,
            keywords=config["keywords"],
            preferred_interfaces=config["preferred_interfaces"],
        )

        if score <= 0:
            continue

        device = _by_id_path_for_device(port_info.device) or port_info.device
        scored.append((score, device))

    if not scored:
        return None

    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def find_gateway_serial_port(alias: str = DEFAULT_PORT) -> str:
    if alias == "auto":
        aliases_to_try = (DEFAULT_PORT, "nordic_dev")
    else:
        if alias not in PORT_ALIASES:
            raise ValueError(
                f"Unknown serial port alias '{alias}'. "
                f"Known aliases: {_known_aliases_text()}"
            )

        aliases_to_try = (alias,)

    for alias_name in aliases_to_try:
        port = _find_port_for_alias(alias_name)
        if port is not None:
            return port

    available = "\n".join(f"- {line}" for line in list_gateway_serial_ports())
    raise RuntimeError(
        f"Could not find a serial port for alias '{alias}'. "
        f"Pass a serial path explicitly or check the device connection.\n"
        f"Available serial ports:\n{available}"
    )


def resolve_gateway_serial_port(port: str | None = DEFAULT_PORT) -> str:
    if port is None or port.strip() == "":
        return find_gateway_serial_port(DEFAULT_PORT)

    normalized = port.strip()

    if normalized == "auto":
        return find_gateway_serial_port("auto")

    if normalized in PORT_ALIASES:
        return find_gateway_serial_port(normalized)

    return normalized


@contextmanager
def open_gateway_serial(
    port: str | None = DEFAULT_PORT,
    baudrate: int = DEFAULT_BAUD,
) -> Iterator[serial.Serial]:
    resolved_port = resolve_gateway_serial_port(port)
    print(f"Opening serial port '{resolved_port}' at baudrate {baudrate}...")

    ser = serial.Serial(
        port=resolved_port,
        baudrate=baudrate,
        timeout=0.1,
        write_timeout=1.0,
        dsrdtr=False,
        rtscts=False,
    )

    try:
        ser.setDTR(True)
        ser.setRTS(True)
        time.sleep(0.5)
        ser.reset_input_buffer()
        yield ser
    finally:
        ser.close()