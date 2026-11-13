"""Movesense ECG packet observations and individual sample mapping.

Source units stay microseconds to reuse DOT epoch/acceptance diagnostics. The
local Movesense profile defines the packet time as the FIRST sample; the LAST
sample is paired once with the gateway callback. No arrival time is assigned to
individual measurements. Callback residuals include BLE delivery effects.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict

from Movesense.profile import (
    DEFAULT_STARTUP_GATE, MOVESENSE_ECG_STREAM_ID, MOVESENSE_PACKET_TYPE_DATA,
    parse_ecg_packet_timestamp_us, parse_ecg_sample_timestamps_ms,
    parse_ecg_sample_values_mv,
)
from NexusBLESdk import GenericStreamMonitor, StartupGateConfig

from .alignment import DotExperiment, ExperimentFailure, RECORD_FIELDS
from .motion import MOTION_FIELDS

TIMESTAMP_POLICY = {
    "packet_timestamp": "uint32 little-endian milliseconds at payload offset 2; first sample",
    "semantics_evidence": "Movesense/profile.py:parse_ecg_sample_timestamps_ms explicitly defines first-sample time and derives subsequent samples",
    "vendor_specification": "https://bitbucket.org/movesense/movesense-device-lib/src/master/MovesenseCoreLib/resources/movesense-api/meas/ecg.yaml",
    "verification_scope": "Verified against authoritative local profile; running sensor firmware semantics have not been independently measured",
    "observation_anchor": "packet_timestamp_ms * 1000 + (actual_sample_count - 1) * 5000 microseconds; one observation per valid ECG notification",
    "reference": "gateway stream-frame callback timestamp in microseconds * 1000, never host arrival time",
    "sample_mapping": "accepted per-sensor model.map(first_sample_us + sample_index * 5000); no extra observations; startup rows may be unmapped",
    "amplitude": "Existing Movesense profile decoding: 64-byte sample body is 16 int32; 32-byte body is 16 int16; other whole int32 bodies use actual count; raw * 0.00038147 mV",
    "interpretation": "Clock offset and callback prediction residuals are delivery diagnostics, not independently verified ECG measurement-time errors",
}

PACKET_FIELDS = [field for field in RECORD_FIELDS
                 if field not in MOTION_FIELDS + ["raw_dot_timestamp_us", "unwrapped_dot_timestamp_us"]] + [
    "sensor_type", "packet_index", "packet_timestamp_ms", "source_anchor_us",
    "packet_sample_count", "sample_count_changed", "missing_samples_before_packet",
    "validation_error", "payload_hex",
]
SAMPLE_FIELDS = ["address", "sensor_id", "sensor_type", "connection_epoch", "stream_phase",
                 "packet_index", "sample_index", "packet_timestamp_ms", "source_sample_timestamp_us",
                 "mapped_timestamp_ns", "gateway_timestamp_us", "ecg_mv"]


def decode_ecg(payload: bytes) -> tuple[list[int], list[float]]:
    """Strict envelope validation around the existing profile's two encodings."""
    if len(payload) < 10 or payload[:2] != bytes([MOVESENSE_PACKET_TYPE_DATA, MOVESENSE_ECG_STREAM_ID]):
        raise ValueError("Not a nonempty ECG data notification")
    if (len(payload) - 6) % 4:
        raise ValueError("ECG body is not a whole supported sample array")
    times = [value * 1000 for value in parse_ecg_sample_timestamps_ms(payload, 200)]
    values = parse_ecg_sample_values_mv(payload)
    if not times or len(times) != len(values):
        raise ValueError("ECG sample count/timestamp count mismatch")
    return times, values


