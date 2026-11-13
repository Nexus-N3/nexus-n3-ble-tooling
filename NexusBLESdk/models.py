from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StreamFrame:
    sensor_id: int
    gateway_timestamp_us: int
    payload: bytes
    # Read completion, not application processing or physical byte arrival.
    # Defaults preserve positional construction by existing clients/tests.
    node_receive_monotonic_ns: int | None = field(default=None, compare=False)
    receive_read_id: int | None = field(default=None, compare=False)
    receive_read_size: int | None = field(default=None, compare=False)
    receive_batch_index: int | None = field(default=None, compare=False)


@dataclass(frozen=True)
class DiscoveredDevice:
    address: str
    name: str = ""
    rssi: int | None = None
    service_uuids: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SensorConnection:
    address: str
    sensor_id: int | None = None
