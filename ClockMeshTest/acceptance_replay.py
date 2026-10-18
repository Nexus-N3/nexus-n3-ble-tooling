"""Explicit scale-only candidate acceptance experiment, entirely offline."""
from __future__ import annotations

import argparse
from collections import deque
import csv
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import math
from pathlib import Path
from uuid import uuid4

from clock_mesh import AffineClockModel, ClockAlignmentConfig, ClockModelState

from .evidence import ErrorStatistics, INTERVAL_NS
from .replay import file_identity, load_configuration, write_json
from .stream_client import software_info


ATTEMPT_CADENCE_NS = 5_000_000_000
PATHS = ("frozen", "controlled", "candidate")
ORDER = [
    "For each supplied row, predict frozen.map, controlled.map, and controlled.map_candidate before either observe call",
    "Compute all evaluation errors as raw gateway reference minus those pre-update predictions",
    "Observe the current raw pair in frozen, then controlled",
    "If scheduled, capture controlled pivot/candidate diagnostics, then call controlled.accept_candidate at the just-observed source timestamp",
    "Capture post-attempt state, verify successful pivot preservation exactly, and save predictions without recomputing evaluation errors",
]
IDENTITY_FIELDS = ["source_row", "address", "sensor_id", "connection_epoch",
                   "raw_dot_timestamp_us", "gateway_timestamp_us", "reference_time_ns"]
OBSERVATION_FIELDS = [
    *IDENTITY_FIELDS, "recorded_stream_phase", "model_observation_supplied", "stream_phase",
    "frozen_model_state", "controlled_model_state", "observation_count",
    *[field for path in PATHS for field in (f"{path}_predicted_reference_ns", f"{path}_prediction_error_ns")],
    "controlled_scale_pre_update", "controlled_scale_post_update", "candidate_scale_post_update",
    "candidate_fit_observation_count_pre_update", "candidate_fit_reference_span_ns_pre_update",
    "retained_tracking_observation_count", "retained_tracking_reference_span_ns",
    "acceptance_attempted", "acceptance_outcome", "successful_acceptance_count",
    "controlled_mapped_pivot_post_update_ns", "controlled_mapped_increment_ns", "common_comparison_period",
]
EVENT_FIELDS = [
    *IDENTITY_FIELDS, "attempt_index", "outcome", "failure", "old_accepted_scale_ns_per_source_unit",
    "candidate_scale_ns_per_source_unit", "candidate_relative_offset_ns", "candidate_relative_offset_availability",
    "new_accepted_scale_ns_per_source_unit", "retained_tracking_observation_count",
    "retained_tracking_reference_span_ns", "accepted_pivot_before_ns", "accepted_pivot_after_ns",
    "candidate_pivot_ns", "candidate_minus_accepted_at_pivot_ns", "pivot_preserved_exactly",
    "pre_update_controlled_prediction_error_ns", "next_attempt_reference_time_ns",
]


class ThreePathStatistics:
    def __init__(self):
        self.errors = {path: ErrorStatistics() for path in PATHS}

    def add(self, row: dict) -> None:
        for path, stats in self.errors.items():
            error = row[f"{path}_prediction_error_ns"]
            if error is not None:
                stats.add(error, row["reference_time_ns"])

    def record(self) -> dict:
        return {path: stats.record() for path, stats in self.errors.items()}


@dataclass
class BiasTrend:
    """OLS error versus gateway-reference time; descriptive, never a clock update."""
    count: int = 0
    origin_ns: int | None = None
    last_ns: int | None = None
    sum_t: int = 0
    sum_tt: int = 0
    sum_y: int = 0
    sum_ty: int = 0

    def add(self, error_ns: int, reference_ns: int) -> None:
        if self.origin_ns is None:
            self.origin_ns = reference_ns
        t = reference_ns - self.origin_ns
        self.last_ns = reference_ns
        self.count += 1
        self.sum_t += t
        self.sum_tt += t * t
        self.sum_y += error_ns
        self.sum_ty += t * error_ns

    def record(self) -> dict:
        denominator = self.count * self.sum_tt - self.sum_t * self.sum_t
        slope = ((self.count * self.sum_ty - self.sum_t * self.sum_y) / denominator
                 if denominator else None)
        start = ((self.sum_y - slope * self.sum_t) / self.count if slope is not None else None)
        span = self.last_ns - self.origin_ns if self.count else None
        return {"count": self.count, "mean_signed_prediction_error_ns": self.sum_y / self.count if self.count else None,
                "error_time_slope_ns_per_reference_second": slope * 1e9 if slope is not None else None,
                "fitted_bias_at_period_start_ns": start,
                "fitted_bias_at_period_end_ns": start + slope * span if start is not None else None,
                "first_reference_time_ns": self.origin_ns, "last_reference_time_ns": self.last_ns,
                "actual_reference_span_ns": span,
                "interpretation": "descriptive prediction-error trend after first acceptance; not proven clock drift or offset correction"}


