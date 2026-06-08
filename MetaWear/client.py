from __future__ import annotations

import json
import time

from NexusBLESdk import GatewayClient, SensorConnection, StreamFrame

from .profile import (
    METAWEAR_COMMAND_UUID,
    METAWEAR_NAME,
    METAWEAR_NOTIFY_UUID,
    configure_commands,
    parse_packet,
    select_addresses,
    start_commands,
    stop_commands,
)


class MetaWearClient:
    def __init__(self, gateway: GatewayClient):
        self.gateway = gateway
        self.connections: list[SensorConnection] = []
        self._raw_dump_file = None
        self._parsed_row_writer = None

    def discover(self, sensor_count: int, scan_timeout_ms: int) -> list[str]:
        matches = self.gateway.scan(scan_timeout_ms, name_filter=METAWEAR_NAME)
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
        if sampling_rate_hz != 100:
            raise ValueError(
                f"MetaWear first-pass profile only supports 100 Hz accel streaming, got {sampling_rate_hz}"
            )

        effective_subscribe_timeout_s = max(
            subscribe_timeout_s,
            min(20.0, 6.0 + (len(self.connections) * 1.5)),
        )

        # Put sensors into a known non-streaming state first.
        for connection in self.connections:
            print(f"CONFIG {connection.address}: pre-stop")
            self.gateway.assert_connected(connection.address, action="pre-stop")

            for payload_hex in stop_commands():
                try:
                    self.gateway.write_gatt(
                        connection.address,
                        METAWEAR_COMMAND_UUID,
                        payload_hex,
                        timeout_s=write_timeout_s,
                        without_response=without_response,
                        allow_timeout=True,
                    )
                    time.sleep(0.05)
                except Exception as exc:
                    if self.gateway.is_disconnected(connection.address):
                        raise RuntimeError(
                            f"sensor disconnected before pre-stop complete address={connection.address}: {exc}"
                        ) from exc
                    print(f"PRE-STOP WARNING: {connection.address}: payload={payload_hex}: {exc}")

            time.sleep(0.25)

        # Subscribe before configuring/starting so we do not miss early notifications.
        for connection in self.connections:
            print(f"CONFIG {connection.address}: subscribe")
            self.gateway.subscribe_with_retry(
                connection.address,
                METAWEAR_NOTIFY_UUID,
                effective_subscribe_timeout_s,
                binary_notifications=True,
            )
            time.sleep(0.75)

        # Configure accel ODR/range.
        for connection in self.connections:
            print(f"CONFIG {connection.address}: configure accel {sampling_rate_hz}Hz")
            for payload_hex in configure_commands():
                print(
                    f"CONFIG {connection.address}: write payload={payload_hex} "
                    f"without_response={without_response}"
                )
                self.gateway.write_gatt(
                    connection.address,
                    METAWEAR_COMMAND_UUID,
                    payload_hex,
                    timeout_s=write_timeout_s,
                    without_response=without_response,
                )
                time.sleep(0.05)

    def start_streams(self, *, write_timeout_s: float, without_response: bool) -> dict[str, float | None]:
        started_at: dict[str, float | None] = {}

        for connection in self.connections:
            print(f"START STREAM: {connection.address}")

            command_time: float | None = None
            for payload_hex in start_commands():
                command_time = self.gateway.write_gatt(
                    connection.address,
                    METAWEAR_COMMAND_UUID,
                    payload_hex,
                    timeout_s=write_timeout_s,
                    without_response=without_response,
                )
                time.sleep(0.02)

            started_at[connection.address] = command_time

        return started_at

    def stop_streams(self, *, write_timeout_s: float, without_response: bool):
        print("Stopping stream now.")

        for connection in self.connections:
            print(f"STOP STREAM: {connection.address}")

            for payload_hex in stop_commands():
                write_complete_time = self.gateway.write_gatt(
                    connection.address,
                    METAWEAR_COMMAND_UUID,
                    payload_hex,
                    timeout_s=write_timeout_s,
                    without_response=without_response,
                    allow_timeout=True,
                )

                if write_complete_time is None:
                    print(
                        f"STOP STREAM WARNING: {connection.address}: "
                        f"payload={payload_hex}: timed out waiting for write_complete"
                    )

                time.sleep(0.05)

            print(f"STOP STREAM COMPLETE: {connection.address}")

    def disconnect_all(self, timeout_s: float):
        self.gateway.disconnect(
            [connection.address for connection in self.connections],
            timeout_s=timeout_s,
            allow_timeout=True,
        )

    def set_raw_dump_file(self, raw_dump_file):
        self._raw_dump_file = raw_dump_file

    def set_parsed_row_writer(self, parsed_row_writer):
        self._parsed_row_writer = parsed_row_writer

    def handle_stream_frame(self, frame: StreamFrame, *, measurement_active: bool):
        address = self._address_for_sensor_id(frame.sensor_id)
        packet = parse_packet(frame.payload)
        self._dump_raw_frame(
            frame=frame,
            address=address,
            packet=packet,
        )

        if self._parsed_row_writer is None or not measurement_active:
            return

        if packet.get("kind") != "accel":
            return

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

    def _dump_raw_frame(
        self,
        *,
        frame: StreamFrame,
        address: str | None,
        packet: dict[str, float | int | str],
    ):
        if self._raw_dump_file is None:
            return

        entry = {
            "sensor_id": frame.sensor_id,
            "gateway_timestamp_us": frame.gateway_timestamp_us,
            "address": address,
            "payload_len": len(frame.payload),
            "payload_hex": frame.payload.hex(),
            **packet,
        }
        self._raw_dump_file.write(json.dumps(entry, separators=(",", ":")) + "\n")
        self._raw_dump_file.flush()
