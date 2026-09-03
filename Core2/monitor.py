"""CORE 2 stream monitoring."""

from __future__ import annotations

from NexusBLESdk import (
    GenericStreamMonitor,
    SensorConnection,
    StartupGateConfig,
    StreamFrame,
)

from .profile import EXPECTED_NOTIFICATION_RATE_HZ


def core2_timestamp_source(frame: StreamFrame) -> int:
    """
    Return the timestamp used for CORE 2 stream diagnostics.

    CORE 2 measurement notifications do not contain a sensor timestamp,
    so stream cadence and gap detection use the timestamp captured by the
    Nexus N3 BLE Gateway when the notification was received.
    """

    return frame.gateway_timestamp_us


class Core2StreamMonitor(GenericStreamMonitor):
    """
    Generic Nexus stream monitor configured for CORE 2.

    CORE 2 currently produces one BLE measurement notification per second.
    The Gateway notification timestamp is therefore used to evaluate
    startup stability, observed notification rate, and notification gaps.
    """

    def __init__(
        self,
        *,
        connections: list[SensorConnection],
        labels_by_address: dict[str, str | None],
        startup_gate: StartupGateConfig,
        verbose: bool = True,
    ):
        super().__init__(
            connections=connections,
            labels_by_address=labels_by_address,
            expected_rate_hz=int(EXPECTED_NOTIFICATION_RATE_HZ),
            timestamp_source=core2_timestamp_source,
            detect_gaps=True,
            startup_gate=startup_gate,
            verbose=verbose,
        )