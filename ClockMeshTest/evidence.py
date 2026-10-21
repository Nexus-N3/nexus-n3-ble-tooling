"""Post-capture prediction-error statistics from saved observation evidence."""
from __future__ import annotations

import csv
from dataclasses import dataclass
import json
from pathlib import Path


INTERVAL_NS = 5_000_000_000


@dataclass
class ErrorStatistics:
    """Constant-space exact integer sums; convert to floats only for output."""

    count: int = 0
    total: int = 0
    sum_squares: int = 0
    minimum: int | None = None
    maximum: int | None = None
    first_reference_ns: int | None = None
    last_reference_ns: int | None = None

    def add(self, error_ns: int, reference_ns: int) -> None:
        self.count += 1
        self.total += error_ns
        self.sum_squares += error_ns * error_ns
        self.minimum = error_ns if self.minimum is None else min(self.minimum, error_ns)
        self.maximum = error_ns if self.maximum is None else max(self.maximum, error_ns)
        if self.first_reference_ns is None:
            self.first_reference_ns = reference_ns
        self.last_reference_ns = reference_ns

    def record(self) -> dict:
        # Algebraically identical to population variance, without float cancellation.
        variance = ((self.count * self.sum_squares - self.total * self.total) /
                    (self.count * self.count)) if self.count else None
        return {
            "tracking_error_count": self.count,
            "mean_prediction_error_ns": self.total / self.count if self.count else None,
            "prediction_error_stddev_ns": variance ** 0.5 if variance is not None else None,
            "min_prediction_error_ns": self.minimum,
            "max_prediction_error_ns": self.maximum,
            "first_observation_reference_time_ns": self.first_reference_ns,
            "last_observation_reference_time_ns": self.last_reference_ns,
            "actual_observation_span_ns": (self.last_reference_ns - self.first_reference_ns
                                           if self.count else None),
        }


def summarize_observations(path: Path) -> dict:
    """Stream CSV once; retain aggregates per epoch/interval, never raw errors.

    Intervals are [start, end), relative to each epoch's first tracking observation.
    Include empty intervals between observations and the final partial interval.
    Epochs without tracking observations have a null-valued full-epoch summary.
    """
    epochs = {}
    required = {"address", "sensor_id", "connection_epoch", "stream_phase", "model_state",
                "model_observation_supplied", "latest_prediction_error_ns", "reference_time_ns"}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("Observation CSV lacks required prediction-error evidence fields")
        for row in reader:
            key = (row["address"], int(row["sensor_id"]), int(row["connection_epoch"]))
            epoch = epochs.setdefault(key, {"all": ErrorStatistics(), "intervals": {}})
            # Exclude acquisition, calibration, and unsupplied/late observations,
            # even if a previous error accidentally appears in such a record.
            if (row["stream_phase"] != "measurement" or row["model_state"] != "tracking" or
                    row["model_observation_supplied"].lower() not in {"true", "1"} or
                    row["latest_prediction_error_ns"] == ""):
                continue
            reference_ns = int(row["reference_time_ns"])
            error_ns = int(row["latest_prediction_error_ns"])
            full = epoch["all"]
            if full.last_reference_ns is not None and reference_ns < full.last_reference_ns:
                raise ValueError("Tracking reference time went backward within one epoch")
            full.add(error_ns, reference_ns)
            index = (reference_ns - full.first_reference_ns) // INTERVAL_NS
            epoch["intervals"].setdefault(index, ErrorStatistics()).add(error_ns, reference_ns)
    full_records, interval_records = [], []
    for (address, sensor_id, epoch_id), epoch in epochs.items():
        identity = {"address": address, "sensor_id": sensor_id, "connection_epoch": epoch_id}
        full = epoch["all"]
        full_records.append({**identity, "scope": "all tracking observations in epoch", **full.record()})
        if not full.count:
            continue
        final_index = (full.last_reference_ns - full.first_reference_ns) // INTERVAL_NS
        for index in range(final_index + 1):
            start_ns = full.first_reference_ns + index * INTERVAL_NS
            stats = epoch["intervals"].get(index, ErrorStatistics())
            interval_records.append({
                **identity, "scope": "non-overlapping five-second tracking interval",
                "interval_index": index, "tracking_origin_reference_time_ns": full.first_reference_ns,
                "interval_start_offset_ns": index * INTERVAL_NS,
                "interval_end_offset_ns_exclusive": (index + 1) * INTERVAL_NS,
                "interval_start_reference_time_ns": start_ns,
                "interval_end_reference_time_ns_exclusive": start_ns + INTERVAL_NS,
                "final_partial_interval": index == final_index,
                **stats.record(),
            })
    return {"schema_version": 1, "source_file": path.name,
            "prediction_error_definition": "observed reference minus accepted fixed mapping",
            "standard_deviation": "population", "interval_duration_ns": INTERVAL_NS,
            "interval_boundary_convention": "[start, end); origin is first tracking observation in each epoch",
            "final_partial_interval_definition": "coverage ends at last tracking observation, not nominal interval end",
            "model_retained_window": "separate diagnostics in summary.experiments (summary.experiment for one DOT) and summary.ended_epochs; not used here",
            "epochs": full_records, "intervals": interval_records}


def write_prediction_error_report(observations: Path, intervals_path: Path) -> dict:
    report = summarize_observations(observations)
    intervals = report.pop("intervals")
    intervals_path.write_text(json.dumps({**report, "intervals": intervals}, indent=2) + "\n",
                              encoding="utf-8")
    return {**report, "interval_results_file": intervals_path.name, "interval_record_count": len(intervals)}


def console_summary(summary: dict) -> list[str]:
    lines = [f"Capture {summary['status']}"]
    experiments = [*summary.get("ended_epochs", [])]
    if "experiments" in summary:
        experiments.extend(summary["experiments"])
    elif "experiment" in summary:
        experiments.append(summary["experiment"])
    epochs = summary.get("prediction_error_statistics", {}).get("epochs", [])
    for exp in experiments:
        lines.append(f"DOT {exp['address']} (sensor_id={exp['sensor_id']}) epoch {exp['connection_epoch']}: stability={exp['stability_gate_outcome']}, "
                     f"calibration={exp['calibration_outcome']}, duration_s={exp['calibration_duration_seconds']}, "
                     f"scale_ns/source_unit={exp['learned_scale_ns_per_source_unit']}")
        full = next((r for r in epochs if (r['address'], r['sensor_id'], r['connection_epoch']) ==
                     (exp['address'], exp['sensor_id'], exp['connection_epoch'])), None)
        if full:
            lines.append("  Full-epoch prediction errors (ns): "
                         f"count={full['tracking_error_count']} mean={full['mean_prediction_error_ns']} "
                         f"population_stddev={full['prediction_error_stddev_ns']} "
                         f"min={full['min_prediction_error_ns']} max={full['max_prediction_error_ns']}")
        lines.append("  Retained-window prediction errors (ns): "
                     f"count={exp['retained_tracking_error_count']} mean={exp['mean_prediction_error_ns']} "
                     f"population_stddev={exp['prediction_error_stddev_ns']}")
    for key in ("failure", "analysis_error", "cleanup_errors"):
        if summary.get(key):
            lines.append(f"{key}: {summary[key]}")
    return lines
