from __future__ import annotations

import struct

# Nexus N3 HDR Dot GATT profile and packet parsing
NEXUS_N3_HDR_DOT_NAME = "Nexus N3 HDR Dot"

NEXUS_N3_HDR_DOT_IMU_MEASUREMENT_UUID = "8e420002-f315-4f60-9fb8-838830daea50"
NEXUS_N3_HDR_DOT_CONTROL_COMMAND_UUID = "8e430002-f315-4f60-9fb8-838830daea50"
NEXUS_N3_HDR_DOT_DEVICE_STATUS_UUID = "8e430003-f315-4f60-9fb8-838830daea50"

# Control opcodes
NEXUS_N3_HDR_DOT_START_HEX = "01"
NEXUS_N3_HDR_DOT_STOP_HEX = "02"

# CTRL_SET_STREAM_MODE = 0x03
NEXUS_N3_HDR_DOT_SET_STREAM_MODE_HEX = {
    "ALL": "0301",
    "MAG": "0302",
    "X": "0303",
    "Y": "0304",
    "Z": "0305",
    "XYZ": "0306",
}

# Stream mode values from imu.h
NEXUS_N3_HDR_DOT_STREAM_ALL = 0x01
NEXUS_N3_HDR_DOT_STREAM_MAG = 0x02
NEXUS_N3_HDR_DOT_STREAM_X = 0x03
NEXUS_N3_HDR_DOT_STREAM_Y = 0x04
NEXUS_N3_HDR_DOT_STREAM_Z = 0x05
NEXUS_N3_HDR_DOT_STREAM_XYZ = 0x06

NEXUS_N3_HDR_DOT_STREAM_MODE_NAMES = {
    NEXUS_N3_HDR_DOT_STREAM_ALL: "ALL",
    NEXUS_N3_HDR_DOT_STREAM_MAG: "MAG",
    NEXUS_N3_HDR_DOT_STREAM_X: "X",
    NEXUS_N3_HDR_DOT_STREAM_Y: "Y",
    NEXUS_N3_HDR_DOT_STREAM_Z: "Z",
    NEXUS_N3_HDR_DOT_STREAM_XYZ: "XYZ",
}

NEXUS_N3_HDR_DOT_STREAM_MODE_VALUES = {
    "ALL": NEXUS_N3_HDR_DOT_STREAM_ALL,
    "MAG": NEXUS_N3_HDR_DOT_STREAM_MAG,
    "X": NEXUS_N3_HDR_DOT_STREAM_X,
    "Y": NEXUS_N3_HDR_DOT_STREAM_Y,
    "Z": NEXUS_N3_HDR_DOT_STREAM_Z,
    "XYZ": NEXUS_N3_HDR_DOT_STREAM_XYZ,
}

NEXUS_N3_HDR_DOT_STREAM_VALUES_PER_SAMPLE = {
    "ALL": 4,
    "MAG": 1,
    "X": 1,
    "Y": 1,
    "Z": 1,
    "XYZ": 3,
}

NEXUS_N3_HDR_DOT_STREAM_FIELDS = {
    "ALL": ("accel_x_mg", "accel_y_mg", "accel_z_mg", "magnitude_mg"),
    "MAG": ("magnitude_mg",),
    "X": ("accel_x_mg",),
    "Y": ("accel_y_mg",),
    "Z": ("accel_z_mg",),
    "XYZ": ("accel_x_mg", "accel_y_mg", "accel_z_mg"),
}

NEXUS_N3_HDR_DOT_DEVICE_STATUS_V2_SIZE = 28
NEXUS_N3_HDR_DOT_TIMESTAMP_BYTES = 4

DEFAULT_LOCATIONS = [
    "LEFT_KNEE",
    "RIGHT_KNEE",
]

DEFAULT_STARTUP_GATE = {
    "enabled": True,
    "stability_window_seconds": 5.0,
    "packets_required": 100,
    "min_rate_hz": 18.0,
    "min_observation_seconds": 2.0,
    "max_gap_events": 0,
    "gap_grace_seconds": 2.0,
}


def normalize_stream_mode(stream_mode: str | int) -> str:
    if isinstance(stream_mode, str):
        mode = stream_mode.strip().upper()
        if mode not in NEXUS_N3_HDR_DOT_STREAM_VALUES_PER_SAMPLE:
            raise ValueError(f"Unsupported Nexus N3 HDR Dot stream mode: {stream_mode!r}")
        return mode

    if isinstance(stream_mode, int):
        try:
            return NEXUS_N3_HDR_DOT_STREAM_MODE_NAMES[stream_mode]
        except KeyError as exc:
            raise ValueError(f"Unsupported Nexus N3 HDR Dot stream mode value: {stream_mode}") from exc

    raise TypeError(f"Unsupported stream mode type: {type(stream_mode)!r}")


