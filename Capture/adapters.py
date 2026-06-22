from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from NexusBLESdk import CsvRowWriter, GenericStreamMonitor, StartupGateConfig, StreamFrame

from MetaWear.client import MetaWearClient
from MetaWear.profile import (
    DEFAULT_LOCATIONS as METAWEAR_LOCATIONS,
    DEFAULT_STARTUP_GATE as METAWEAR_STARTUP_GATE,
    METAWEAR_ACCEL_ODR_HZ,
)
from MovellaDot.client import MovellaDotClient
from MovellaDot.profile import (
    DEFAULT_LOCATIONS as MOVELLA_LOCATIONS,
    DEFAULT_STARTUP_GATE as MOVELLA_STARTUP_GATE,
    MOVELLA_DEVICE_CONTROL_UUID,
    MOVELLA_LONG_PAYLOAD_UUID,
    MOVELLA_SET_RATE_HEX,
    parse_sensor_timestamp as parse_movella_timestamp,
)
from Movesense.client import MovesenseClient
from Movesense.monitor import MovesenseMonitorAdapter
from Movesense.profile import (
    DEFAULT_LOCATIONS as MOVESENSE_LOCATIONS,
    DEFAULT_STARTUP_GATE as MOVESENSE_STARTUP_GATE,
    MOVESENSE_ECG_SAMPLES_PER_PACKET,
    MOVESENSE_SAMPLING_RATES_HZ,
    parse_ecg_packet_timestamp_us,
)
from NexusN3Dot.client import NexusN3DotClient
from NexusN3Dot.profile import (
    DEFAULT_LOCATIONS as NEXUS_N3_LOCATIONS,
    DEFAULT_STARTUP_GATE as NEXUS_N3_STARTUP_GATE,
    NEXUS_N3_DOT_SET_ODR_HEX,
    parse_sensor_timestamp as parse_n3_timestamp,
)


def parse_frame_gateway_timestamp(frame: StreamFrame) -> int:
    return frame.gateway_timestamp_us


@dataclass(frozen=True)
class SensorSpec:
    sensor_type: str
    display_name: str
    sampling_rates_hz: tuple[int, ...]
    default_sampling_rate_hz: int
    default_locations: tuple[str, ...]
    startup_gate_defaults: dict[str, Any]
    supports_identify: bool = False
    max_sensor_count: int | None = None


class CaptureAdapter:
    spec: SensorSpec

    def __init__(self, gateway):
        self.gateway = gateway
        self.connections = []
        self._parsed_row_writer = None
        self._parsed_output_path: Path | None = None

    @property
    def output_files(self) -> list[str]:
        if self._parsed_output_path is None:
            return []
        return [str(self._parsed_output_path)]

    def discover(self, sensor_count: int, scan_timeout_ms: int) -> list[str]:
        raise NotImplementedError

    def connect(self, addresses: list[str], timeout_s: float):
        raise NotImplementedError

    def configure(
        self,
        *,
        sampling_rate_hz: int,
        subscribe_timeout_s: float,
        write_timeout_s: float,
        without_response: bool,
    ) -> None:
        raise NotImplementedError

    def create_monitor(
        self,
        *,
        labels_by_address: dict[str, str | None],
        sampling_rate_hz: int,
        startup_gate_config: StartupGateConfig,
    ):
        raise NotImplementedError

    def start_streams(self, *, write_timeout_s: float, without_response: bool) -> dict[str, float | None]:
        raise NotImplementedError

    def stop_streams(self, *, write_timeout_s: float, without_response: bool) -> None:
        raise NotImplementedError

    def disconnect_all(self, timeout_s: float) -> None:
        raise NotImplementedError

    def handle_stream_frame(self, frame: StreamFrame, *, monitor, wall_time: float) -> None:
        raise NotImplementedError

    def close(self) -> None:
        if self._parsed_row_writer is not None:
            self._parsed_row_writer.close()
            self._parsed_row_writer = None

    def identify_sensor(
        self,
        address: str,
        *,
        read_timeout_s: float,
        write_timeout_s: float,
        without_response: bool,
    ) -> None:
        raise RuntimeError(f"Identify is not supported for {self.spec.display_name}")

    def collect_post_capture_details(self, *, timeout_s: float) -> dict[str, Any]:
        return {}


