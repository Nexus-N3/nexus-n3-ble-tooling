from __future__ import annotations

import struct

# N3 Dot GATT profile and packet parsing
NEXUS_N3_DOT_NAME = "Nexus N3 Dot"
NEXUS_N3_DOT_IMU_MEASUREMENT_UUID = "8e400002-f315-4f60-9fb8-838830daea50"
NEXUS_N3_DOT_CONTROL_COMMAND_UUID = "8e410002-f315-4f60-9fb8-838830daea50"
NEXUS_N3_DOT_DEVICE_STATUS_UUID = "8e410003-f315-4f60-9fb8-838830daea50"
NEXUS_N3_DOT_SET_ODR_HEX = {
    20: "031400",
    50: "033200",
    100: "036400",
}
NEXUS_N3_DOT_START_HEX = "01"
NEXUS_N3_DOT_STOP_HEX = "02"
NEXUS_N3_DOT_PACKET = struct.Struct("<BBHIhhhhhh")
NEXUS_N3_DOT_DEVICE_STATUS_V1 = struct.Struct("<B H I I")
NEXUS_N3_DOT_DEVICE_STATUS_V2 = struct.Struct("<B H I I I I")

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

# should be moved to the sdk if we want to share with other profiles
DEFAULT_STARTUP_GATE = {
    "enabled": True,
    "stability_window_seconds": 5.0,
    "packets_required": 100,
    "min_rate_hz": 98.0,
    "min_observation_seconds": 2.0,
    "max_gap_events": 0,
    "gap_grace_seconds": 2.0,
}


def parse_sensor_timestamp(payload: bytes) -> int:
    if len(payload) != NEXUS_N3_DOT_PACKET.size:
        raise ValueError(
            f"Nexus N3 Dot payload wrong size: expected {NEXUS_N3_DOT_PACKET.size}, got {len(payload)}"
        )

    version, _flags, _seq, timestamp_us, *_axes = NEXUS_N3_DOT_PACKET.unpack(payload)
    if version != 1:
        raise ValueError(f"Unsupported Nexus N3 Dot packet version: {version}")
    return int(timestamp_us)


def parse_packet(payload: bytes) -> dict[str, int]:
    if len(payload) != NEXUS_N3_DOT_PACKET.size:
        raise ValueError(
            f"Nexus N3 Dot payload wrong size: expected {NEXUS_N3_DOT_PACKET.size}, got {len(payload)}"
        )

    (
        version,
        flags,
        sequence,
        timestamp_us,
        accel_x_mg,
        accel_y_mg,
        accel_z_mg,
        gyro_x_mdps,
        gyro_y_mdps,
        gyro_z_mdps,
    ) = NEXUS_N3_DOT_PACKET.unpack(payload)
    if version != 1:
        raise ValueError(f"Unsupported Nexus N3 Dot packet version: {version}")
    return {
        "version": int(version),
        "flags": int(flags),
        "sequence": int(sequence),
        "timestamp_us": int(timestamp_us),
        "accel_x_mg": int(accel_x_mg),
        "accel_y_mg": int(accel_y_mg),
        "accel_z_mg": int(accel_z_mg),
        "gyro_x_mdps": int(gyro_x_mdps),
        "gyro_y_mdps": int(gyro_y_mdps),
        "gyro_z_mdps": int(gyro_z_mdps),
    }


def select_addresses(matches, count: int) -> list[str]:
    if len(matches) < count:
        raise RuntimeError(f"Requested {count} Nexus N3 Dot sensors, found {len(matches)}")
    return [entry.address for entry in matches[:count]]


def parse_device_status(payload: bytes) -> dict[str, int]:
    if len(payload) == NEXUS_N3_DOT_DEVICE_STATUS_V2.size:
        running, odr_hz, packets_sent, packets_dropped, imu_read_failures, notify_failures = (
            NEXUS_N3_DOT_DEVICE_STATUS_V2.unpack(payload)
        )
        return {
            "running": int(running),
            "odr_hz": int(odr_hz),
            "packets_sent": int(packets_sent),
            "packets_dropped": int(packets_dropped),
            "imu_read_failures": int(imu_read_failures),
            "notify_failures": int(notify_failures),
        }

    if len(payload) == NEXUS_N3_DOT_DEVICE_STATUS_V1.size:
        running, odr_hz, packets_sent, packets_dropped = NEXUS_N3_DOT_DEVICE_STATUS_V1.unpack(payload)
        return {
            "running": int(running),
            "odr_hz": int(odr_hz),
            "packets_sent": int(packets_sent),
            "packets_dropped": int(packets_dropped),
            "imu_read_failures": -1,
            "notify_failures": -1,
        }

    raise ValueError(
        "Nexus N3 Dot device status wrong size: "
        f"expected {NEXUS_N3_DOT_DEVICE_STATUS_V1.size} or {NEXUS_N3_DOT_DEVICE_STATUS_V2.size}, got {len(payload)}"
    )
