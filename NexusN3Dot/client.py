from __future__ import annotations

import time

from NexusBLESdk import GatewayClient, SensorConnection

from .profile import (
    NEXUS_N3_DOT_CONTROL_COMMAND_UUID,
    NEXUS_N3_DOT_IMU_MEASUREMENT_UUID,
    NEXUS_N3_DOT_NAME,
    NEXUS_N3_DOT_SET_ODR_HEX,
    NEXUS_N3_DOT_START_HEX,
    NEXUS_N3_DOT_STOP_HEX,
    select_addresses,
)


class NexusN3DotClient:
    def __init__(self, gateway: GatewayClient):
        self.gateway = gateway
        self.connections: list[SensorConnection] = []

    def discover(self, sensor_count: int, scan_timeout_ms: int) -> list[str]:
        matches = self.gateway.scan(scan_timeout_ms, name_filter=NEXUS_N3_DOT_NAME)
        return select_addresses(matches, sensor_count)

    def connect(self, addresses: list[str], timeout_s: float) -> list[SensorConnection]:
        self.connections = self.gateway.connect(addresses, timeout_s=timeout_s)
        return self.connections

    def configure(
        self,
        *,
        sampling_rate_hz: int,
        subscribe_timeout_s: float,
        write_timeout_s: float,
        without_response: bool,
    ):
        for connection in self.connections:
            print(f"CONFIG {connection.address}: pre-stop")
            self.gateway.write_gatt(
                connection.address,
                NEXUS_N3_DOT_CONTROL_COMMAND_UUID,
                NEXUS_N3_DOT_STOP_HEX,
                timeout_s=write_timeout_s,
                without_response=True,
            )
            time.sleep(0.25)

            print(f"CONFIG {connection.address}: subscribe")
            self.gateway.subscribe(
                connection.address,
                NEXUS_N3_DOT_IMU_MEASUREMENT_UUID,
                timeout_s=subscribe_timeout_s,
                binary_notifications=True,
            )
            time.sleep(0.75)

            print(f"CONFIG {connection.address}: set-rate {sampling_rate_hz}Hz")
            self.gateway.write_gatt(
                connection.address,
                NEXUS_N3_DOT_CONTROL_COMMAND_UUID,
                NEXUS_N3_DOT_SET_ODR_HEX[sampling_rate_hz],
                timeout_s=write_timeout_s,
                without_response=without_response,
            )
            time.sleep(0.25)

    def start_streams(self, *, write_timeout_s: float, without_response: bool) -> dict[str, float | None]:
        started_at: dict[str, float | None] = {}
        for connection in self.connections:
            print(f"START STREAM: {connection.address}")
            started_at[connection.address] = self._send_start_command(
                connection.address,
                without_response=True,
            )
            time.sleep(0.02)
        return started_at

    def stop_streams(self, *, write_timeout_s: float, without_response: bool):
        print("Stopping stream now.")
        for connection in self.connections:
            print(f"STOP STREAM: {connection.address}")
            self.gateway.write_gatt(
                connection.address,
                NEXUS_N3_DOT_CONTROL_COMMAND_UUID,
                NEXUS_N3_DOT_STOP_HEX,
                timeout_s=write_timeout_s,
                without_response=True,
                allow_timeout=True,
            )
            print(f"STOP STREAM COMPLETE: {connection.address}")

    def disconnect_all(self, timeout_s: float):
        self.gateway.disconnect(
            [connection.address for connection in self.connections],
            timeout_s=timeout_s,
            allow_timeout=True,
        )

    def _send_start_command(self, address: str, *, without_response: bool) -> float:
        request_id = self.gateway.request_id("write")
        self.gateway.send(
            {
                "type": "gatt_write",
                "request_id": request_id,
                "address": address,
                "characteristic_uuid": NEXUS_N3_DOT_CONTROL_COMMAND_UUID,
                "payload_hex": NEXUS_N3_DOT_START_HEX,
                "without_response": without_response,
            }
        )
        return time.monotonic()