class MovellaCaptureAdapter(CaptureAdapter):
    spec = SensorSpec(
        sensor_type="movelladot",
        display_name="Movella DOT",
        sampling_rates_hz=tuple(sorted(MOVELLA_SET_RATE_HEX)),
        default_sampling_rate_hz=60,
        default_locations=tuple(MOVELLA_LOCATIONS),
        startup_gate_defaults=dict(MOVELLA_STARTUP_GATE),
        supports_identify=True,
    )

    def __init__(self, gateway):
        super().__init__(gateway)
        self.client = MovellaDotClient(gateway)

    def discover(self, sensor_count: int, scan_timeout_ms: int) -> list[str]:
        return self.client.discover(sensor_count, scan_timeout_ms)

    def connect(self, addresses: list[str], timeout_s: float):
        self.connections = self.client.connect(addresses, timeout_s=timeout_s)
        return self.connections

    def configure(self, *, sampling_rate_hz: int, subscribe_timeout_s: float, write_timeout_s: float, without_response: bool) -> None:
        self.client.configure(
            sampling_rate_hz=sampling_rate_hz,
            subscribe_timeout_s=subscribe_timeout_s,
            write_timeout_s=write_timeout_s,
            without_response=without_response,
        )

    def attach_output(self, session_dir: Path) -> None:
        self._parsed_output_path = session_dir / "movella_dot_stream.csv"
        self._parsed_row_writer = CsvRowWriter(
            self._parsed_output_path,
            [
                "address",
                "sensor_id",
                "gateway_timestamp_us",
                "timestamp_us",
                "quat_w",
                "quat_x",
                "quat_y",
                "quat_z",
                "accel_x",
                "accel_y",
                "accel_z",
                "gyro_x",
                "gyro_y",
                "gyro_z",
            ],
        )
        self.client.set_parsed_row_writer(self._parsed_row_writer)

    def create_monitor(self, *, labels_by_address: dict[str, str | None], sampling_rate_hz: int, startup_gate_config: StartupGateConfig):
        return GenericStreamMonitor(
            connections=self.connections,
            labels_by_address=labels_by_address,
            expected_rate_hz=sampling_rate_hz,
            timestamp_parser=parse_movella_timestamp,
            startup_gate=startup_gate_config,
            verbose=True,
        )

    def start_streams(self, *, write_timeout_s: float, without_response: bool) -> dict[str, float | None]:
        return self.client.start_streams(write_timeout_s=write_timeout_s, without_response=without_response)

    def stop_streams(self, *, write_timeout_s: float, without_response: bool) -> None:
        self.client.stop_streams(write_timeout_s=write_timeout_s, without_response=without_response)

    def disconnect_all(self, timeout_s: float) -> None:
        self.client.disconnect_all(timeout_s=timeout_s)

    def handle_stream_frame(self, frame: StreamFrame, *, monitor, wall_time: float) -> None:
        self.client.handle_stream_frame(frame, measurement_active=monitor.measurement_active)

    def identify_sensor(self, address: str, *, read_timeout_s: float, write_timeout_s: float, without_response: bool) -> None:
        payload = self.gateway.read_gatt(address, MOVELLA_DEVICE_CONTROL_UUID, timeout_s=read_timeout_s)
        current_hex = payload.hex()
        if len(current_hex) < 6:
            raise RuntimeError(f"Device control read too short for identify on {address}: {current_hex}")
        identify_hex = "010102" + current_hex[6:]
        self.gateway.write_gatt(
            address,
            MOVELLA_DEVICE_CONTROL_UUID,
            identify_hex,
            timeout_s=write_timeout_s,
            without_response=without_response,
        )


