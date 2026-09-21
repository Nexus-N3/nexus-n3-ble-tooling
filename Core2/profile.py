"""CORE 2 BLE profile definitions and packet parsing."""

from __future__ import annotations

import struct
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# BLE profile
# ---------------------------------------------------------------------------

CORE_NAME_PREFIX = "CORE"

CORE_TEMP_SERVICE_UUID = "00002100-5B1E-4347-B07C-97B514DAE121"
CORE_TEMP_MEASUREMENT_UUID = "00002101-5B1E-4347-B07C-97B514DAE121"
CORE_TEMP_CONTROL_POINT_UUID = "00002102-5B1E-4347-B07C-97B514DAE121"

# Legacy CORE firmware advertised this private service rather than the
# current Core Temp Service.
CORE_LEGACY_PRIVATE_SERVICE_UUID = "00004200-F366-40B2-AC37-70CCE0AA83B1"

# Service UUIDs accepted when identifying a CORE device during discovery.
#
# Keep this as a set because discovery performs a set intersection against
# the services advertised by each BLE device.
CORE_DISCOVERY_SERVICE_UUIDS = {
    CORE_TEMP_SERVICE_UUID.lower(),
    CORE_LEGACY_PRIVATE_SERVICE_UUID.lower(),
}

BATTERY_SERVICE_UUID = "0000180F-0000-1000-8000-00805F9B34FB"
BATTERY_LEVEL_UUID = "00002A19-0000-1000-8000-00805F9B34FB"


# CORE currently emits Core Body Temperature characteristic notifications
# at 1 Hz. This is the BLE notification cadence, not the update rate of
# the individual temperature metrics.
EXPECTED_NOTIFICATION_RATE_HZ = 1.0


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def is_core2_device(device) -> bool:
    """
    Return True if a scan result identifies a CORE device.

    CORE devices may be identified either by an advertised CORE service
    UUID or by the standard "CORE" device-name prefix.
    """

    advertised_services = {
        str(uuid).lower()
        for uuid in getattr(device, "service_uuids", ())
        if uuid
    }

    if advertised_services & CORE_DISCOVERY_SERVICE_UUIDS:
        return True

    name = str(
        getattr(device, "name", "") or ""
    ).strip()

    return name.upper().startswith(
        CORE_NAME_PREFIX.upper()
    )


def select_addresses(
    matches,
    sensor_count: int,
) -> list[str]:
    """Select unique CORE 2 addresses from gateway scan results."""

    if sensor_count <= 0:
        return []

    addresses: list[str] = []

    for device in matches:
        if not is_core2_device(device):
            continue

        if device.address in addresses:
            continue

        addresses.append(device.address)

        if len(addresses) >= sensor_count:
            break

    return addresses


# ---------------------------------------------------------------------------
# Core Body Temperature characteristic flags
# ---------------------------------------------------------------------------

FLAG_SKIN_TEMPERATURE = 0x01
FLAG_CORE_RESERVED = 0x02
FLAG_QUALITY_STATE = 0x04
FLAG_TEMPERATURE_FAHRENHEIT = 0x08
FLAG_HEART_RATE = 0x10
FLAG_HEAT_STRAIN_INDEX = 0x20

# Bits 6-7 are reserved for future use.
RFU_MASK = 0xC0


# ---------------------------------------------------------------------------
# Special values
# ---------------------------------------------------------------------------

CORE_TEMPERATURE_UNAVAILABLE = 0x7FFF
HEAT_STRAIN_INDEX_UNAVAILABLE = 0xFF


# ---------------------------------------------------------------------------
# Quality / state values
# ---------------------------------------------------------------------------

QUALITY_INVALID = 0
QUALITY_POOR = 1
QUALITY_FAIR = 2
QUALITY_GOOD = 3
QUALITY_EXCELLENT = 4
QUALITY_NOT_AVAILABLE = 7

HR_STATE_NOT_SUPPORTED = 0
HR_STATE_NOT_RECEIVING = 1
HR_STATE_RECEIVING = 2
HR_STATE_NOT_AVAILABLE = 3


QUALITY_NAMES = {
    QUALITY_INVALID: "invalid",
    QUALITY_POOR: "poor",
    QUALITY_FAIR: "fair",
    QUALITY_GOOD: "good",
    QUALITY_EXCELLENT: "excellent",
    QUALITY_NOT_AVAILABLE: "n/a",
}

HR_STATE_NAMES = {
    HR_STATE_NOT_SUPPORTED: "not supported",
    HR_STATE_NOT_RECEIVING: "not receiving",
    HR_STATE_RECEIVING: "receiving",
    HR_STATE_NOT_AVAILABLE: "n/a",
}


# ---------------------------------------------------------------------------
# Decoded measurement
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Core2Measurement:
    """Decoded CORE Body Temperature characteristic notification."""

    flags: int

    core_temperature: float | None
    skin_temperature: float | None
    core_reserved: int | None

    core_data_quality: int | None
    heart_rate_state: int | None

    heart_rate: int | None
    heat_strain_index: float | None