class EcgExperiment(DotExperiment):
    """Reuse calibration, epoch reset and pivot acceptance with packet-rate gating."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.monitor = GenericStreamMonitor(
            connections=[self.connection], labels_by_address={}, expected_rate_hz=12.5,
            timestamp_parser=parse_ecg_packet_timestamp_us, detect_gaps=False,
            startup_gate=StartupGateConfig(**DEFAULT_STARTUP_GATE), verbose=False,
        )
        self.packet_count = self.sample_count = self.malformed_count = self.ignored_count = 0
        self.sample_counts = Counter()
        self.missing_samples = self.missing_packet_gap_events = 0
        self.sample_count_changes = self.mapped_monotonicity_errors = 0
        self.source_monotonicity_errors = self.source_spacing_errors = 0
        self.previous_sample_us = self.previous_sample_count = self.previous_mapped_ns = None

    def check_deadlines(self, now):
        try:
            super().check_deadlines(now)
        except ExperimentFailure:
            self.failure = self.failure.replace("no selected-DOT frame", "no valid ECG packet")
            raise ExperimentFailure(self.failure) from None

    def source_timestamp_us(self, frame):
        return decode_ecg(frame.payload)[0][-1]

    def process(self, frame, now):
        if frame.sensor_id != self.connection.sensor_id:
            raise ValueError(f"Unexpected ECG sensor_id {frame.sensor_id}")
        # GATT subscribe acknowledgements are not timing observations.
        if len(frame.payload) >= 2 and frame.payload[:2] != bytes([2, MOVESENSE_ECG_STREAM_ID]):
            self.ignored_count += 1
            return None, []
        self.packet_count += 1
        try:
            timestamps, values = decode_ecg(frame.payload)
        except ValueError as exc:
            self.malformed_count += 1
            return {**dict.fromkeys(PACKET_FIELDS), "address": self.connection.address,
                    "sensor_id": frame.sensor_id, "sensor_type": "movesense_ecg",
                    "connection_epoch": self.epoch, "stream_phase": self.phase,
                    "packet_index": self.packet_count, "gateway_timestamp_us": frame.gateway_timestamp_us,
                    "reference_time_ns": frame.gateway_timestamp_us * 1000,
                    "model_observation_supplied": False, "model_state": self.model.state.value,
                    "validation_error": str(exc), "payload_hex": frame.payload.hex()}, []
        changed = self.previous_sample_count is not None and len(values) != self.previous_sample_count
        missing = 0
        if self.previous_sample_us is not None:
            delta = timestamps[0] - self.previous_sample_us
            if delta <= 0 or delta % 5000:
                self.source_monotonicity_errors += int(delta <= 0)
                self.source_spacing_errors += int(delta % 5000 != 0)
                # Force the existing conservative epoch reset; do not feed overlap,
                # duplicate or off-grid source samples to the model.
                self.previous_dot = timestamps[-1]
            elif delta > 5000:
                missing = delta // 5000 - 1
                self.missing_samples += missing
                self.missing_packet_gap_events += 1
                stats = self.monitor.stats_by_address[self.connection.address]
                if (not self.monitor.measurement_active and
                        stats.startup_gate_first_sensor_timestamp is not None):
                    stats.startup_gate_gap_events += 1
        row = super().process(frame, now)
        row.pop("raw_dot_timestamp_us")
        row.pop("unwrapped_dot_timestamp_us")
        row.update(sensor_type="movesense_ecg", packet_index=self.packet_count,
                   packet_timestamp_ms=timestamps[0] // 1000, source_anchor_us=timestamps[-1],
                   packet_sample_count=len(values), sample_count_changed=changed,
                   missing_samples_before_packet=missing, validation_error="", payload_hex=frame.payload.hex(),
                   connection_interval_us=None)
        samples = []
        for index, (timestamp, value) in enumerate(zip(timestamps, values)):
            mapped = self.model.map(timestamp) if row["model_observation_supplied"] else None
            if mapped is not None:
                if self.previous_mapped_ns is not None and mapped <= self.previous_mapped_ns:
                    self.mapped_monotonicity_errors += 1
                self.previous_mapped_ns = mapped
            samples.append({"address": self.connection.address, "sensor_id": frame.sensor_id,
                            "sensor_type": "movesense_ecg", "connection_epoch": self.epoch,
                            "stream_phase": row["stream_phase"], "packet_index": self.packet_count,
                            "sample_index": index, "packet_timestamp_ms": timestamps[0] // 1000,
                            "source_sample_timestamp_us": timestamp, "mapped_timestamp_ns": mapped,
                            "gateway_timestamp_us": frame.gateway_timestamp_us, "ecg_mv": value})
        self.sample_count += len(samples)
        self.sample_counts[len(samples)] += 1
        self.sample_count_changes += int(changed)
        self.previous_sample_us, self.previous_sample_count = timestamps[-1], len(values)
        return row, samples

    def summary(self):
        return {**super().summary(), "sensor_type": "movesense_ecg",
                "ecg_packet_notifications": self.packet_count,
                "ecg_valid_packets": self.packet_count - self.malformed_count,
                "ecg_samples": self.sample_count, "malformed_packets": self.malformed_count,
                "ignored_notifications": self.ignored_count, "sample_count_histogram": dict(self.sample_counts),
                "sample_count_changes": self.sample_count_changes,
                "missing_samples_inferred": self.missing_samples,
                "missing_packet_gap_events": self.missing_packet_gap_events,
                "missing_packet_count_estimate_at_16_samples": self.missing_samples / 16,
                "missing_count_scope": "Interior source-time gaps only; absent packets' sample counts and leading/trailing losses are unknown",
                "mapped_sample_monotonicity_errors": self.mapped_monotonicity_errors,
                "source_sample_monotonicity_errors": self.source_monotonicity_errors,
                "source_sample_spacing_errors": self.source_spacing_errors,
                "model_reset_count": self.epoch - 1,
                "stability_gate": asdict(self.monitor.startup_gate)}
