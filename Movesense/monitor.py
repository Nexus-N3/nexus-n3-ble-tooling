from __future__ import annotations

from dataclasses import dataclass
import time

from NexusBLESdk import GenericStreamMonitor, StreamFrame

from .profile import parse_ecg_packet_sample_count


@dataclass
class MovesenseStreamStats:
    ecg_packets_received: int = 0
    ecg_samples_received: int = 0
    hr_samples_received: int = 0
    temp_samples_received: int = 0


class MovesenseMonitorAdapter:
    def __init__(self, base_monitor: GenericStreamMonitor):
        self.base_monitor = base_monitor
        self.stats_by_address = {
            address: MovesenseStreamStats()
            for address in base_monitor.stats_by_address
        }

    @property
    def measurement_active(self) -> bool:
        return self.base_monitor.measurement_active

    def mark_stream_started(self, address: str, command_time: float | None):
        self.base_monitor.mark_stream_started(address, command_time)

    def announce_startup_state(self):
        self.base_monitor.announce_startup_state()

    def evaluate_startup_stability(self) -> tuple[bool, list[str]]:
        return self.base_monitor.evaluate_startup_stability()

    def activate_measurement(self):
        self.base_monitor.activate_measurement()

    def handle_ecg_frame(self, frame: StreamFrame, wall_time: float):
        address = self.base_monitor.address_by_sensor_id.get(frame.sensor_id)
        if address in self.stats_by_address:
            stats = self.stats_by_address[address]
            stats.ecg_packets_received += 1
            stats.ecg_samples_received += parse_ecg_packet_sample_count(frame.payload)
        self.base_monitor.handle_stream_frame(frame, wall_time)

    def record_hr_sample(self, address: str):
        if address in self.stats_by_address:
            self.stats_by_address[address].hr_samples_received += 1

    def record_temp_sample(self, address: str):
        if address in self.stats_by_address:
            self.stats_by_address[address].temp_samples_received += 1

    def drain_after_stop(
        self,
        gateway_client,
        *,
        frame_handler,
        quiet_window_s: float = 0.35,
        max_drain_s: float = 2.0,
    ):
        self.base_monitor._log(
            "Draining post-stop stream tail: "
            f"quiet_window={quiet_window_s:.2f}s max_drain={max_drain_s:.2f}s."
        )

        drain_deadline = time.monotonic() + max_drain_s
        quiet_deadline = time.monotonic() + quiet_window_s

        while time.monotonic() < drain_deadline:
            remaining_quiet = quiet_deadline - time.monotonic()
            if remaining_quiet <= 0:
                return

            try:
                item_type, item = gateway_client.read_item(timeout_s=max(0.01, min(0.1, remaining_quiet)))
            except TimeoutError:
                continue

            if item_type == "stream_frame":
                self.base_monitor.post_stop_drain_frames += 1
                quiet_deadline = time.monotonic() + quiet_window_s
                address = self.base_monitor.address_by_sensor_id.get(item.sensor_id)
                if address is None:
                    self.base_monitor.post_stop_drain_unknown_sensor_ids[item.sensor_id] += 1
                else:
                    self.base_monitor.post_stop_drain_by_address[address] += 1
                frame_handler(item, time.monotonic())
                continue

            if item.get("type") == "sensor_disconnected":
                raise RuntimeError(
                    f"Unexpected disconnect during post-stop drain: {item.get('address')} reason={item.get('reason')}"
                )

        self.base_monitor._log("Post-stop drain reached max_drain timeout.")

    def summary_lines(self, gateway_client) -> list[str]:
        lines = list(self.base_monitor.summary_lines(gateway_client))
        for address in sorted(self.stats_by_address):
            stats = self.stats_by_address[address]
            lines.append(
                f"{address} movesense_ecg_packets={stats.ecg_packets_received} "
                f"movesense_ecg_samples={stats.ecg_samples_received} "
                f"movesense_hr_samples={stats.hr_samples_received} "
                f"movesense_temp_samples={stats.temp_samples_received}"
            )
        return lines