class NexusN3CaptureAdapter(CaptureAdapter):
    spec = SensorSpec(
        sensor_type="nexusn3dot",
        display_name="Nexus N3 Dot",
        sampling_rates_hz=tuple(sorted(NEXUS_N3_DOT_SET_ODR_HEX)),
        default_sampling_rate_hz=100,
        default_locations=tuple(NEXUS_N3_LOCATIONS),
        startup_gate_defaults=dict(NEXUS_N3_STARTUP_GATE),
    )

    def __init__(self, gateway):
        super().__init__(gateway)
        self.client = NexusN3DotClient(gateway)

    def discover(self, sensor_count: int, scan_timeout_ms: int) -> list[str]:
        return self.client.discover(sensor_count, scan_timeout_ms)

    def connect(self, addresses: list[str], timeout_s: float):
        self.connections = self.client.connect(addresses, timeout_s=timeout_s)
        return self.connections

    def configure(self, *, sampling_rate_hz: int, subscribe_timeout_s: float, write_timeout_s: float, without_response: bool) -> None:
        self.client.configure(
            sampling_rate_hz=sampling_rate_hz,
            subscribe_timeout_s=subscribe_timeout_s,
            write_timeout_s=write_timeout_s,
            without_response=without_response,
        )

    def attach_output(self, session_dir: Path) -> None:
        self._parsed_output_path = session_dir / "nexus_n3_dot_stream.csv"
        self._parsed_row_writer = CsvRowWriter(
            self._parsed_output_path,
            [
                "address",
                "sensor_id",
                "gateway_timestamp_us",
                "version",
                "flags",
                "sequence",
                "timestamp_us",
                "accel_x_mg",
                "accel_y_mg",
                "accel_z_mg",
                "gyro_x_mdps",
                "gyro_y_mdps",
                "gyro_z_mdps",
            ],
        )
        self.client.set_parsed_row_writer(self._parsed_row_writer)

    def create_monitor(self, *, labels_by_address: dict[str, str | None], sampling_rate_hz: int, startup_gate_config: StartupGateConfig):
        return GenericStreamMonitor(
            connections=self.connections,
            labels_by_address=labels_by_address,
            expected_rate_hz=sampling_rate_hz,
            timestamp_parser=parse_n3_timestamp,
            startup_gate=startup_gate_config,
            verbose=True,
        )

    def start_streams(self, *, write_timeout_s: float, without_response: bool) -> dict[str, float | None]:
        return self.client.start_streams(write_timeout_s=write_timeout_s, without_response=without_response)

    def stop_streams(self, *, write_timeout_s: float, without_response: bool) -> None:
        self.client.stop_streams(write_timeout_s=write_timeout_s, without_response=without_response)

    def disconnect_all(self, timeout_s: float) -> None:
        self.client.disconnect_all(timeout_s=timeout_s)

    def handle_stream_frame(self, frame: StreamFrame, *, monitor, wall_time: float) -> None:
        self.client.handle_stream_frame(frame, measurement_active=monitor.measurement_active)

    def collect_post_capture_details(self, *, timeout_s: float) -> dict[str, Any]:
        try:
            return {"device_status_by_address": self.client.read_device_status_all(timeout_s=timeout_s)}
        except (TimeoutError, RuntimeError, ValueError) as exc:
            return {"device_status_warning": str(exc)}