def build_set_stream_mode_command(stream_mode: str | int) -> bytes:
    mode = normalize_stream_mode(stream_mode)
    return bytes([0x03, NEXUS_N3_HDR_DOT_STREAM_MODE_VALUES[mode]])


def build_identify_command(duration_ms: int) -> bytes:
    if duration_ms < 0 or duration_ms > 0xFFFF:
        raise ValueError(f"Identify duration out of range: {duration_ms}")
    return struct.pack("<BH", 0x04, duration_ms)


def parse_sensor_timestamp(payload: bytes) -> int:
    if len(payload) < NEXUS_N3_HDR_DOT_TIMESTAMP_BYTES:
        raise ValueError(
            f"Nexus N3 HDR Dot payload too short for timestamp: got {len(payload)} bytes"
        )
    return int(struct.unpack_from("<I", payload, 0)[0])


def parse_packet(payload: bytes, stream_mode: str | int = "MAG") -> dict:
    mode = normalize_stream_mode(stream_mode)

    values_per_sample = NEXUS_N3_HDR_DOT_STREAM_VALUES_PER_SAMPLE[mode]
    fields = NEXUS_N3_HDR_DOT_STREAM_FIELDS[mode]
    bytes_per_sample = values_per_sample * 2

    if len(payload) == 0:
        raise ValueError("Nexus N3 HDR Dot payload is empty")

    payload_bytes = payload
    header_bytes = 0
    timestamp_us = None
    if len(payload) >= NEXUS_N3_HDR_DOT_TIMESTAMP_BYTES and (
        len(payload) - NEXUS_N3_HDR_DOT_TIMESTAMP_BYTES
    ) % bytes_per_sample == 0:
        header_bytes = NEXUS_N3_HDR_DOT_TIMESTAMP_BYTES
        payload_bytes = payload[header_bytes:]
        timestamp_us = parse_sensor_timestamp(payload)
    elif len(payload) % bytes_per_sample != 0:
        raise ValueError(
            f"Nexus N3 HDR Dot payload wrong size for {mode}: "
            f"got {len(payload)}, bytes_per_sample={bytes_per_sample}"
        )

    value_count = len(payload_bytes) // 2
    values = struct.unpack_from(f"<{value_count}h", payload_bytes)

    samples = []
    for offset in range(0, value_count, values_per_sample):
        sample_values = values[offset : offset + values_per_sample]
        samples.append(
            {
                field_name: int(value)
                for field_name, value in zip(fields, sample_values)
            }
        )

    return {
        "stream_mode": mode,
        "sample_count": len(samples),
        "payload_bytes": len(payload),
        "timestamp_us": timestamp_us,
        "samples": samples,
    }


def select_addresses(matches, count: int) -> list[str]:
    if len(matches) < count:
        raise RuntimeError(f"Requested {count} Nexus N3 HDR Dot sensors, found {len(matches)}")
    return [entry.address for entry in matches[:count]]


def parse_device_status(payload: bytes) -> dict[str, int | str]:
    if len(payload) != NEXUS_N3_HDR_DOT_DEVICE_STATUS_V2_SIZE:
        raise ValueError(
            "Nexus N3 HDR Dot device status wrong size: "
            f"expected {NEXUS_N3_HDR_DOT_DEVICE_STATUS_V2_SIZE}, got {len(payload)}"
        )

    running = int(payload[0])
    stream_mode_value = int(payload[1])
    stream_mode_name = NEXUS_N3_HDR_DOT_STREAM_MODE_NAMES.get(stream_mode_value, "UNKNOWN")

    output_rate_hz = struct.unpack_from("<H", payload, 2)[0]
    max_payload_bytes = struct.unpack_from("<H", payload, 4)[0]
    samples_per_notification = struct.unpack_from("<H", payload, 6)[0]
    packets_sent = struct.unpack_from("<I", payload, 8)[0]
    packets_dropped = struct.unpack_from("<I", payload, 12)[0]

    return {
        "running": running,
        "stream_mode": stream_mode_value,
        "stream_mode_name": stream_mode_name,
        "output_rate_hz": int(output_rate_hz),
        "max_payload_bytes": int(max_payload_bytes),
        "samples_per_notification": int(samples_per_notification),
        "packets_sent": int(packets_sent),
        "packets_dropped": int(packets_dropped),
        "raw_status_hex": payload.hex(" "),
    }