from __future__ import annotations

import time

from NexusBLESdk import GatewayClient, SensorConnection

from .profile import (
    NEXUS_N3_HDR_DOT_CONTROL_COMMAND_UUID,
    NEXUS_N3_HDR_DOT_DEVICE_STATUS_UUID,
    NEXUS_N3_HDR_DOT_IMU_MEASUREMENT_UUID,
    NEXUS_N3_HDR_DOT_NAME,
    NEXUS_N3_HDR_DOT_START_HEX,
    NEXUS_N3_HDR_DOT_STOP_HEX,
    build_set_stream_mode_command,
    parse_device_status,
    parse_packet,
    select_addresses,
)


class NexusN3HDRDotClient:
    def __init__(self, gateway: GatewayClient):
        self.gateway = gateway
        self.connections: list[SensorConnection] = []
        self._parsed_row_writer = None
        self.stream_mode = "MAG"

    def discover(self, sensor_count: int, scan_timeout_ms: int) -> list[str]:
        matches = self.gateway.scan(scan_timeout_ms, name_filter=NEXUS_N3_HDR_DOT_NAME)
        return select_addresses(matches, sensor_count)

    def connect(self, addresses: list[str], timeout_s: float) -> list[SensorConnection]:
        self.connections = self.gateway.connect(addresses, timeout_s=timeout_s)
        return self.connections

    def configure(
        self,
        *,
        stream_mode: str,
        subscribe_timeout_s: float,
        write_timeout_s: float,
        without_response: bool,
    ):
        self.stream_mode = stream_mode.upper()

        effective_subscribe_timeout_s = max(
            subscribe_timeout_s,
            min(20.0, 6.0 + (len(self.connections) * 1.5)),
        )

        for connection in self.connections:
            print(f"CONFIG {connection.address}: pre-stop")
            self.gateway.assert_connected(connection.address, action="pre-stop")
            self.gateway.write_gatt(
                connection.address,
                NEXUS_N3_HDR_DOT_CONTROL_COMMAND_UUID,
                NEXUS_N3_HDR_DOT_STOP_HEX,
                timeout_s=write_timeout_s,
                without_response=False,
            )
            time.sleep(0.5)

            print(f"CONFIG {connection.address}: subscribe")
            self.gateway.subscribe_with_retry(
                connection.address,
                NEXUS_N3_HDR_DOT_IMU_MEASUREMENT_UUID,
                effective_subscribe_timeout_s,
                binary_notifications=True,
            )
            time.sleep(0.75)

            payload = build_set_stream_mode_command(self.stream_mode)
            payload_hex = payload.hex()

            print(
                f"CONFIG {connection.address}: set-stream-mode {self.stream_mode} "
                f"payload={payload_hex} without_response={without_response}"
            )
            self.gateway.write_gatt(
                connection.address,
                NEXUS_N3_HDR_DOT_CONTROL_COMMAND_UUID,
                payload_hex,
                timeout_s=write_timeout_s,
                without_response=without_response,
            )
            time.sleep(0.5)

    def start_streams(self, *, write_timeout_s: float, without_response: bool) -> dict[str, float | None]:
        started_at: dict[str, float | None] = {}

        for connection in self.connections:
            print(f"START STREAM: {connection.address}")
            started_at[connection.address] = self._send_start_command(
                connection.address,
                without_response=without_response,
            )
            time.sleep(0.02)

        return started_at

    def stop_streams(self, *, write_timeout_s: float, without_response: bool):
        print("Stopping stream now.")

        for connection in self.connections:
            print(f"STOP STREAM: {connection.address}")
            self._send_control_command(
                connection.address,
                NEXUS_N3_HDR_DOT_STOP_HEX,
                without_response=without_response,
            )
            print(f"STOP STREAM COMPLETE: {connection.address}")
            time.sleep(0.05)

    def disconnect_all(self, timeout_s: float):
        self.gateway.disconnect(
            [connection.address for connection in self.connections],
            timeout_s=timeout_s,
            allow_timeout=True,
        )

    def read_device_status_all(self, *, timeout_s: float = 5.0) -> dict[str, dict]:
        results: dict[str, dict] = {}

        for connection in self.connections:
            payload = self.gateway.read_gatt(
                connection.address,
                NEXUS_N3_HDR_DOT_DEVICE_STATUS_UUID,
                timeout_s=timeout_s,
            )
            results[connection.address] = parse_device_status(payload)

        return results

    def set_parsed_row_writer(self, parsed_row_writer):
        self._parsed_row_writer = parsed_row_writer

    def handle_stream_frame(self, frame, *, measurement_active: bool):
        if self._parsed_row_writer is None or not measurement_active:
            return

        address = self._address_for_sensor_id(frame.sensor_id)
        packet = parse_packet(frame.payload, stream_mode=self.stream_mode)

        for sample_index, sample in enumerate(packet["samples"]):
            self._parsed_row_writer.write_row(
                {
                    "address": address or "",
                    "sensor_id": frame.sensor_id,
                    "gateway_timestamp_us": frame.gateway_timestamp_us,
                    "stream_mode": packet["stream_mode"],
                    "sample_index": sample_index,
                    "samples_in_notification": packet["sample_count"],
                    "payload_bytes": packet["payload_bytes"],
                    **sample,
                }
            )

    def _send_start_command(self, address: str, *, without_response: bool) -> float:
        return self.gateway.write_gatt_nowait(
            address,
            NEXUS_N3_HDR_DOT_CONTROL_COMMAND_UUID,
            NEXUS_N3_HDR_DOT_START_HEX,
            without_response=without_response,
        )

    def _send_control_command(self, address: str, payload_hex: str, *, without_response: bool) -> float:
        return self.gateway.write_gatt_nowait(
            address,
            NEXUS_N3_HDR_DOT_CONTROL_COMMAND_UUID,
            payload_hex,
            without_response=without_response,
        )

    def _address_for_sensor_id(self, sensor_id: int | None) -> str | None:
        if sensor_id is None:
            return None

        for connection in self.connections:
            if connection.sensor_id == sensor_id:
                return connection.address

        return None