# ---------------------------------------------------------------------------
# Binary helpers
# ---------------------------------------------------------------------------

class DataView:
    """Tiny helper for little-endian binary parsing."""

    def __init__(self, data: bytes):
        self.data = data

    def _slice(self, start: int, count: int) -> bytes:
        return self.data[start:start + count]

    def get_uint_8(self, start: int) -> int:
        return int.from_bytes(
            self._slice(start, 1),
            byteorder="little",
            signed=False,
        )

    def get_int_16(self, start: int) -> int:
        return struct.unpack(
            "<h",
            self._slice(start, 2),
        )[0]


# ---------------------------------------------------------------------------
# Measurement parsing
# ---------------------------------------------------------------------------

def parse_measurement(packet: bytes) -> Core2Measurement | None:
    """Parse a CORE Body Temperature characteristic notification."""

    # Flags + mandatory SINT16 core temperature.
    if not packet or len(packet) < 3:
        return None

    view = DataView(packet)

    flags = view.get_uint_8(0)

    # Bits 6-7 are reserved in the current profile.
    if flags & RFU_MASK:
        return None

    offset = 1

    temperature_is_fahrenheit = bool(
        flags & FLAG_TEMPERATURE_FAHRENHEIT
    )

    # ------------------------------------------------------------------
    # Core body temperature - mandatory
    # ------------------------------------------------------------------

    core_raw = view.get_int_16(offset)
    offset += 2

    if core_raw == CORE_TEMPERATURE_UNAVAILABLE:
        core_temperature = None
    else:
        core_temperature = core_raw / 100.0

        if temperature_is_fahrenheit:
            core_temperature = _fahrenheit_to_celsius(
                core_temperature
            )

    # ------------------------------------------------------------------
    # Optional fields
    # ------------------------------------------------------------------

    skin_temperature = None
    core_reserved = None
    core_data_quality = None
    heart_rate_state = None
    heart_rate = None
    heat_strain_index = None

    if flags & FLAG_SKIN_TEMPERATURE:
        if offset + 2 > len(packet):
            return None

        skin_raw = view.get_int_16(offset)
        offset += 2

        skin_temperature = skin_raw / 100.0

        if temperature_is_fahrenheit:
            skin_temperature = _fahrenheit_to_celsius(
                skin_temperature
            )

    if flags & FLAG_CORE_RESERVED:
        if offset + 2 > len(packet):
            return None

        core_reserved = view.get_int_16(offset)
        offset += 2

    if flags & FLAG_QUALITY_STATE:
        if offset + 1 > len(packet):
            return None

        quality_state = view.get_uint_8(offset)
        offset += 1

        quality = quality_state & 0x07
        state = (quality_state >> 4) & 0x03

        if quality != QUALITY_NOT_AVAILABLE:
            core_data_quality = quality

        if state != HR_STATE_NOT_AVAILABLE:
            heart_rate_state = state

    if flags & FLAG_HEART_RATE:
        if offset + 1 > len(packet):
            return None

        heart_rate_raw = view.get_uint_8(offset)
        offset += 1

        # CORE uses zero when no HR signal is being received.
        if heart_rate_raw != 0:
            heart_rate = heart_rate_raw

    if flags & FLAG_HEAT_STRAIN_INDEX:
        if offset + 1 > len(packet):
            return None

        hsi_raw = view.get_uint_8(offset)
        offset += 1

        if hsi_raw != HEAT_STRAIN_INDEX_UNAVAILABLE:
            heat_strain_index = hsi_raw / 10.0

    # The packet should contain exactly the fields described by flags.
    if offset != len(packet):
        return None

    return Core2Measurement(
        flags=flags,
        core_temperature=core_temperature,
        skin_temperature=skin_temperature,
        core_reserved=core_reserved,
        core_data_quality=core_data_quality,
        heart_rate_state=heart_rate_state,
        heart_rate=heart_rate,
        heat_strain_index=heat_strain_index,
    )


# ---------------------------------------------------------------------------
# Battery
# ---------------------------------------------------------------------------

def parse_battery_level(packet: bytes) -> int | None:
    """Parse the standard BLE Battery Level characteristic."""

    if not packet:
        return None

    level = packet[0]

    if level > 100:
        return None

    return level


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def quality_name(value: int | None) -> str:
    """Return a readable CORE measurement quality."""

    if value is None:
        return "n/a"

    return QUALITY_NAMES.get(
        value,
        f"unknown ({value})",
    )


def heart_rate_state_name(value: int | None) -> str:
    """Return a readable CORE heart-rate input state."""

    if value is None:
        return "n/a"

    return HR_STATE_NAMES.get(
        value,
        f"unknown ({value})",
    )


def _fahrenheit_to_celsius(value: float) -> float:
    """Convert Fahrenheit to Celsius."""

    return (value - 32.0) * 5.0 / 9.0