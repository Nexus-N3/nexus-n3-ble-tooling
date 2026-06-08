from __future__ import annotations

import time

from NexusBLESdk import GatewayClient, SensorConnection

from .profile import (
    MOVELLA_DEVICE_CONTROL_UUID,
    MOVELLA_LONG_PAYLOAD_UUID,
    MOVELLA_NAME,
    MOVELLA_SET_RATE_HEX,
    MOVELLA_START_HEX,
    MOVELLA_START_STOP_STREAM_UUID,
    MOVELLA_STOP_HEX,
    parse_packet,
    select_addresses,
)


class MovellaDotClient:
    def __init__(self, gateway: GatewayClient):
        self.gateway = gateway
        self.connections: list[SensorConnection] = []
        self._parsed_row_writer = None

    def discover(self, sensor_count: int, scan_timeout_ms: int) -> list[str]:
        matches = self.gateway.scan(scan_timeout_ms, name_filter=MOVELLA_NAME)
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
        effective_subscribe_timeout_s = max(
            subscribe_timeout_s,
            min(20.0, 6.0 + (len(self.connections) * 1.5)),
        )

        for connection in self.connections:
            print(f"CONFIG {connection.address}: pre-stop")
            self.gateway.assert_connected(connection.address, action="pre-stop")
            try:
                self.gateway.write_gatt(
                    connection.address,
                    MOVELLA_START_STOP_STREAM_UUID,
                    MOVELLA_STOP_HEX,
                    timeout_s=write_timeout_s,
                    without_response=True,
                )
                time.sleep(0.25)
            except Exception as exc:
                if self.gateway.is_disconnected(connection.address):
                    raise RuntimeError(
                        f"sensor disconnected before pre-stop complete address={connection.address}: {exc}"
                    ) from exc
                if "gatt_write_failed (-3)" in str(exc):
                    raise RuntimeError(
                        f"gateway lost connection before configure for address={connection.address}: {exc}"
                    ) from exc
                print(f"PRE-STOP WARNING: {connection.address}: {exc}")

        for connection in self.connections:
            print(f"CONFIG {connection.address}: subscribe")
            self.gateway.subscribe_with_retry(
                connection.address,
                MOVELLA_LONG_PAYLOAD_UUID,
                effective_subscribe_timeout_s,
                binary_notifications=True,
            )
            time.sleep(0.75)

        for connection in self.connections:
            print(f"CONFIG {connection.address}: set-rate {sampling_rate_hz}Hz")
            self.gateway.write_gatt(
                connection.address,
                MOVELLA_DEVICE_CONTROL_UUID,
                MOVELLA_SET_RATE_HEX[sampling_rate_hz],
                timeout_s=write_timeout_s,
                without_response=without_response,
            )
            time.sleep(0.25)

    def start_streams(self, *, write_timeout_s: float, without_response: bool) -> dict[str, float | None]:
        started_at: dict[str, float | None] = {}
        for connection in self.connections:
            print(f"START STREAM: {connection.address}")
            started_at[connection.address] = self.gateway.write_gatt(
                connection.address,
                MOVELLA_START_STOP_STREAM_UUID,
                MOVELLA_START_HEX,
                timeout_s=write_timeout_s,
                without_response=without_response,
            )
            time.sleep(0.02)
        return started_at

    def stop_streams(self, *, write_timeout_s: float, without_response: bool):
        print("Stopping stream now.")
        for connection in self.connections:
            print(f"STOP STREAM: {connection.address}")
            write_complete_time = self.gateway.write_gatt(
                connection.address,
                MOVELLA_START_STOP_STREAM_UUID,
                MOVELLA_STOP_HEX,
                timeout_s=write_timeout_s,
                without_response=without_response,
                allow_timeout=True,
            )
            if write_complete_time is None:
                print(f"STOP STREAM WARNING: {connection.address}: timed out waiting for write_complete")
            else:
                print(f"STOP STREAM COMPLETE: {connection.address}")
            time.sleep(0.05)

    def disconnect_all(self, timeout_s: float):
        self.gateway.disconnect(
            [connection.address for connection in self.connections],
            timeout_s=timeout_s,
            allow_timeout=True,
        )

    def set_parsed_row_writer(self, parsed_row_writer):
        self._parsed_row_writer = parsed_row_writer

    def handle_stream_frame(self, frame, *, measurement_active: bool):
        if self._parsed_row_writer is None or not measurement_active:
            return
        address = self._address_for_sensor_id(frame.sensor_id)
        packet = parse_packet(frame.payload)
        self._parsed_row_writer.write_row(
            {
                "address": address or "",
                "sensor_id": frame.sensor_id,
                "gateway_timestamp_us": frame.gateway_timestamp_us,
                **packet,
            }
        )

    def _address_for_sensor_id(self, sensor_id: int | None) -> str | None:
        if sensor_id is None:
            return None
        for connection in self.connections:
            if connection.sensor_id == sensor_id:
                return connection.address
        return None
