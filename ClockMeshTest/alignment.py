"""Experiment phases and evidence, independent of serial I/O."""
from __future__ import annotations

from dataclasses import asdict

from clock_mesh import AffineClockModel, ClockAlignmentConfig, ClockModelState
from MovellaDot.profile import parse_sensor_timestamp
from NexusBLESdk import GenericStreamMonitor, SensorConnection, StartupGateConfig, StreamFrame

from .candidates import CANDIDATE_FIELDS, observe_with_candidate


class ExperimentFailure(RuntimeError):
    """An experiment could not complete within its continuity/timing policy."""


RECORD_FIELDS = [
    "address", "sensor_id", "connection_epoch", "stream_phase", "continuity",
    "raw_dot_timestamp_us", "unwrapped_dot_timestamp_us", "gateway_timestamp_us",
    "reference_time_ns", "mapped_timestamp_ns", "model_observation_supplied",
    "model_state", "estimated_scale_ns_per_source_unit", "latest_prediction_error_ns",
    "mean_prediction_error_ns", "prediction_error_stddev_ns", "observation_count",
    "retained_calibration_observation_count", "retained_tracking_error_count",
    "host_elapsed_seconds", "calibration_elapsed_seconds", "connection_interval_us",
] + CANDIDATE_FIELDS


