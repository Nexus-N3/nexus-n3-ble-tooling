"""CORE 2 BLE client for discovery, connection, streaming, and frame handling."""

from __future__ import annotations

import json
import time
from dataclasses import asdict

from NexusBLESdk import GatewayClient, SensorConnection, StreamFrame

from .profile import (
    BATTERY_LEVEL_UUID,
    CORE_TEMP_MEASUREMENT_UUID,
    Core2Measurement,
    parse_battery_level,
    parse_measurement,
    select_addresses,
)


class Core2Client:
    """CORE 2 client using the Nexus N3 BLE Gateway."""

    def __init__(self, gateway: GatewayClient):
        self.gateway = gateway
        self.connections: list[SensorConnection] = []

        self.battery_levels: dict[str, int | None] = {}

        self._raw_dump_file = None
        self._parsed_row_writer = None

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover(
        self,
        sensor_count: int,
        scan_timeout_ms: int,
    ) -> list[str]:
        """
        Discover CORE 2 sensors.

        The gateway scan results contain advertised service UUIDs.
        select_addresses() identifies CORE devices from the current
        Core Temp Service UUID or the legacy CORE private service UUID.
        """

        matches = self.gateway.scan(scan_timeout_ms)

        return select_addresses(
            matches,
            sensor_count,
        )

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(
        self,
        addresses: list[str],
        timeout_s: float,
    ) -> list[SensorConnection]:
        """Connect to previously discovered CORE 2 sensors."""

        self.connections = self.gateway.connect(
            addresses,
            timeout_s=timeout_s,
        )

        return self.connections

    # ------------------------------------------------------------------
    # Setup / configuration
    # ------------------------------------------------------------------

    def configure(
        self,
        *,
        read_timeout_s: float,
    ) -> dict[str, int | None]:
        """
        Perform post-connection CORE 2 setup.

        CORE 2 requires no measurement configuration before streaming.
        Battery Level is read here so it is available before the
        measurement stream starts.

        This method does not subscribe to the Core Temperature
        characteristic because enabling that subscription is itself
        the physical stream-start operation.
        """

        self.battery_levels = {}

        for connection in self.connections:
            address = connection.address

            print(f"CONFIG {address}: read battery")

            try:
                payload = self.gateway.read_gatt(
                    address,
                    BATTERY_LEVEL_UUID,
                    read_timeout_s,
                )

                battery_level = parse_battery_level(payload)

                if battery_level is None:
                    print(
                        f"BATTERY WARNING: {address}: "
                        f"invalid payload={payload.hex()}"
                    )
                else:
                    print(
                        f"BATTERY {address}: "
                        f"{battery_level}%"
                    )

                self.battery_levels[address] = battery_level

            except Exception as exc:
                # Battery status is useful but is not required to
                # acquire the CORE measurement stream.
                print(
                    f"BATTERY WARNING: {address}: "
                    f"read failed: {exc}"
                )

                self.battery_levels[address] = None

        return dict(self.battery_levels)

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def start_streams(
        self,
        *,
        subscribe_timeout_s: float,
    ) -> dict[str, float]:
        """
        Start CORE 2 measurement streaming.

        CORE 2 has no separate START command. Enabling notifications on
        the Core Body Temperature characteristic is the physical stream
        start operation.
        """

        started_at: dict[str, float] = {}

        effective_subscribe_timeout_s = max(
            subscribe_timeout_s,
            min(
                20.0,
                6.0 + (len(self.connections) * 1.5),
            ),
        )

        for connection in self.connections:
            address = connection.address

            print(f"START STREAM: {address}")

            # Notifications may begin immediately when the CCCD is
            # enabled, so capture the command time before subscribing.
            started_at[address] = time.monotonic()

            self.gateway.subscribe_with_retry(
                address,
                CORE_TEMP_MEASUREMENT_UUID,
                effective_subscribe_timeout_s,
                binary_notifications=True,
            )

            time.sleep(0.25)

        return started_at

    def stop_streams(
        self,
        *,
        unsubscribe_timeout_s: float,
    ) -> None:
        """
        Stop CORE 2 measurement streaming.

        CORE 2 has no separate STOP command. Disabling notifications on
        the Core Body Temperature characteristic is the physical stream
        stop operation.
        """

        print("Stopping stream now.")

        for connection in self.connections:
            address = connection.address

            print(f"STOP STREAM: {address}")

            self.gateway.unsubscribe(
                address,
                CORE_TEMP_MEASUREMENT_UUID,
                unsubscribe_timeout_s,
            )

            time.sleep(0.05)

    # ------------------------------------------------------------------
    # Disconnect
    # ------------------------------------------------------------------

    def disconnect_all(
        self,
        timeout_s: float,
    ) -> None:
        """Disconnect all connected CORE 2 sensors."""

        self.gateway.disconnect(
            [
                connection.address
                for connection in self.connections
            ],
            timeout_s=timeout_s,
            allow_timeout=True,
        )

    # ------------------------------------------------------------------
    # Output configuration
    # ------------------------------------------------------------------

    def set_raw_dump_file(
        self,
        raw_dump_file,
    ) -> None:
        """Set an optional JSONL file for raw frame output."""

        self._raw_dump_file = raw_dump_file

    def set_parsed_row_writer(
        self,
        parsed_row_writer,
    ) -> None:
        """Set an optional writer for parsed CORE measurements."""

        self._parsed_row_writer = parsed_row_writer

    # ------------------------------------------------------------------
    # Incoming stream frames
    # ------------------------------------------------------------------

    def handle_stream_frame(
        self,
        frame: StreamFrame,
        *,
        measurement_active: bool,
        wall_time: float,
    ) -> None:
        """
        Handle an incoming CORE 2 notification frame.

        Parsing and persistence are handled here. Stream-health
        monitoring is handled separately by GenericStreamMonitor.
        """

        address = self._address_for_sensor_id(
            frame.sensor_id
        )

        measurement = parse_measurement(
            frame.payload
        )

        # Raw frames are retained regardless of startup-gate state or
        # whether the CORE payload could be parsed successfully.
        self._dump_raw_frame(
            frame=frame,
            wall_time=wall_time,
            address=address,
            measurement=measurement,
        )

        if address is None:
            return

        # Parsed measurement output begins only after the startup gate
        # has passed.
        if (
            measurement_active
            and measurement is not None
        ):
            self._write_parsed_row(
                frame=frame,
                wall_time=wall_time,
                address=address,
                measurement=measurement,
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _address_for_sensor_id(
        self,
        sensor_id: int | None,
    ) -> str | None:
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
        wall_time: float,
        address: str | None,
        measurement: Core2Measurement | None,
    ) -> None:
        if self._raw_dump_file is None:
            return

        entry = {
            "wall_time_s": wall_time,
            "sensor_id": frame.sensor_id,
            "gateway_timestamp_us": frame.gateway_timestamp_us,
            "address": address,
            "payload_hex": frame.payload.hex(),
            "parse_ok": measurement is not None,
        }

        if measurement is not None:
            entry.update(asdict(measurement))

        self._raw_dump_file.write(
            json.dumps(
                entry,
                separators=(",", ":"),
            )
            + "\n"
        )

        self._raw_dump_file.flush()

    def _write_parsed_row(
        self,
        *,
        frame: StreamFrame,
        wall_time: float,
        address: str,
        measurement: Core2Measurement,
    ) -> None:
        if self._parsed_row_writer is None:
            return

        row = {
            "wall_time_s": wall_time,
            "gateway_timestamp_us": frame.gateway_timestamp_us,
            "sensor_id": frame.sensor_id,
            "address": address,
            **asdict(measurement),
        }

        self._parsed_row_writer.write_row(row)