class MovesenseCaptureAdapter(CaptureAdapter):
    spec = SensorSpec(
        sensor_type="movesense",
        display_name="Movesense",
        sampling_rates_hz=tuple(sorted(MOVESENSE_SAMPLING_RATES_HZ)),
        default_sampling_rate_hz=MOVESENSE_SAMPLING_RATES_HZ[0],
        default_locations=tuple(MOVESENSE_LOCATIONS),
        startup_gate_defaults=dict(MOVESENSE_STARTUP_GATE),
        max_sensor_count=1,
    )

    def __init__(self, gateway):
        super().__init__(gateway)
        self.client = MovesenseClient(gateway)
        self.monitor = None

    def discover(self, sensor_count: int, scan_timeout_ms: int) -> list[str]:
        if sensor_count != 1:
            raise RuntimeError("Movesense capture currently supports exactly 1 sensor.")
        return self.client.discover(sensor_count, scan_timeout_ms)

    def connect(self, addresses: list[str], timeout_s: float):
        self.connections = self.client.connect(addresses, timeout_s=timeout_s)
        return self.connections

    def configure(self, *, sampling_rate_hz: int, subscribe_timeout_s: float, write_timeout_s: float, without_response: bool) -> None:
        self.client.configure(
            sampling_rate_hz=sampling_rate_hz,
            subscribe_timeout_s=subscribe_timeout_s,
            write_timeout_s=write_timeout_s,
            without_response=without_response,
        )

    def attach_output(self, session_dir: Path) -> None:
        self._parsed_output_path = session_dir / "movesense_stream.csv"
        self._parsed_row_writer = CsvRowWriter(
            self._parsed_output_path,
            [
                "address",
                "sensor_id",
                "stream",
                "timestamp_ms",
                "gateway_timestamp_us",
                "packet_timestamp_ms",
                "sample_index",
                "sampling_rate_hz",
                "value",
                "unit",
            ],
        )
        self.client.set_parsed_row_writer(self._parsed_row_writer)

    def create_monitor(self, *, labels_by_address: dict[str, str | None], sampling_rate_hz: int, startup_gate_config: StartupGateConfig):
        packet_rate_hz = sampling_rate_hz / MOVESENSE_ECG_SAMPLES_PER_PACKET
        base_monitor = GenericStreamMonitor(
            connections=self.connections,
            labels_by_address=labels_by_address,
            expected_rate_hz=int(packet_rate_hz) if packet_rate_hz.is_integer() else packet_rate_hz,
            timestamp_parser=parse_ecg_packet_timestamp_us,
            startup_gate=startup_gate_config,
            verbose=True,
        )
        self.monitor = MovesenseMonitorAdapter(base_monitor)
        return self.monitor

    def start_streams(self, *, write_timeout_s: float, without_response: bool) -> dict[str, float | None]:
        return self.client.start_streams(write_timeout_s=write_timeout_s, without_response=without_response)

    def stop_streams(self, *, write_timeout_s: float, without_response: bool) -> None:
        self.client.stop_streams(write_timeout_s=write_timeout_s, without_response=without_response)

    def disconnect_all(self, timeout_s: float) -> None:
        self.client.disconnect_all(timeout_s=timeout_s)

    def handle_stream_frame(self, frame: StreamFrame, *, monitor, wall_time: float) -> None:
        self.client.handle_stream_frame(frame, monitor, wall_time)