class DotExperiment:
    """One model for one connection; stop conservatively on ambiguous continuity.

    Host monotonic time is used only for watchdogs, never as model reference time.
    No nominal source scale is supplied to ClockMesh.
    """

    def __init__(self, connection: SensorConnection, *, started_at: float,
                 calibration_config: ClockAlignmentConfig, no_data_seconds: float):
        self.connection = connection
        self.started_at = started_at
        self.last_data_at = started_at
        self.no_data_seconds = no_data_seconds
        self.config = calibration_config
        self.model = AffineClockModel(calibration_config)
        self.epoch = 1
        self.phase = "acquisition"
        self.failure: str | None = None
        self.stability_outcome = "pending"
        self.calibration_outcome = "not_started"
        self.calibration_started_at: float | None = None
        self.calibration_reference_ns: int | None = None
        self.calibration_duration_seconds: float | None = None
        self.calibration_reference_span_seconds: float | None = None
        self.tracking_error_count = 0
        self.previous_dot: int | None = None
        self.previous_gateway: int | None = None
        self.monitor = GenericStreamMonitor(
            connections=[connection], labels_by_address={}, expected_rate_hz=60,
            timestamp_parser=parse_sensor_timestamp, startup_gate=StartupGateConfig(),
            verbose=False,
        )
        self.ended_epochs: list[dict] = []

    def _calibration_elapsed(self, now: float) -> float | None:
        if self.calibration_started_at is None:
            return None
        return now - self.calibration_started_at

    def check_deadlines(self, now: float) -> None:
        if self.failure:
            raise ExperimentFailure(self.failure)
        if now - self.last_data_at >= self.no_data_seconds:
            self.failure = "No-data timeout: no selected-DOT frame received"
        elif (self.stability_outcome == "pending" and
              now - self.started_at >= self.monitor.startup_gate.stability_window_seconds):
            _, reasons = self.monitor.evaluate_startup_stability()
            self.stability_outcome = "failed"
            self.failure = "Stability gate deadline exceeded: " + ", ".join(reasons)
        elif (self.calibration_outcome == "pending" and
              self._calibration_elapsed(now) >= self.config.calibration_deadline_ns / 1e9):
            self.calibration_outcome = "failed"
            self.calibration_duration_seconds = self._calibration_elapsed(now)
            self.failure = "Calibration deadline exceeded before alignment"
        if self.failure:
            if self.stability_outcome == "pending":
                self.stability_outcome = "failed"
            if self.calibration_outcome == "pending":
                self.calibration_outcome = "failed"
                self.calibration_duration_seconds = self._calibration_elapsed(now)
            raise ExperimentFailure(self.failure)

    def process(self, frame: StreamFrame, now: float) -> dict:
        if frame.sensor_id != self.connection.sensor_id:
            raise ExperimentFailure(f"Unexpected sensor_id {frame.sensor_id}")
        # Preserve a late received frame as evidence without feeding it to the model.
        try:
            self.check_deadlines(now)
        except ExperimentFailure:
            pass
        raw = parse_sensor_timestamp(frame.payload)  # uint32 little-endian microseconds
        gateway = frame.gateway_timestamp_us  # uint64 little-endian microseconds
        reference_ns = gateway * 1_000
        phase = self.phase
        supplied = False
        error = None
        candidate = dict.fromkeys(CANDIDATE_FIELDS)
        continuity = "continuous"
        if self.previous_dot is not None:
            dot_delta = raw - self.previous_dot
            gateway_delta = gateway - self.previous_gateway
            # Width is verified, actual device wrap semantics are not. A backward
            # jump (even near 2**32), duplicate, or implausible forward jump must
            # never be passed through to the accepted fixed mapping.
            if dot_delta <= 0 or gateway_delta < 0 or abs(dot_delta - gateway_delta) > 1_000_000:
                continuity = "discontinuity_new_epoch"
                self.ended_epochs.append(self.summary())
                self.epoch += 1
                self.model = AffineClockModel(self.config)
                self.phase = phase = "acquisition"
                self.calibration_started_at = None
                self.calibration_reference_ns = None
                self.calibration_duration_seconds = None
                self.calibration_reference_span_seconds = None
                self.calibration_outcome = "not_started"
                self.stability_outcome = "not_run_new_epoch"
                self.tracking_error_count = 0
                self.failure = "Timestamp discontinuity; new epoch recorded, restart experiment"
        self.previous_dot, self.previous_gateway = raw, gateway
        self.last_data_at = now
        if not self.failure:
            # Snapshot gate state BEFORE this frame: the gate-passing frame is
            # acquisition evidence; the NEXT frame is the first calibration pair.
            if self.monitor.measurement_active:
                if self.calibration_started_at is None:
                    self.calibration_started_at = now
                    self.calibration_reference_ns = reference_ns
                    self.calibration_outcome = "pending"
                phase = "measurement" if self.model.mapping_available else "calibration"
                was_tracking = self.model.state in {ClockModelState.ALIGNED, ClockModelState.TRACKING}
                span = (reference_ns - self.calibration_reference_ns) / 1e9
                # Also enforce the deadline in the reference domain so buffered
                # observations cannot quietly align after the configured span.
                if (self.calibration_outcome == "pending" and
                        span > self.config.calibration_deadline_ns / 1e9):
                    self.calibration_outcome = "failed"
                    self.calibration_duration_seconds = self._calibration_elapsed(now)
                    self.calibration_reference_span_seconds = span
                    self.failure = "Calibration reference-span deadline exceeded before alignment"
                else:
                    candidate = observe_with_candidate(self.model, raw, reference_ns)
                    supplied = True
                    if was_tracking:
                        error = self.model.last_prediction_error_ns
                        self.tracking_error_count += 1
                    if self.calibration_outcome == "pending" and self.model.mapping_available:
                        self.calibration_outcome = "aligned"
                        self.calibration_duration_seconds = self._calibration_elapsed(now)
                        self.calibration_reference_span_seconds = span
                        self.phase = "measurement"
                    else:
                        self.phase = phase
            self.monitor.handle_stream_frame(frame, now)
            if self.monitor.measurement_active and self.stability_outcome == "pending":
                self.stability_outcome = "passed"
                self.phase = "calibration"
        return {
            "address": self.connection.address, "sensor_id": frame.sensor_id,
            "connection_epoch": self.epoch, "stream_phase": phase, "continuity": continuity,
            "raw_dot_timestamp_us": raw,
            # No rollover inferred: within a verified continuous epoch these are identical.
            "unwrapped_dot_timestamp_us": raw if continuity == "continuous" else None,
            "gateway_timestamp_us": gateway, "reference_time_ns": reference_ns,
            "mapped_timestamp_ns": self.model.map(raw) if supplied else None,
            "model_observation_supplied": supplied, "model_state": self.model.state.value,
            "estimated_scale_ns_per_source_unit": self.model.estimated_scale_ns_per_source_unit,
            "latest_prediction_error_ns": error,
            "mean_prediction_error_ns": self.model.mean_prediction_error_ns,
            "prediction_error_stddev_ns": self.model.prediction_error_stddev_ns,
            "observation_count": self.model.observation_count,
            "retained_calibration_observation_count": self.model.retained_observation_count,
            "retained_tracking_error_count": len(self.model.recent_prediction_errors_ns),
            "host_elapsed_seconds": now - self.started_at,
            "calibration_elapsed_seconds": self._calibration_elapsed(now),
            **candidate,
        }

    def summary(self) -> dict:
        return {
            "address": self.connection.address, "sensor_id": self.connection.sensor_id,
            "connection_epoch": self.epoch, "stream_phase": self.phase,
            "stability_gate_outcome": self.stability_outcome,
            "calibration_outcome": self.calibration_outcome,
            "calibration_duration_seconds": self.calibration_duration_seconds,
            "calibration_reference_span_seconds": self.calibration_reference_span_seconds,
            "learned_scale_ns_per_source_unit": self.model.estimated_scale_ns_per_source_unit,
            "expected_scale_ns_per_source_unit": 1_000,
            "model_state": self.model.state.value,
            "observation_count": self.model.observation_count,
            "retained_calibration_observation_count": self.model.retained_observation_count,
            "tracking_error_count_full_epoch": self.tracking_error_count,
            "tracking_error_summary_scope": "retained window in current epoch",
            "retained_tracking_error_count": len(self.model.recent_prediction_errors_ns),
            "mean_prediction_error_ns": self.model.mean_prediction_error_ns,
            "prediction_error_stddev_ns": self.model.prediction_error_stddev_ns,
            "packet_diagnostics_scope": "entire connection: startup; post-gate includes calibration and measurement",
            "packet_diagnostics": asdict(self.monitor.stats_by_address[self.connection.address]),
            "failure": self.failure,
        }