class AcceptanceEpoch:
    """Two independent models and a gateway-time attempt schedule per device/epoch."""

    def __init__(self, config: ClockAlignmentConfig):
        self.frozen = AffineClockModel(config)
        self.controlled = AffineClockModel(config)
        self.config = config
        self.references: deque[int] = deque(maxlen=config.max_tracking_observations)
        self.previous_pair: tuple[int, int] | None = None
        self.next_attempt_ns: int | None = None
        self.attempt_count = 0
        self.success_count = 0
        self.first_success_ns: int | None = None
        self.last_success_ns: int | None = None
        self.previous_prediction_ns: int | None = None
        self.increments = ErrorStatistics()
        self.nonpositive_increment_count = 0
        self.full = ThreePathStatistics()
        self.common = ThreePathStatistics()
        self.trend = BiasTrend()
        self.origin_ns: int | None = None
        self.intervals: dict[int, tuple[ThreePathStatistics, ThreePathStatistics]] = {}

    def span(self) -> int | None:
        return self.references[-1] - self.references[0] if self.references else None

    def _attempt(self, source: int, reference: int, prediction_error: int | None) -> dict:
        model = self.controlled
        before = model.map(source)
        candidate_pivot = model.map_candidate(source)
        old_scale = model.estimated_scale_ns_per_source_unit
        candidate_scale = model.candidate_scale_ns_per_source_unit
        self.attempt_count += 1
        # Advance from every attempt, including rejected/error outcomes; do not
        # catch up with multiple attempts or retry at acquisition sample cadence.
        self.next_attempt_ns = reference + ATTEMPT_CADENCE_NS
        failure = None
        try:
            success = model.accept_candidate(source)
            outcome = "accepted" if success else "rejected"
        except Exception as exc:
            success = False
            outcome, failure = "error", f"{type(exc).__name__}: {exc}"
        after = model.map(source)
        if success:
            self.success_count += 1
            if self.first_success_ns is None:
                self.first_success_ns = reference
            self.last_success_ns = reference
        return {
            "attempt_index": self.attempt_count, "outcome": outcome, "failure": failure,
            "old_accepted_scale_ns_per_source_unit": old_scale,
            "candidate_scale_ns_per_source_unit": candidate_scale,
            "candidate_relative_offset_ns": None,
            "candidate_relative_offset_availability": "not exposed by the inspected public ClockMesh API",
            "new_accepted_scale_ns_per_source_unit": model.estimated_scale_ns_per_source_unit,
            "retained_tracking_observation_count": model.retained_tracking_observation_count,
            "retained_tracking_reference_span_ns": self.span(),
            "accepted_pivot_before_ns": before, "accepted_pivot_after_ns": after,
            "candidate_pivot_ns": candidate_pivot,
            "candidate_minus_accepted_at_pivot_ns": candidate_pivot - before if candidate_pivot is not None and before is not None else None,
            "pivot_preserved_exactly": (before is not None and before == after) if success else None,
            "pre_update_controlled_prediction_error_ns": prediction_error,
            "next_attempt_reference_time_ns": self.next_attempt_ns,
        }

    def process(self, source: int, reference: int, supplied: bool) -> tuple[dict, dict | None]:
        row = {f"{path}_{field}": None for path in PATHS
               for field in ("predicted_reference_ns", "prediction_error_ns")}
        row.update(stream_phase="acquisition", controlled_scale_pre_update=None,
                   candidate_fit_observation_count_pre_update=None, candidate_fit_reference_span_ns_pre_update=None,
                   controlled_mapped_increment_ns=None)
        event = None
        if supplied:
            if self.previous_pair is not None and (source <= self.previous_pair[0] or reference < self.previous_pair[1]):
                raise ValueError("Discontinuous supplied raw timestamp pair within one device/epoch")
            self.previous_pair = source, reference
            tracking = self.controlled.state in {ClockModelState.ALIGNED, ClockModelState.TRACKING}
            row["stream_phase"] = "measurement" if tracking else "calibration"
            # All three predictions precede BOTH observes. Calibration mappings
            # are not labeled evaluation predictions until tracking starts.
            frozen_prediction = self.frozen.map(source)
            controlled_prediction = self.controlled.map(source)
            candidate_prediction = self.controlled.map_candidate(source)
            if tracking:
                for path, predicted in zip(PATHS, (frozen_prediction, controlled_prediction, candidate_prediction)):
                    row[f"{path}_predicted_reference_ns"] = predicted
                    row[f"{path}_prediction_error_ns"] = reference - predicted if predicted is not None else None
            row.update(controlled_scale_pre_update=self.controlled.estimated_scale_ns_per_source_unit,
                       candidate_fit_observation_count_pre_update=self.controlled.retained_tracking_observation_count,
                       candidate_fit_reference_span_ns_pre_update=self.span())
            self.frozen.observe(source_time=source, reference_time_ns=reference)
            self.controlled.observe(source_time=source, reference_time_ns=reference)
            if tracking:
                self.references.append(reference)
                if len(self.references) != self.controlled.retained_tracking_observation_count:
                    raise ValueError("Tracking reference-time mirror disagrees with public observation count")
            elif (self.controlled.state not in {ClockModelState.ALIGNED, ClockModelState.TRACKING} and
                  self.controlled.observation_count >= self.config.max_calibration_observations):
                raise ValueError("Calibration storage exhausted before alignment")
            candidate_scale = self.controlled.candidate_scale_ns_per_source_unit
            first_eligible = (
                self.next_attempt_ns is None and self.span() is not None and
                self.span() >= self.config.min_tracking_acceptance_span_ns and
                self.controlled.retained_tracking_observation_count >= self.config.min_learned_observations and
                candidate_scale is not None and math.isfinite(candidate_scale) and candidate_scale > 0
            )
            scheduled = self.next_attempt_ns is not None and reference >= self.next_attempt_ns
            if first_eligible or scheduled:
                event = self._attempt(source, reference, row["controlled_prediction_error_ns"])
            predicted = row["controlled_predicted_reference_ns"]
            if predicted is not None:
                if self.previous_prediction_ns is not None:
                    increment = predicted - self.previous_prediction_ns
                    row["controlled_mapped_increment_ns"] = increment
                    self.increments.add(increment, reference)
                    self.nonpositive_increment_count += int(increment <= 0)
                self.previous_prediction_ns = predicted
        row.update(
            frozen_model_state=self.frozen.state.value, controlled_model_state=self.controlled.state.value,
            observation_count=self.controlled.observation_count,
            controlled_scale_post_update=self.controlled.estimated_scale_ns_per_source_unit if supplied else None,
            candidate_scale_post_update=self.controlled.candidate_scale_ns_per_source_unit if supplied else None,
            retained_tracking_observation_count=self.controlled.retained_tracking_observation_count,
            retained_tracking_reference_span_ns=self.span(), acceptance_attempted=event is not None,
            acceptance_outcome=event["outcome"] if event else None, successful_acceptance_count=self.success_count,
            controlled_mapped_pivot_post_update_ns=self.controlled.map(source) if supplied else None,
            common_comparison_period=(self.first_success_ns is not None and reference >= self.first_success_ns),
        )
        return row, event

    def collect(self, row: dict) -> None:
        if row["controlled_prediction_error_ns"] is None:
            return
        reference = row["reference_time_ns"]
        if self.origin_ns is None:
            self.origin_ns = reference
        self.full.add(row)
        if row["common_comparison_period"]:
            self.common.add(row)
            self.trend.add(row["controlled_prediction_error_ns"], reference)
        full, common = self.intervals.setdefault((reference - self.origin_ns) // INTERVAL_NS,
                                                 (ThreePathStatistics(), ThreePathStatistics()))
        full.add(row)
        if row["common_comparison_period"]:
            common.add(row)

    def summary(self, identity: dict) -> dict:
        increments = self.increments.record()
        return {
            **identity, "acceptance_attempt_count": self.attempt_count,
            "successful_acceptance_count": self.success_count,
            "unsuccessful_attempt_count": self.attempt_count - self.success_count,
            "first_successful_acceptance_reference_ns": self.first_success_ns,
            "last_successful_acceptance_reference_ns": self.last_success_ns,
            "full_tracking_period": self.full.record(), "common_comparison_period": self.common.record(),
            "remaining_controlled_bias_and_trend": self.trend.record(),
            "controlled_mapped_timestamp_increments": {
                "count": increments["tracking_error_count"],
                "mean_increment_ns": increments["mean_prediction_error_ns"],
                "population_stddev_increment_ns": increments["prediction_error_stddev_ns"],
                "minimum_increment_ns": increments["min_prediction_error_ns"],
                "maximum_increment_ns": increments["max_prediction_error_ns"],
                "nonpositive_increment_count": self.nonpositive_increment_count,
                "scope": "successive available pre-update controlled predictions in this epoch; all increments in observations.csv",
            },
            "final_frozen_scale_ns_per_source_unit": self.frozen.estimated_scale_ns_per_source_unit,
            "final_controlled_scale_ns_per_source_unit": self.controlled.estimated_scale_ns_per_source_unit,
            "final_candidate_scale_ns_per_source_unit": self.controlled.candidate_scale_ns_per_source_unit,
        }


