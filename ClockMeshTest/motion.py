"""Motion columns from the configured Movella DOT payload; no clock dependency."""
from MovellaDot.profile import parse_packet

# CSV column, existing decoder key, unit. Values pass through without conversion.
MOTION_CHANNELS = (
    ("quat_w", "quat_w", "dimensionless"),
    ("quat_x", "quat_x", "dimensionless"),
    ("quat_y", "quat_y", "dimensionless"),
    ("quat_z", "quat_z", "dimensionless"),
    ("accel_x_m_s2", "accel_x", "m/s²"),
    ("accel_y_m_s2", "accel_y", "m/s²"),
    ("accel_z_m_s2", "accel_z", "m/s²"),
    ("gyro_x_deg_s", "gyro_x", "deg/s"),
    ("gyro_y_deg_s", "gyro_y", "deg/s"),
    ("gyro_z_deg_s", "gyro_z", "deg/s"),
)
MOTION_FIELDS = [column for column, _, _ in MOTION_CHANNELS]
MOTION_UNITS = {column: unit for column, _, unit in MOTION_CHANNELS}


def decode_motion(payload: bytes) -> dict[str, float]:
    """Reuse the profile's <I10f decoder, including its payload length check."""
    packet = parse_packet(payload)
    return {column: packet[key] for column, key, _ in MOTION_CHANNELS}
