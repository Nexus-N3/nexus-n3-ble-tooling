from __future__ import annotations

import struct


METAWEAR_NAME = "MetaWear"
METAWEAR_NAME_PREFIXES = ("MetaWear", "MetaMotion", "MMR", "MMRL")

METAWEAR_SERVICE_UUID = "326a9000-85cb-9195-d9dd-464cfbbae75a"
METAWEAR_COMMAND_UUID = "326a9001-85cb-9195-d9dd-464cfbbae75a"
METAWEAR_NOTIFY_UUID = "326a9006-85cb-9195-d9dd-464cfbbae75a"


# limit to acceleration for first pass
METAWEAR_MODULE_ACCEL = 0x03

METAWEAR_ACCEL_POWER_MODE = 0x01
METAWEAR_ACCEL_DATA_INTERRUPT_ENABLE = 0x02
METAWEAR_ACCEL_DATA_CONFIG = 0x03
METAWEAR_ACCEL_DATA = 0x04

# First pass: 100 Hz, +/-16g, unpacked acceleration.
#
# Command:
#   03 03 28 0c
#
# Meaning:
#   03 = accelerometer module
#   03 = accelerometer config register
#   28 = ODR config byte, 100 Hz for BMI160-style accel
#   0c = range config byte, +/-16g for BMI160-style accel
METAWEAR_ACCEL_ODR_HZ = 100
METAWEAR_ACCEL_RANGE_G = 16
METAWEAR_ACCEL_CONFIG_HEX = "0303280C"

# For +/-16g, the scale is 2048 LSB/g.
METAWEAR_ACCEL_SCALE_LSB_PER_G = 2048.0

# Notification shape:
#
#   byte 0      module_id    0x03
#   byte 1      register_id  0x04
#   byte 2-3    x_raw        int16 little-endian
#   byte 4-5    y_raw        int16 little-endian
#   byte 6-7    z_raw        int16 little-endian
#
METAWEAR_ACCEL_PREFIX = struct.Struct("<2B3h")

DEFAULT_STARTUP_GATE = {
    "enabled": True,
    "stability_window_seconds": 5.0,
    "packets_required": 100,
    "min_rate_hz": 95.0,
    "min_observation_seconds": 2.0,
    "max_gap_events": 0,
    "gap_grace_seconds": 2.0,
}

DEFAULT_LOCATIONS = [
    "LEFT_ANKLE",
    "RIGHT_ANKLE",
    "LEFT_THIGH",
    "RIGHT_THIGH",
    "CHEST",
    "LOWER_BACK",
    "HEAD",
    "UPPER_BACK",
    "LEFT_WRIST",
    "RIGHT_WRIST",
]


def is_metawear_name(name: str | None) -> bool:
    if not name:
        return False
    return any(name.startswith(prefix) for prefix in METAWEAR_NAME_PREFIXES)


def parse_sensor_timestamp(payload: bytes) -> int:
    """
    Compatibility hook for NexusBLESdk.GenericStreamMonitor.

    Simple live MetaWear acceleration notifications do not contain an embedded
    sensor timestamp. Use frame.gateway_timestamp_us in the client/CSV output.
    """
    raise ValueError("MetaWear acceleration notifications do not contain an embedded sensor timestamp")


def parse_packet(payload: bytes) -> dict[str, float | int | str]:
    if len(payload) < METAWEAR_ACCEL_PREFIX.size:
        raise ValueError(
            f"MetaWear acceleration payload too short: expected at least "
            f"{METAWEAR_ACCEL_PREFIX.size}, got {len(payload)}"
        )

    module_id, register_id, x_raw, y_raw, z_raw = METAWEAR_ACCEL_PREFIX.unpack_from(payload)

    if module_id != METAWEAR_MODULE_ACCEL or register_id != METAWEAR_ACCEL_DATA:
        return {
            "kind": "unknown",
            "module_id": int(module_id),
            "register_id": int(register_id),
            "payload_len": len(payload),
        }

    return {
        "kind": "accel",
        "accel_x": float(x_raw) / METAWEAR_ACCEL_SCALE_LSB_PER_G,
        "accel_y": float(y_raw) / METAWEAR_ACCEL_SCALE_LSB_PER_G,
        "accel_z": float(z_raw) / METAWEAR_ACCEL_SCALE_LSB_PER_G,
        "accel_x_raw": int(x_raw),
        "accel_y_raw": int(y_raw),
        "accel_z_raw": int(z_raw),
    }


def configure_commands() -> list[str]:
    return [
        METAWEAR_ACCEL_CONFIG_HEX,
    ]


def start_commands() -> list[str]:
    return [
        "030401",    # subscribe to acceleration data signal
        "03020100",  # enable accelerometer data sampling
        "030101",    # start accelerometer
    ]


def stop_commands() -> list[str]:
    return [
        "030100",    # stop accelerometer
        "03020001",  # disable accelerometer data sampling
        "030400",    # unsubscribe from acceleration data signal
    ]


def select_addresses(matches, count: int) -> list[str]:
    if len(matches) < count:
        raise RuntimeError(f"Requested {count} MetaWear sensors, found {len(matches)}")
    return [entry.address for entry in matches[:count]]