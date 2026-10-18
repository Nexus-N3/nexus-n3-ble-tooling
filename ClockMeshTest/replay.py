"""Offline pre-update candidate comparisons from raw saved timestamp pairs."""
from __future__ import annotations

import argparse
from collections import deque
import csv
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from uuid import uuid4

from clock_mesh import AffineClockModel, ClockAlignmentConfig, ClockModelState

from .candidates import CANDIDATE_FIELDS, observe_with_candidate
from .evidence import ErrorStatistics, INTERVAL_NS
from .stream_client import software_info


REPLAY_FIELDS = [
    "source_row", "address", "sensor_id", "connection_epoch", "recorded_stream_phase",
    "raw_dot_timestamp_us", "gateway_timestamp_us", "reference_time_ns",
    "model_observation_supplied", "stream_phase", "model_state", "observation_count",
    "accepted_predicted_reference_ns", "accepted_prediction_error_ns",
    *CANDIDATE_FIELDS, "retained_tracking_reference_span_ns",
    "candidate_fit_observation_count", "candidate_fit_reference_span_ns",
    "candidate_prediction_uses_full_window", "common_evaluation_period",
    "accepted_scale_ns_per_source_unit", "retained_calibration_observation_count",
]


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def file_identity(path: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": str(path.resolve()), "sha256": digest.hexdigest()}


def load_configuration(source: Path, override: Path | None) -> tuple[ClockAlignmentConfig, dict]:
    manifest_path = source.with_name("manifest.json")
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    original = manifest.get("configuration", {}).get("clock_alignment") if manifest else None
    values = json.loads(override.read_text()) if override else original
    if not isinstance(values, dict):
        raise ValueError("Required model configuration missing: adjacent manifest.configuration.clock_alignment or --config-json is required")
    known = {f.name for f in fields(ClockAlignmentConfig)}
    # Older captures predate these tracking-policy APIs. Comparison replay never
    # accepts candidates; acceptance replay explicitly overrides both policy fields.
    new_policy_fields = {"max_tracking_observations", "min_tracking_acceptance_span_ns"}
    missing = known - new_policy_fields - values.keys()
    unknown = values.keys() - known
    if missing or unknown:
        raise ValueError(f"Invalid/incomplete model configuration: missing={sorted(missing)}, unknown={sorted(unknown)}")
    config = ClockAlignmentConfig(**values)
    for field in fields(config):
        value = getattr(config, field.name)
        if field.name == "nominal_scale_ns_per_source_unit":
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or
                                      not math.isfinite(value) or value <= 0):
                raise ValueError("Invalid nominal source scale")
        elif type(value) is not int or value <= 0:
            raise ValueError(f"Invalid configuration value: {field.name}")
    if config.max_calibration_observations < config.min_learned_observations:
        raise ValueError("Calibration storage cannot accommodate the minimum observation count")
    return config, {"adjacent_manifest": file_identity(manifest_path) if manifest else None,
                    "original_capture_manifest": manifest,
                    "original_model_configuration": original,
                    "configuration_override": {**file_identity(override), "values": values} if override else None,
                    "configuration_missing_tracking_limit": "max_tracking_observations" not in values,
                    "new_policy_defaults_for_older_capture": {name: getattr(config, name)
                                                               for name in new_policy_fields - values.keys()},
                    "calibration_storage_policy": "preserve recovered/explicit max_calibration_observations independently of tracking limits"}


@dataclass
class ScaleStatistics:
    count: int = 0
    mean: float = 0
    m2: float = 0
    minimum: float | None = None
    maximum: float | None = None
    first: float | None = None
    last: float | None = None

    def add(self, value: float | None) -> None:
        if value is None:
            return
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        self.m2 += delta * (value - self.mean)
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        if self.first is None:
            self.first = value
        self.last = value

    def record(self) -> dict:
        return {"count": self.count, "unit": "ns per source unit", "first": self.first, "last": self.last,
                "mean": self.mean if self.count else None,
                "population_stddev": math.sqrt(max(self.m2 / self.count, 0)) if self.count else None,
                "minimum": self.minimum, "maximum": self.maximum}


