'''
    Movesense BLE client for managing connections, configuring streams, and handling incoming data frames.
     - This client uses the NexusBLESdk to interact with Movesense sensors over BLE.
     - It provides methods for discovering sensors, connecting to them, configuring the data streams, starting and stopping the streams, and disconnecting.
     - It also handles incoming stream frames, parsing the payloads according to the Movesense profile, and can dump raw frames or write parsed rows to a specified writer.
     - The client is designed to work with ECG, heart rate, and temperature data from Movesense sensors, and can be extended to support additional stream types if needed.
'''
from __future__ import annotations

import json
import time

from NexusBLESdk import GatewayClient, SensorConnection, StreamFrame

from .profile import (
    MOVESENSE_ECG_STREAM_ID,
    MOVESENSE_HR_STREAM_ID,
    MOVESENSE_NAME_PREFIX,
    MOVESENSE_NOTIFY_UUID,
    MOVESENSE_PACKET_TYPE_DATA,
    MOVESENSE_PACKET_TYPE_GET_RESPONSE,
    MOVESENSE_SAMPLING_RATES_HZ,
    MOVESENSE_TEMP_STREAM_ID,
    MOVESENSE_WRITE_UUID,
    build_stop_command,
    build_subscribe_command,
    iter_parsed_rows,
    parse_hr_value,
    summarize_payload,
    parse_temp_value,
    select_addresses,
)


class MovesenseClient:
    def __init__(self, gateway: GatewayClient):
        self.gateway = gateway
        self.connections: list[SensorConnection] = []
        self.sampling_rate_hz = MOVESENSE_SAMPLING_RATES_HZ[0]
        self.ecg_path_suffix = "mv"
        self._raw_dump_file = None
        self._parsed_row_writer = None
        self._active_stream_ids = (
            MOVESENSE_ECG_STREAM_ID,
            MOVESENSE_HR_STREAM_ID,
            MOVESENSE_TEMP_STREAM_ID,
        )

    def discover(self, sensor_count: int, scan_timeout_ms: int) -> list[str]:
        matches = self.gateway.scan(
            scan_timeout_ms,
            name_prefix_filter=MOVESENSE_NAME_PREFIX,
        )
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
            print(f"CONFIG {connection.address}: subscribe")
            self.gateway.subscribe_with_retry(
                connection.address,
                MOVESENSE_NOTIFY_UUID,
                effective_subscribe_timeout_s,
                binary_notifications=True,
            )
            time.sleep(0.25)

        if sampling_rate_hz not in MOVESENSE_SAMPLING_RATES_HZ:
            raise ValueError(
                f"Unsupported Movesense ECG sampling rate: {sampling_rate_hz} "
                f"(supported: {MOVESENSE_SAMPLING_RATES_HZ})"
            )
        self.sampling_rate_hz = sampling_rate_hz

    def start_streams(self, *, write_timeout_s: float, without_response: bool) -> dict[str, float | None]:
        started_at: dict[str, float | None] = {}
        for connection in self.connections:
            print(f"START STREAM: {connection.address}")
            started_at[connection.address] = self.gateway.write_gatt(
                connection.address,
                MOVESENSE_WRITE_UUID,
                build_subscribe_command(
                    MOVESENSE_ECG_STREAM_ID,
                    self._build_ecg_path(),
                ),
                timeout_s=write_timeout_s,
                without_response=without_response,
            )
            time.sleep(0.05)
            self.gateway.write_gatt(
                connection.address,
                MOVESENSE_WRITE_UUID,
                build_subscribe_command(MOVESENSE_HR_STREAM_ID, "/Meas/HR"),
                timeout_s=write_timeout_s,
                without_response=without_response,
            )
            time.sleep(0.05)
            self.gateway.write_gatt(
                connection.address,
                MOVESENSE_WRITE_UUID,
                build_subscribe_command(MOVESENSE_TEMP_STREAM_ID, "/Meas/Temp"),
                timeout_s=write_timeout_s,
                without_response=without_response,
            )
            time.sleep(0.05)
        return started_at

    def stop_streams(self, *, write_timeout_s: float, without_response: bool):
        print("Stopping stream now.")
        for connection in self.connections:
            for stream_id in self._active_stream_ids:
                print(f"STOP STREAM: {connection.address} stream_id={stream_id}")
                write_complete_time = self.gateway.write_gatt(
                    connection.address,
                    MOVESENSE_WRITE_UUID,
                    build_stop_command(stream_id),
                    timeout_s=write_timeout_s,
                    without_response=without_response,
                    allow_timeout=True,
                )
                if write_complete_time is None:
                    print(
                        f"STOP STREAM WARNING: {connection.address}: "
                        f"stream_id={stream_id} timed out waiting for write_complete"
                    )
                time.sleep(0.05)

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

    def set_ecg_path_suffix(self, suffix: str):
        self.ecg_path_suffix = suffix

    def handle_stream_frame(self, frame: StreamFrame, monitor, wall_time: float):
        payload = frame.payload
        if len(payload) < 2:
            return

        packet_type = payload[0]
        stream_id = payload[1]
        address = self._address_for_sensor_id(frame.sensor_id)

        self._dump_raw_frame(
            frame=frame,
            wall_time=wall_time,
            address=address,
        )
        if monitor.measurement_active:
            self._write_parsed_rows(
                frame=frame,
                address=address,
            )

        if packet_type == MOVESENSE_PACKET_TYPE_GET_RESPONSE:
            return

        if packet_type != MOVESENSE_PACKET_TYPE_DATA:
            return

        if stream_id == MOVESENSE_ECG_STREAM_ID:
            monitor.handle_ecg_frame(frame, wall_time)
            return

        if address is None:
            return

        if stream_id == MOVESENSE_HR_STREAM_ID and parse_hr_value(payload) is not None:
            monitor.record_hr_sample(address)
        elif stream_id == MOVESENSE_TEMP_STREAM_ID and parse_temp_value(payload) is not None:
            monitor.record_temp_sample(address)

    def _address_for_sensor_id(self, sensor_id: int | None) -> str | None:
        if sensor_id is None:
            return None
        for connection in self.connections:
            if connection.sensor_id == sensor_id:
                return connection.address
        return None

    def _dump_raw_frame(self, *, frame: StreamFrame, wall_time: float, address: str | None):
        if self._raw_dump_file is None:
            return
        entry = {
            "wall_time_s": wall_time,
            "sensor_id": frame.sensor_id,
            "gateway_timestamp_us": frame.gateway_timestamp_us,
            "address": address,
        }
        entry.update(summarize_payload(frame.payload, self.sampling_rate_hz))
        self._raw_dump_file.write(json.dumps(entry, separators=(",", ":")) + "\n")
        self._raw_dump_file.flush()

    def _write_parsed_rows(self, *, frame: StreamFrame, address: str | None):
        if self._parsed_row_writer is None:
            return
        for row in iter_parsed_rows(
            frame.payload,
            sampling_rate_hz=self.sampling_rate_hz,
            address=address,
            sensor_id=frame.sensor_id,
            gateway_timestamp_us=frame.gateway_timestamp_us,
        ):
            self._parsed_row_writer.write_row(row)

    def _build_ecg_path(self) -> str:
        suffix = self.ecg_path_suffix.strip()
        if suffix:
            return f"/Meas/ECG/{self.sampling_rate_hz}/{suffix}"
        return f"/Meas/ECG/{self.sampling_rate_hz}"
