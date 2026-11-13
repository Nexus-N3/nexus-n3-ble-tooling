"""Two-stage DOT -> gateway -> node monotonic alignment; no transport correction."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, replace

from clock_mesh import AffineClockModel, ClockModelState

from .alignment import DotExperiment, ExperimentFailure, RECORD_FIELDS, try_start_official
from .candidates import observe_with_candidate
from .evidence import ErrorStatistics
from .motion import decode_motion

NODE_FIELDS = RECORD_FIELDS + [
    "sequence", "sensor_sequence", "node_receive_monotonic_ns", "processing_monotonic_ns",
    "receive_read_id", "receive_read_size", "receive_batch_index", "mapped_gateway_ns",
    "mapped_node_ns", "session_time_ns", "gateway_node_epoch", "gateway_node_observation_supplied",
    "gateway_node_state", "gateway_node_scale", "gateway_node_prediction_error_ns",
    "gateway_node_candidate_prediction_error_ns", "gateway_node_candidate_accepted",
    "gateway_node_observation_count", "composed_monotonicity_violation",
]


class Distribution:
    """Exact integer moments with fixed nanosecond interval histogram bins."""
    bounds = (0, 100_000, 1_000_000, 5_000_000, 10_000_000, 20_000_000, 100_000_000)

    def __init__(self):
        self.stats = ErrorStatistics()
        self.bins = Counter()

    def add(self, value, reference):
        self.stats.add(value, reference)
        label = next((f"<= {bound}" for bound in self.bounds if value <= bound), "> 100000000")
        self.bins[label] += 1

    def record(self):
        r = self.stats.record()
        return {"count": r["tracking_error_count"], "mean_ns": r["mean_prediction_error_ns"],
                "population_stddev_ns": r["prediction_error_stddev_ns"],
                "min_ns": r["min_prediction_error_ns"], "max_ns": r["max_prediction_error_ns"],
                "histogram_ns": dict(self.bins)}


# new clock relationship between the gateway and the node this is running on.
class GatewayNodeClock:
    """One observation per received notification, shared by every selected DOT.

    Equal source/receive timestamps are valid: gateway ticks can coincide and
    multiple frames can complete in one serial read. Reversals end the capture.
    """
    def __init__(self, config):
        self.config = replace(config, nominal_scale_ns_per_source_unit=1.0)
        self.model = AffineClockModel(self.config)
        self.epoch = 1
        self.previous_source = self.previous_receive = None
        self.calibration_started_ns = self.calibration_receive_ns = None
        self.failure = None
        self.official_started_ns = None
        self.mapping_updates = []
        self.ended_epochs = []
        self.stats = {phase: {key: Distribution() for key in (
            "prediction_error", "callback_interval", "receive_interval")}
            for phase in ("startup", "measurement")}
        self.counts = {phase: Counter() for phase in self.stats}
        self.previous_read_id = None
        self.batch_counts = Counter()
        self.batch_size = 0
        self.batch_phase_sizes = Counter()
        self.phase_batches = {phase: Counter() for phase in self.stats}

    @property
    def ready(self):
        return (not self.failure and self.previous_source is not None
                and self.model.state in (ClockModelState.ALIGNED, ClockModelState.TRACKING)
                and self.model.candidate_is_eligible(self.previous_source))

    def check_deadline(self, now_ns):
        if self.failure:
            raise ExperimentFailure(self.failure)
        if (self.calibration_receive_ns is not None and
                self.model.state not in (ClockModelState.ALIGNED, ClockModelState.TRACKING) and
                now_ns - self.calibration_receive_ns >= self.config.calibration_deadline_ns):
            self.failure = "Gateway-to-node calibration deadline exceeded"
            raise ExperimentFailure(self.failure)

    def observe(self, frame, phase):
        source = frame.gateway_timestamp_us * 1000
        receive = frame.node_receive_monotonic_ns
        if receive is None:
            raise ExperimentFailure("StreamFrame lacks an early node reception timestamp")
        if self.failure:
            raise ExperimentFailure(self.failure)
        if (self.previous_source is not None and
                (source < self.previous_source or receive < self.previous_receive)):
            self.ended_epochs.append(self.summary())
            self.epoch += 1
            self.model = AffineClockModel(self.config)
            self.failure = "Gateway or node reception timestamp reversal; new epoch recorded, restart capture"
            return {"supplied": False, "error": None, "candidate_error": None, "accepted": False}
        if self.calibration_started_ns is None:
            self.calibration_started_ns = source
            self.calibration_receive_ns = receive
        self.check_deadline(receive)
        counts = self.counts[phase]
        counts["observations"] += 1
        if self.previous_source is not None:
            self.stats[phase]["callback_interval"].add(source - self.previous_source, receive)
            self.stats[phase]["receive_interval"].add(receive - self.previous_receive, receive)
            counts["equal_callback_timestamps"] += int(source == self.previous_source)
            counts["equal_receive_timestamps"] += int(receive == self.previous_receive)
        if frame.receive_read_id != self.previous_read_id or self.batch_size == 0:
            if self.batch_size:
                self.batch_counts[self.batch_size] += 1
                for batch_phase, size in self.batch_phase_sizes.items():
                    self.phase_batches[batch_phase][size] += 1
            self.batch_size = 0
            self.batch_phase_sizes.clear()
            counts["serial_read_batches"] += 1
            self.previous_read_id = frame.receive_read_id
        self.batch_size += 1
        self.batch_phase_sizes[phase] += 1
        was_tracking = self.model.state in (ClockModelState.ALIGNED, ClockModelState.TRACKING)
        candidate = observe_with_candidate(self.model, source, receive)
        error = self.model.last_prediction_error_ns if was_tracking else None
        if error is not None:
            self.stats[phase]["prediction_error"].add(error, receive)
        self.previous_source, self.previous_receive = source, receive
        accepted = self.accept(full=False) if phase == "measurement" else False
        return {"supplied": True, "error": error,
                "candidate_error": candidate["candidate_prediction_error_ns"], "accepted": accepted}

    def accept(self, *, full):
        pivot = self.previous_source
        before = self.model.map(pivot)
        candidate = self.model.map_candidate(pivot)
        scale = self.model.estimated_scale_ns_per_source_unit
        method = self.model.accept_full_candidate if full else self.model.accept_candidate
        accepted = method(pivot)
        after = self.model.map(pivot)
        self.mapping_updates.append({
            "model_id": "gateway_to_node", "connection_epoch": self.epoch,
            "kind": "official_start_full_affine" if full else "continuous_scale",
            "source_pivot_ns": pivot, "node_receive_monotonic_ns": self.previous_receive,
            "mapped_pivot_before_ns": before, "mapped_pivot_after_ns": after,
            "candidate_pivot_ns": candidate, "old_scale": scale,
            "new_scale": self.model.estimated_scale_ns_per_source_unit, "accepted": accepted,
            "observation_count": self.model.observation_count,
            "candidate_reference_span_ns": self.model.retained_tracking_reference_span_ns,
        })
        if accepted and not full and before != after:
            raise ExperimentFailure("Gateway-to-node scale acceptance broke pivot continuity")
        return accepted

    def summary(self):
        batches = self.batch_counts.copy()
        phase_batches = {phase: counts.copy() for phase, counts in self.phase_batches.items()}
        if self.batch_size:
            batches[self.batch_size] += 1
            for phase, size in self.batch_phase_sizes.items():
                phase_batches[phase][size] += 1
        return {"model_id": "gateway_to_node", "connection_epoch": self.epoch,
                "model_resets": self.epoch - 1, "failure": self.failure,
                "configuration": asdict(self.config), "model_state": self.model.state.value,
                "mapping_available": self.model.mapping_available,
                "candidate_eligible": self.ready, "observation_count": self.model.observation_count,
                "scale_ns_per_gateway_ns": self.model.estimated_scale_ns_per_source_unit,
                "mapping_update_count": len(self.mapping_updates),
                "batch_frame_count_histogram": dict(batches),
                "batch_scope": "consecutive selected frames grouped by serial read ID; full capture",
                "phase_statistics": {phase: {"counts": dict(self.counts[phase]),
                    "read_batch_frame_count_histogram": dict(phase_batches[phase]),
                    **{key: value.record() for key, value in stats.items()}}
                    for phase, stats in self.stats.items()}}


class NodeExperiment:
    def __init__(self, connections, *, started_at, config, no_data_seconds):
        self.dots = {c.sensor_id: DotExperiment(c, started_at=started_at,
                     calibration_config=config, no_data_seconds=no_data_seconds) for c in connections}
        self.gateway_node = GatewayNodeClock(config)
        self.official_start_node_monotonic_ns = None
        self.sequence = 0
        self.sensor_sequences = Counter()
        self.previous_mapped = {}
        self.monotonicity = {phase: Counter() for phase in ("startup", "measurement")}
        self.dot_errors = {phase: {sensor_id: Distribution() for sensor_id in self.dots}
                           for phase in self.monotonicity}
        self.dot_phase_counts = {phase: {sensor_id: Counter(
            received_frames=0, model_observations=0, inferred_missing_samples=0) for sensor_id in self.dots}
                                 for phase in self.monotonicity}
        self.previous_global_mapped = None
        self.global_monotonicity = Counter()

    def check_deadlines(self, now, now_ns):
        for exp in self.dots.values():
            exp.check_deadlines(now)
        self.gateway_node.check_deadline(now_ns)

    def try_start(self, now, now_ns):
        if self.official_start_node_monotonic_ns is not None or not self.gateway_node.ready:
            return False
        if not all(exp.ready_for_official_start for exp in self.dots.values()):
            return False
        if not try_start_official(list(self.dots.values()), now):
            return False
        try:
            if not self.gateway_node.accept(full=True):
                raise ExperimentFailure("Shared gateway-to-node full candidate rejected")
        finally:
            self._tag_updates(now_ns)
        self.official_start_node_monotonic_ns = now_ns
        self.gateway_node.official_started_ns = now_ns
        return True

    def _tag_updates(self, processing_ns):
        for updates in [self.gateway_node.mapping_updates] + [exp.mapping_updates for exp in self.dots.values()]:
            # Only the newest acceptance can be created by an observation/boundary.
            if updates and "processing_monotonic_ns" not in updates[-1]:
                updates[-1].update(processing_monotonic_ns=processing_ns, sequence=self.sequence)

    def process(self, frame, now, processing_ns):
        exp = self.dots.get(frame.sensor_id)
        if exp is None:
            raise ExperimentFailure(f"Unexpected sensor_id {frame.sensor_id}")
        phase = "startup" if self.official_start_node_monotonic_ns is None else "measurement"
        motion = decode_motion(frame.payload)
        self.sequence += 1
        self.sensor_sequences[frame.sensor_id] += 1
        shared = self.gateway_node.observe(frame, phase)
        # On a shared reversal, preserve the triggering frame as unsupplied evidence
        # and do not feed any model with that ambiguous timing pair.
        if not shared["supplied"]:
            return {**dict.fromkeys(NODE_FIELDS), "sensor_id": frame.sensor_id,
                    "address": exp.connection.address, "stream_phase": phase,
                    "gateway_timestamp_us": frame.gateway_timestamp_us,
                    "node_receive_monotonic_ns": frame.node_receive_monotonic_ns,
                    "gateway_node_epoch": self.gateway_node.epoch,
                    "gateway_node_observation_supplied": False,
                    "sequence": self.sequence, "sensor_sequence": self.sensor_sequences[frame.sensor_id],
                    "raw_dot_timestamp_us": exp.source_timestamp_us(frame),
                    "reference_time_ns": frame.gateway_timestamp_us * 1000,
                    "receive_read_id": frame.receive_read_id, "receive_read_size": frame.receive_read_size,
                    "receive_batch_index": frame.receive_batch_index,
                    "processing_monotonic_ns": processing_ns,
                    "model_observation_supplied": False,
                    "continuity": "gateway_node_reversal"}
        previous_source = exp.previous_dot
        row = exp.process(frame, now)
        row.update(motion)
        self._tag_updates(processing_ns)
        counts = self.dot_phase_counts[phase][frame.sensor_id]
        counts["received_frames"] += 1
        counts["model_observations"] += int(row["model_observation_supplied"])
        if previous_source is not None and row["raw_dot_timestamp_us"] > previous_source:
            delta = row["raw_dot_timestamp_us"] - previous_source
            if delta > 1.5 * 1e6 / 60:
                counts["inferred_missing_samples"] += max(0, round(delta / (1e6 / 60)) - 1)
        intermediate = row["mapped_timestamp_ns"]
        mapped = self.gateway_node.model.map(intermediate) if intermediate is not None else None
        violation = False
        if mapped is not None:
            if self.previous_global_mapped is not None:
                self.global_monotonicity[phase] += int(mapped < self.previous_global_mapped)
            self.previous_global_mapped = mapped
            previous = self.previous_mapped.get(frame.sensor_id)
            violation = previous is not None and mapped <= previous
            self.monotonicity[phase][frame.sensor_id] += int(violation)
            self.previous_mapped[frame.sensor_id] = mapped
        if row["latest_prediction_error_ns"] is not None:
            self.dot_errors[phase][frame.sensor_id].add(row["latest_prediction_error_ns"], row["reference_time_ns"])
        session = (mapped - self.official_start_node_monotonic_ns
                   if phase == "measurement" and mapped is not None and not exp.failure else None)
        row.update(sequence=self.sequence, sensor_sequence=self.sensor_sequences[frame.sensor_id],
                   node_receive_monotonic_ns=frame.node_receive_monotonic_ns,
                   processing_monotonic_ns=processing_ns, receive_read_id=frame.receive_read_id,
                   receive_read_size=frame.receive_read_size, receive_batch_index=frame.receive_batch_index,
                   mapped_gateway_ns=intermediate, mapped_node_ns=mapped, session_time_ns=session,
                   gateway_node_epoch=self.gateway_node.epoch,
                   gateway_node_observation_supplied=shared["supplied"],
                   gateway_node_state=self.gateway_node.model.state.value,
                   gateway_node_scale=self.gateway_node.model.estimated_scale_ns_per_source_unit,
                   gateway_node_prediction_error_ns=shared["error"],
                   gateway_node_candidate_prediction_error_ns=shared["candidate_error"],
                   gateway_node_candidate_accepted=shared["accepted"],
                   gateway_node_observation_count=self.gateway_node.model.observation_count,
                   composed_monotonicity_violation=violation, connection_interval_us=None)
        return row

    def summary(self):
        return {"experiments": [{**exp.summary(), "model_id": f"dot_{sid}_to_gateway",
                                  "mapping_available": exp.model.mapping_available,
                                  "model_resets": exp.epoch - 1,
                                  "candidate_eligible": (exp.previous_dot is not None and
                                                         exp.model.candidate_is_eligible(exp.previous_dot))}
                                 for sid, exp in self.dots.items()],
                "gateway_node": self.gateway_node.summary(),
                "gateway_node_ended_epochs": self.gateway_node.ended_epochs,
                "ended_epochs": [epoch for exp in self.dots.values() for epoch in exp.ended_epochs],
                "official_start_node_monotonic_ns": self.official_start_node_monotonic_ns,
                "composed_monotonicity_violations": {phase: dict(counts) for phase, counts in self.monotonicity.items()},
                "composed_reversals_in_acquisition_order": dict(self.global_monotonicity),
                "monotonicity_scope": "Per-DOT checks require strictly increasing mapped time. Acquisition-order checks allow equal times and include inter-sensor fit/delivery differences; not a physical synchronisation test.",
                "dot_counts_by_phase": {phase: {sid: dict(counts) for sid, counts in sensors.items()}
                                        for phase, sensors in self.dot_phase_counts.items()},
                "dot_prediction_errors_by_phase": {phase: {sid: value.record() for sid, value in errors.items()}
                                                   for phase, errors in self.dot_errors.items()}}