class ErrorPair:
    def __init__(self):
        self.accepted = ErrorStatistics()
        self.candidate = ErrorStatistics()
        self.accepted_paired = ErrorStatistics()

    def add(self, row: dict) -> None:
        reference = row["reference_time_ns"]
        accepted, candidate = row["accepted_prediction_error_ns"], row["candidate_prediction_error_ns"]
        if accepted is not None:
            self.accepted.add(accepted, reference)
        if candidate is not None:
            self.candidate.add(candidate, reference)
            if accepted is not None:
                self.accepted_paired.add(accepted, reference)

    def record(self) -> dict:
        return {"accepted": self.accepted.record(), "candidate": self.candidate.record(),
                "candidate_predictions_available": self.candidate.count,
                "accepted_on_candidate_available_rows": self.accepted_paired.record()}


class EpochReplay:
    def __init__(self, config: ClockAlignmentConfig):
        self.model = AffineClockModel(config)
        self.references: deque[int] = deque(maxlen=config.max_tracking_observations)
        self.previous: tuple[int, int] | None = None
        self.origin: int | None = None
        self.full = ErrorPair()
        self.common = ErrorPair()
        self.partial_window = ErrorPair()
        self.full_window = ErrorPair()
        self.scales = ScaleStatistics()
        self.intervals: dict[int, tuple[ErrorPair, ScaleStatistics]] = {}
        self.common_start: int | None = None

    def span(self) -> int | None:
        return self.references[-1] - self.references[0] if self.references else None

    def process(self, source_time: int, reference: int, supplied: bool) -> dict:
        model = self.model
        row = dict.fromkeys(CANDIDATE_FIELDS)
        row.update(accepted_predicted_reference_ns=None, accepted_prediction_error_ns=None,
                   candidate_fit_observation_count=None, candidate_fit_reference_span_ns=None,
                   candidate_prediction_uses_full_window=False, stream_phase="acquisition")
        if supplied:
            if self.previous is not None and (source_time <= self.previous[0] or reference < self.previous[1]):
                raise ValueError("Discontinuous supplied raw timestamps within an epoch; cannot replay as normal data")
            self.previous = source_time, reference
            tracking = model.state in {ClockModelState.ALIGNED, ClockModelState.TRACKING}
            row["stream_phase"] = "measurement" if tracking else "calibration"
            prediction = model.map(source_time) if tracking else None
            row.update(accepted_predicted_reference_ns=prediction,
                       accepted_prediction_error_ns=reference - prediction if prediction is not None else None,
                       candidate_fit_observation_count=model.retained_tracking_observation_count,
                       candidate_fit_reference_span_ns=self.span(),
                       candidate_prediction_uses_full_window=(model.retained_tracking_observation_count ==
                                                               model.config.max_tracking_observations))
            row.update(observe_with_candidate(model, source_time, reference))
            if tracking:
                self.references.append(reference)
                if len(self.references) != model.retained_tracking_observation_count:
                    raise ValueError("Tracking-window mirror differs from the ClockMesh API count")
            elif (model.state not in {ClockModelState.ALIGNED, ClockModelState.TRACKING} and
                  model.observation_count >= model.config.max_calibration_observations):
                raise ValueError("Calibration storage exhausted before alignment; provide adequate original calibration capacity via --config-json")
        row.update(model_state=model.state.value, observation_count=model.observation_count,
                   accepted_scale_ns_per_source_unit=model.estimated_scale_ns_per_source_unit,
                   retained_calibration_observation_count=model.retained_observation_count,
                   retained_tracking_reference_span_ns=self.span())
        return row

    def collect(self, row: dict) -> None:
        if row["accepted_prediction_error_ns"] is None:
            return
        ref = row["reference_time_ns"]
        if self.origin is None:
            self.origin = ref
        self.full.add(row)
        if row["candidate_prediction_error_ns"] is not None:
            (self.full_window if row["candidate_prediction_uses_full_window"] else self.partial_window).add(row)
        if row["common_evaluation_period"]:
            if self.common_start is None:
                self.common_start = ref
            self.common.add(row)
        self.scales.add(row["candidate_scale_ns_per_source_unit"])
        pair, scales = self.intervals.setdefault((ref - self.origin) // INTERVAL_NS, (ErrorPair(), ScaleStatistics()))
        pair.add(row)
        scales.add(row["candidate_scale_ns_per_source_unit"])

    def summary(self, identity: dict) -> dict:
        return {**identity, "full_tracking_period": self.full.record(),
                "common_evaluation_start_reference_ns": self.common_start,
                "common_evaluation_period": self.common.record(),
                "candidate_predictions_from_partial_window": self.partial_window.record(),
                "candidate_predictions_from_full_window": self.full_window.record(),
                "candidate_scale_evolution": {**self.scales.record(), "source_file": "observations.csv",
                                              "timing": "post-update; descriptive, not prediction-error evidence"},
                "final_retained_tracking_observation_count": self.model.retained_tracking_observation_count,
                "final_retained_tracking_reference_span_ns": self.span(),
                "final_model_state": self.model.state.value,
                "retained_calibration_observation_count": self.model.retained_observation_count}


def replay(source: Path, output: Path, *, windows=(300, 600, 1800), config_override: Path | None = None) -> dict:
    source, output = source.resolve(), output.resolve()
    if output == source.parent or source.parent in output.parents:
        raise ValueError("Replay destination must be outside the source capture directory")
    if output.exists():
        raise ValueError("Replay destination already exists; choose a new directory")
    if not windows or len(set(windows)) != len(windows) or any(type(n) is not int or n < 2 for n in windows):
        raise ValueError("Tracking windows must be distinct observation counts >= 2")
    config, recovery = load_configuration(source, config_override)
    required = {"address", "sensor_id", "connection_epoch", "raw_dot_timestamp_us",
                "gateway_timestamp_us", "model_observation_supplied"}
    # Validate required columns before creating output; saved model fields are not inputs.
    with source.open(newline="", encoding="utf-8") as handle:
        if not required.issubset(csv.DictReader(handle).fieldnames or []):
            raise ValueError("Required raw timestamp/device/epoch/supply columns are missing")
    manifest = {"schema_version": 1, "status": "running", "source_observations": file_identity(source),
                "source_capture": recovery, "software": software_info(),
                "tracking_window_observation_counts": list(windows),
                "replay_configurations": {str(n): asdict(replace(config, max_tracking_observations=n)) for n in windows},
                "explicit_overrides": {"max_tracking_observations": list(windows)},
                "prediction_timing": "accepted and candidate predictions before observe; candidate scale/count/span after observe",
                "common_period": "per device/epoch, only rows where all compared pre-update windows are full and predict",
                "window_units": "observation counts, not guaranteed durations; actual reference spans recorded",
                "intervals": "five-second [start,end) bins from first accepted tracking prediction per epoch; final partial preserved",
                "mapping_policy": "accepted mapping fixed; no candidate acceptance; no host-time inputs or acquisition sleeps"}
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "manifest.json", manifest)
    models = {n: {} for n in windows}
    handles = []
    writers = {}
    try:
        for n in windows:
            folder = output / f"window_{n}"
            folder.mkdir()
            handle = (folder / "observations.csv").open("w", newline="", encoding="utf-8")
            handles.append(handle)
            writers[n] = csv.DictWriter(handle, REPLAY_FIELDS)
            writers[n].writeheader()
            write_json(folder / "manifest.json", {**manifest, "selected_model_configuration": manifest["replay_configurations"][str(n)]})
        with source.open(newline="", encoding="utf-8") as handle:
            for index, raw in enumerate(csv.DictReader(handle), start=1):
                flag = raw["model_observation_supplied"].lower()
                if flag not in {"true", "false", "1", "0"}:
                    raise ValueError(f"Invalid model_observation_supplied on row {index}")
                supplied = flag in {"true", "1"}
                key = (raw["address"], int(raw["sensor_id"]), int(raw["connection_epoch"]))
                source_time, gateway = int(raw["raw_dot_timestamp_us"]), int(raw["gateway_timestamp_us"])
                if not key[0] or not 0 <= source_time < 2**32 or not 0 <= gateway < 2**64:
                    raise ValueError(f"Invalid raw timestamp/device identity on row {index}")
                reference = gateway * 1000
                if raw.get("reference_time_ns", "") and int(raw["reference_time_ns"]) != reference:
                    raise ValueError(f"Raw gateway/reference conversion mismatch on row {index}")
                outputs = {}
                for n in windows:
                    if key not in models[n]:
                        models[n][key] = EpochReplay(replace(config, max_tracking_observations=n))
                    row = models[n][key].process(source_time, reference, supplied)
                    outputs[n] = {**row, "source_row": index, "address": key[0], "sensor_id": key[1],
                                  "connection_epoch": key[2], "raw_dot_timestamp_us": source_time,
                                  "gateway_timestamp_us": gateway, "reference_time_ns": reference,
                                  "recorded_stream_phase": raw.get("stream_phase", ""),
                                  "model_observation_supplied": supplied}
                common = all(r["candidate_prediction_uses_full_window"] and
                             r["candidate_prediction_error_ns"] is not None for r in outputs.values())
                # Assert the fixed mapping is independent of candidate window selection.
                if len({r["accepted_predicted_reference_ns"] for r in outputs.values()}) != 1:
                    raise ValueError("Accepted predictions differ across tracking window configurations")
                for n, row in outputs.items():
                    row["common_evaluation_period"] = common
                    models[n][key].collect(row)
                    writers[n].writerow(row)
        if not any(epoch.model.observation_count for epoch in models[windows[0]].values()):
            raise ValueError("No supplied observations available to replay")
        comparison = {"configurations": {}, "common_period_definition": manifest["common_period"],
                      "source_observations": manifest["source_observations"], "output_directory": str(output)}
        for n in windows:
            epochs, intervals = [], []
            for (address, sensor_id, epoch_id), epoch in models[n].items():
                identity = {"address": address, "sensor_id": sensor_id, "connection_epoch": epoch_id}
                epochs.append(epoch.summary(identity))
                if epoch.origin is not None:
                    last = (epoch.full.accepted.last_reference_ns - epoch.origin) // INTERVAL_NS
                    for i in range(last + 1):
                        pair, scales = epoch.intervals.get(i, (ErrorPair(), ScaleStatistics()))
                        intervals.append({**identity, "interval_index": i,
                                          "interval_start_reference_ns": epoch.origin + i * INTERVAL_NS,
                                          "interval_end_reference_ns_exclusive": epoch.origin + (i + 1) * INTERVAL_NS,
                                          "final_partial_interval": i == last,
                                          **pair.record(), "candidate_scale_post_update": scales.record()})
            result = {"max_tracking_observations": n, "epochs": epochs,
                      "per_observation_file": "observations.csv", "five_second_intervals_file": "intervals.json",
                      "error_standard_deviation": "population; errors use pre-update predictions"}
            write_json(output / f"window_{n}" / "summary.json", result)
            write_json(output / f"window_{n}" / "intervals.json", {"intervals": intervals})
            comparison["configurations"][str(n)] = result
        write_json(output / "comparison.json", comparison)
        manifest["status"] = "completed"
        return comparison
    except BaseException as exc:
        manifest.update(status="failed", failure=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        for handle in handles:
            handle.close()
        write_json(output / "manifest.json", manifest)
        for n in windows:
            path = output / f"window_{n}" / "manifest.json"
            if path.exists():
                write_json(path, {**manifest, "selected_model_configuration": manifest["replay_configurations"][str(n)]})


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay raw DOT→gateway pairs to compare pre-update tracking candidates; no hardware.")
    parser.add_argument("observations", type=Path)
    parser.add_argument("--output-dir", type=Path, help="New destination, outside source capture; must not exist.")
    parser.add_argument("--windows", type=int, nargs="+", default=[300, 600, 1800])
    parser.add_argument("--config-json", type=Path, help="Explicit complete model configuration override if recovery is unavailable/inappropriate.")
    args = parser.parse_args()
    output = args.output_dir or Path("output-files/clock-mesh-replay") / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ_") + uuid4().hex[:8])
    try:
        comparison = replay(args.observations, output, windows=tuple(args.windows), config_override=args.config_json)
    except (ValueError, OSError, TypeError, AttributeError) as exc:
        parser.exit(1, f"Replay failed: {exc}\n")
    for window, result in comparison["configurations"].items():
        for epoch in result["epochs"]:
            common = epoch["common_evaluation_period"]
            print(f"Window {window}, epoch {epoch['connection_epoch']}: candidate predictions="
                  f"{epoch['full_tracking_period']['candidate_predictions_available']}; common count="
                  f"{common['candidate_predictions_available']}, accepted/candidate mean ns="
                  f"{common['accepted']['mean_prediction_error_ns']}/{common['candidate']['mean_prediction_error_ns']}, "
                  f"population stddev ns={common['accepted']['prediction_error_stddev_ns']}/"
                  f"{common['candidate']['prediction_error_stddev_ns']}")
    print(f"Replay output: {comparison['output_directory']}")


if __name__ == "__main__":
    main()