class MetaWearCaptureAdapter(CaptureAdapter):
    spec = SensorSpec(
        sensor_type="metawear",
        display_name="MetaWear",
        sampling_rates_hz=(METAWEAR_ACCEL_ODR_HZ,),
        default_sampling_rate_hz=METAWEAR_ACCEL_ODR_HZ,
        default_locations=tuple(METAWEAR_LOCATIONS),
        startup_gate_defaults=dict(METAWEAR_STARTUP_GATE),
    )

    def __init__(self, gateway):
        super().__init__(gateway)
        self.client = MetaWearClient(gateway)
        self._raw_dump_file = None
        self._raw_output_path: Path | None = None

    @property
    def output_files(self) -> list[str]:
        paths = super().output_files
        if self._raw_output_path is not None:
            paths.append(str(self._raw_output_path))
        return paths

    def discover(self, sensor_count: int, scan_timeout_ms: int) -> list[str]:
        return self.client.discover(sensor_count, scan_timeout_ms)

    def connect(self, addresses: list[str], timeout_s: float):
        self.connections = self.client.connect(addresses, timeout_s=timeout_s)
        return self.connections

    def configure(self, *, sampling_rate_hz: int, subscribe_timeout_s: float, write_timeout_s: float, without_response: bool) -> None:
        self.client.configure(
            sampling_rate_hz=sampling_rate_hz,
            subscribe_timeout_s=subscribe_timeout_s,
            write_timeout_s=write_timeout_s,
            without_response=without_response,
        )

    def attach_output(self, session_dir: Path) -> None:
        self._parsed_output_path = session_dir / "metawear_accel_stream.csv"
        self._parsed_row_writer = CsvRowWriter(
            self._parsed_output_path,
            [
                "address",
                "sensor_id",
                "gateway_timestamp_us",
                "kind",
                "accel_x",
                "accel_y",
                "accel_z",
                "accel_x_raw",
                "accel_y_raw",
                "accel_z_raw",
            ],
        )
        self.client.set_parsed_row_writer(self._parsed_row_writer)
        self._raw_output_path = session_dir / "metawear_raw.jsonl"
        self._raw_dump_file = self._raw_output_path.open("w", encoding="utf-8")
        self.client.set_raw_dump_file(self._raw_dump_file)

    def create_monitor(self, *, labels_by_address: dict[str, str | None], sampling_rate_hz: int, startup_gate_config: StartupGateConfig):
        return GenericStreamMonitor(
            connections=self.connections,
            labels_by_address=labels_by_address,
            expected_rate_hz=sampling_rate_hz,
            timestamp_source=parse_frame_gateway_timestamp,
            detect_gaps=False,
            startup_gate=startup_gate_config,
            verbose=True,
        )

    def start_streams(self, *, write_timeout_s: float, without_response: bool) -> dict[str, float | None]:
        return self.client.start_streams(write_timeout_s=write_timeout_s, without_response=without_response)

    def stop_streams(self, *, write_timeout_s: float, without_response: bool) -> None:
        self.client.stop_streams(write_timeout_s=write_timeout_s, without_response=without_response)

    def disconnect_all(self, timeout_s: float) -> None:
        self.client.disconnect_all(timeout_s=timeout_s)

    def handle_stream_frame(self, frame: StreamFrame, *, monitor, wall_time: float) -> None:
        self.client.handle_stream_frame(frame, measurement_active=monitor.measurement_active)

    def close(self) -> None:
        super().close()
        if self._raw_dump_file is not None:
            self._raw_dump_file.close()
            self._raw_dump_file = None


SENSOR_SPECS = {
    "movelladot": MovellaCaptureAdapter.spec,
    "nexusn3dot": NexusN3CaptureAdapter.spec,
    "movesense": MovesenseCaptureAdapter.spec,
    "metawear": MetaWearCaptureAdapter.spec,
}

ADAPTER_TYPES = {
    "movelladot": MovellaCaptureAdapter,
    "nexusn3dot": NexusN3CaptureAdapter,
    "movesense": MovesenseCaptureAdapter,
    "metawear": MetaWearCaptureAdapter,
}


def normalize_sensor_type(sensor_type: str) -> str:
    return sensor_type.strip().lower().replace("_", "").replace("-", "")


def get_sensor_spec(sensor_type: str) -> SensorSpec:
    normalized = normalize_sensor_type(sensor_type)
    if normalized not in SENSOR_SPECS:
        raise KeyError(f"Unsupported sensor type: {sensor_type}")
    return SENSOR_SPECS[normalized]


def create_adapter(sensor_type: str, gateway) -> CaptureAdapter:
    normalized = normalize_sensor_type(sensor_type)
    if normalized not in ADAPTER_TYPES:
        raise KeyError(f"Unsupported sensor type: {sensor_type}")
    return ADAPTER_TYPES[normalized](gateway)