def replay_acceptance(source: Path, output: Path, *, config_override: Path | None = None) -> dict:
    source, output = source.resolve(), output.resolve()
    if output == source.parent or source.parent in output.parents or output.exists():
        raise ValueError("Choose a new output directory outside the source capture; existing destinations are never overwritten")
    recovered, recovery = load_configuration(source, config_override)
    config = replace(recovered, max_tracking_observations=2048,
                     min_tracking_acceptance_span_ns=30_000_000_000)
    required = {"address", "sensor_id", "connection_epoch", "raw_dot_timestamp_us",
                "gateway_timestamp_us", "model_observation_supplied"}
    with source.open(newline="", encoding="utf-8") as handle:
        if not required.issubset(csv.DictReader(handle).fieldnames or []):
            raise ValueError("Required raw observation input columns are missing")
    manifest = {
        "schema_version": 1, "status": "running", "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_observations": file_identity(source), "source_capture": recovery, "software": software_info(),
        "model_configurations": {"frozen": asdict(config), "controlled": asdict(config)},
        "explicit_policy_overrides": {"max_tracking_observations": 2048,
                                      "min_tracking_acceptance_span_ns": 30_000_000_000},
        "attempt_cadence_reference_ns": ATTEMPT_CADENCE_NS,
        "first_attempt": "post-observe retained count >= min_learned_observations, retained reference span >= 30 seconds, finite positive candidate scale",
        "later_attempts": "first supplied observation at least five gateway-reference seconds after the previous attempt, including failures; no catch-up loop",
        "common_period": "per device/epoch, starting with first successful acceptance's pivot row; that row's errors still use pre-update predictions",
        "processing_order": ORDER,
        "interpretation": "candidate mapping fits scale and offset; explicit acceptance preserves pivot time and changes scale only; accumulated bias is not corrected",
        "candidate_relative_offset": "not publicly exposed by inspected API; event field null; candidate minus accepted time at pivot is separately derived",
        "outputs": ["observations.csv", "acceptance_events.csv", "summary.json", "intervals.json"],
    }
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "manifest.json", manifest)
    epochs = {}
    try:
        with (output / "observations.csv").open("w", newline="", encoding="utf-8") as obs_handle, \
             (output / "acceptance_events.csv").open("w", newline="", encoding="utf-8") as event_handle, \
             source.open(newline="", encoding="utf-8") as source_handle:
            writer = csv.DictWriter(obs_handle, OBSERVATION_FIELDS)
            events = csv.DictWriter(event_handle, EVENT_FIELDS)
            writer.writeheader()
            events.writeheader()
            for index, raw in enumerate(csv.DictReader(source_handle), start=1):
                flag = raw["model_observation_supplied"].lower()
                if flag not in {"true", "false", "1", "0"}:
                    raise ValueError(f"Invalid model supply flag on input row {index}")
                supplied = flag in {"true", "1"}
                key = raw["address"], int(raw["sensor_id"]), int(raw["connection_epoch"])
                source_time, gateway = int(raw["raw_dot_timestamp_us"]), int(raw["gateway_timestamp_us"])
                if not key[0] or not 0 <= source_time < 2**32 or not 0 <= gateway < 2**64:
                    raise ValueError(f"Invalid raw timestamp or device identity on input row {index}")
                reference = gateway * 1000
                if raw.get("reference_time_ns") and int(raw["reference_time_ns"]) != reference:
                    raise ValueError(f"Raw reference conversion mismatch on input row {index}")
                identity = {"source_row": index, "address": key[0], "sensor_id": key[1], "connection_epoch": key[2],
                            "raw_dot_timestamp_us": source_time, "gateway_timestamp_us": gateway,
                            "reference_time_ns": reference}
                if key not in epochs:
                    epochs[key] = AcceptanceEpoch(config)
                row, event = epochs[key].process(source_time, reference, supplied)
                row.update(identity, recorded_stream_phase=raw.get("stream_phase", ""), model_observation_supplied=supplied)
                writer.writerow(row)
                if event is not None:
                    events.writerow({**identity, **event})
                    if event["outcome"] == "accepted" and not event["pivot_preserved_exactly"]:
                        raise ValueError("Successful acceptance did not preserve mapped pivot timestamp exactly; event saved")
                epochs[key].collect(row)
        if not any(epoch.controlled.observation_count for epoch in epochs.values()):
            raise ValueError("No supplied timestamp observations to replay")
        summaries, intervals = [], []
        for (address, sensor_id, epoch_id), epoch in epochs.items():
            identity = {"address": address, "sensor_id": sensor_id, "connection_epoch": epoch_id}
            summaries.append(epoch.summary(identity))
            if epoch.origin_ns is not None:
                last = (epoch.full.errors["controlled"].last_reference_ns - epoch.origin_ns) // INTERVAL_NS
                for i in range(last + 1):
                    full, common = epoch.intervals.get(i, (ThreePathStatistics(), ThreePathStatistics()))
                    intervals.append({**identity, "interval_index": i,
                                      "interval_start_reference_ns": epoch.origin_ns + i * INTERVAL_NS,
                                      "interval_end_reference_ns_exclusive": epoch.origin_ns + (i + 1) * INTERVAL_NS,
                                      "final_partial_interval": i == last,
                                      "full_tracking_period": full.record(), "common_comparison_period": common.record()})
        summary = {"epochs": summaries, "output_directory": str(output),
                   "successful_acceptance_count": sum(epoch.success_count for epoch in epochs.values()),
                   "error_statistics": "all errors are pre-update predictions; population standard deviation; reference minus prediction",
                   "common_period_definition": manifest["common_period"],
                   "intervals_file": "intervals.json", "acceptance_events_file": "acceptance_events.csv",
                   "source_observations": manifest["source_observations"]}
        write_json(output / "summary.json", summary)
        write_json(output / "intervals.json", {"interval_duration_ns": INTERVAL_NS,
                                               "boundaries": "[start,end) from first tracking prediction per epoch",
                                               "intervals": intervals})
        manifest["status"] = "completed"
        return summary
    except BaseException as exc:
        manifest.update(status="failed", failure=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        write_json(output / "manifest.json", manifest)


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline frozen versus explicit scale-only acceptance replay; no hardware.")
    parser.add_argument("observations", type=Path)
    parser.add_argument("--output-dir", type=Path, help="New destination outside source capture; must not exist.")
    parser.add_argument("--config-json", type=Path, help="Explicit complete original model configuration override.")
    args = parser.parse_args()
    output = args.output_dir or Path("output-files/clock-mesh-acceptance-replay") / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ_") + uuid4().hex[:8])
    try:
        result = replay_acceptance(args.observations, output, config_override=args.config_json)
    except (ValueError, OSError, TypeError, AttributeError) as exc:
        parser.exit(1, f"Acceptance replay failed: {exc}\n")
    for epoch in result["epochs"]:
        print(f"{epoch['address']} epoch {epoch['connection_epoch']}: acceptances="
              f"{epoch['successful_acceptance_count']}/{epoch['acceptance_attempt_count']}")
        for path, stats in epoch["common_comparison_period"].items():
            print(f"  {path}: common count={stats['tracking_error_count']}, mean ns={stats['mean_prediction_error_ns']}, "
                  f"population stddev ns={stats['prediction_error_stddev_ns']}")
        print(f"  nonpositive mapped increments={epoch['controlled_mapped_timestamp_increments']['nonpositive_increment_count']}; "
              f"post-acceptance error-time slope ns/s={epoch['remaining_controlled_bias_and_trend']['error_time_slope_ns_per_reference_second']}")
    print(f"Acceptance replay output: {result['output_directory']}")


if __name__ == "__main__":
    main()
