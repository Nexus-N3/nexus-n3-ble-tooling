"""Compare two DOT rotation timelines under accepted and candidate mappings.

Usage:
    python -m ClockMeshTest.compare_mappings path/to/observations.csv
    python -m ClockMeshTest.compare_mappings path/to/observations.csv \
        --output comparison.png --summary comparison.json

The comparison uses gyro-magnitude traces from paired measurement rows, linearly
interpolated onto a shared gateway timeline. Candidate timestamps are the
pre-observation full-fit predictions stored in the capture.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


MAPPINGS = {
    "accepted": "mapped_timestamp_ns",
    "candidate": "candidate_predicted_reference_ns",
}
REQUIRED_COLUMNS = {
    "address", "sensor_id", "connection_epoch", "stream_phase",
    "reference_time_ns", *MAPPINGS.values(),
    "gyro_x_deg_s", "gyro_y_deg_s", "gyro_z_deg_s",
}


@dataclass
class MotionTrace:
    label: str
    timestamps_ns: dict[str, list[int]]
    gyro_magnitude_deg_s: list[float]
    excluded_nonmonotonic_candidate_rows: int = 0


def _label(key: tuple[str, str, str]) -> str:
    return f"{key[0]} / ID {key[1]} / epoch {key[2]}"


def load_traces(source: Path) -> list[MotionTrace]:
    with source.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Missing CSV columns: {sorted(missing)}")
        grouped: dict[tuple[str, str, str], MotionTrace] = {}
        for row_number, row in enumerate(reader, start=2):
            if row["stream_phase"] != "measurement":
                continue
            if any(not row[column] for column in (*MAPPINGS.values(),
                                                  "gyro_x_deg_s", "gyro_y_deg_s",
                                                  "gyro_z_deg_s")):
                continue
            key = row["address"], row["sensor_id"], row["connection_epoch"]
            trace = grouped.setdefault(
                key, MotionTrace(_label(key), {name: [] for name in MAPPINGS}, []))
            try:
                timestamps = {name: int(row[column]) for name, column in MAPPINGS.items()}
                if (trace.timestamps_ns["candidate"] and
                        timestamps["candidate"] <= trace.timestamps_ns["candidate"][-1]):
                    trace.excluded_nonmonotonic_candidate_rows += 1
                    continue
                for name, column in MAPPINGS.items():
                    trace.timestamps_ns[name].append(timestamps[name])
                gyro = [float(row[f"gyro_{axis}_deg_s"]) for axis in "xyz"]
                if not all(math.isfinite(value) for value in gyro):
                    raise ValueError("non-finite gyro value")
                trace.gyro_magnitude_deg_s.append(math.sqrt(sum(value * value for value in gyro)))
            except ValueError as exc:
                raise ValueError(f"Invalid measurement data on CSV row {row_number}: {exc}") from exc

    if len(grouped) != 2:
        raise ValueError(
            f"Expected exactly two device/epoch traces with paired mapping and gyro data; "
            f"found {len(grouped)}")
    traces = [grouped[key] for key in sorted(grouped)]
    for trace in traces:
        for name, timestamps in trace.timestamps_ns.items():
            if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
                raise ValueError(f"{trace.label}: {name} timestamps are not strictly increasing")
        if len(trace.gyro_magnitude_deg_s) < 3:
            raise ValueError(f"{trace.label}: fewer than three paired measurement samples")
    return traces


def _aligned_pair(
    traces: list[MotionTrace], mapping: str, sample_interval_ns: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int, int]:
    timestamps = [np.asarray(trace.timestamps_ns[mapping], dtype=np.int64)
                  for trace in traces]
    values = [np.asarray(trace.gyro_magnitude_deg_s, dtype=float) for trace in traces]
    start_ns = max(int(times[0]) for times in timestamps)
    end_ns = min(int(times[-1]) for times in timestamps)
    if end_ns <= start_ns:
        raise ValueError(f"No common mapped-time interval for {mapping} mapping")
    count = (end_ns - start_ns) // sample_interval_ns + 1
    offsets_ns = np.arange(count, dtype=np.int64) * sample_interval_ns
    grid_ns = start_ns + offsets_ns
    aligned = [
        np.interp((grid_ns - start_ns) / 1e9, (times - start_ns) / 1e9, value)
        for times, value in zip(timestamps, values)
    ]
    return grid_ns, aligned[0], aligned[1], start_ns, end_ns


def _correlation(first: np.ndarray, second: np.ndarray) -> float:
    first = first - first.mean()
    second = second - second.mean()
    denominator = float(np.linalg.norm(first) * np.linalg.norm(second))
    return float(np.dot(first, second) / denominator) if denominator else float("nan")


def estimate_lag(
    first: np.ndarray, second: np.ndarray, sample_interval_ns: int, max_lag_ns: int,
) -> tuple[int, float, float]:
    """Return second-minus-first lag, peak correlation, and zero-lag correlation.

    A positive lag means the second trace's matching rotation occurs later than
    the first trace's on the selected mapping's timeline.
    """
    max_shift = max_lag_ns // sample_interval_ns
    if max_shift >= len(first) - 2:
        raise ValueError("Maximum lag leaves too few overlapping samples")
    zero_correlation = _correlation(first, second)
    best_lag_ns: int | None = None
    best_correlation = float("-inf")
    for shift in range(-max_shift, max_shift + 1):
        if shift > 0:
            left, right = first[:-shift], second[shift:]
        elif shift < 0:
            left, right = first[-shift:], second[:shift]
        else:
            left, right = first, second
        correlation = _correlation(left, right)
        if math.isfinite(correlation) and correlation > best_correlation:
            best_lag_ns = shift * sample_interval_ns
            best_correlation = correlation
    if best_lag_ns is None:
        raise ValueError("Gyro-magnitude traces do not have measurable variation")
    return best_lag_ns, best_correlation, zero_correlation


def compare_mappings(
    source: Path,
    output: Path,
    summary_path: Path,
    sample_interval_ms: float = 5.0,
    max_lag_ms: float = 500.0,
) -> dict:
    if not math.isfinite(sample_interval_ms) or sample_interval_ms <= 0:
        raise ValueError("sample-interval-ms must be finite and positive")
    if not math.isfinite(max_lag_ms) or max_lag_ms < 0:
        raise ValueError("max-lag-ms must be finite and nonnegative")
    sample_interval_ns = round(sample_interval_ms * 1e6)
    max_lag_ns = round(max_lag_ms * 1e6)
    if sample_interval_ns <= 0:
        raise ValueError("sample-interval-ms is too small to represent in nanoseconds")
    if max_lag_ns // sample_interval_ns >= 2**31:
        raise ValueError("max-lag-ms is too large")
    if source.resolve() in {output.resolve(), summary_path.resolve()}:
        raise ValueError("Output files must not overwrite the source CSV")
    if output.resolve() == summary_path.resolve():
        raise ValueError("Plot and summary destinations must differ")

    traces = load_traces(source)
    origin_ns = min(
        timestamp
        for trace in traces
        for timestamps in trace.timestamps_ns.values()
        for timestamp in timestamps
    )
    result: dict = {
        "source": str(source.resolve()),
        "shared_origin_ns": origin_ns,
        "method": (
            "Paired measurement rows; gyro angular-speed magnitude; linear interpolation "
            "onto a shared mapped-time grid; Pearson cross-correlation over the configured "
            "lag range. Candidate times are pre-observation full-fit predictions."
        ),
        "lag_definition": (
            "Positive means the second sorted device/epoch trace's corresponding movement "
            "occurs later than the first trace's on the mapping timeline."
        ),
        "sample_interval_ms": sample_interval_ns / 1e6,
        "maximum_tested_lag_ms": (max_lag_ns // sample_interval_ns) *
        sample_interval_ns / 1e6,
        "traces": [trace.label for trace in traces],
        "excluded_nonmonotonic_candidate_rows": {
            trace.label: trace.excluded_nonmonotonic_candidate_rows for trace in traces
        },
        "mappings": {},
    }

    plotted: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray, int, int]] = {}
    for mapping in MAPPINGS:
        grid_ns, first, second, start_ns, end_ns = _aligned_pair(
            traces, mapping, sample_interval_ns)
        lag_ns, peak_correlation, zero_correlation = estimate_lag(
            first, second, sample_interval_ns, max_lag_ns)
        result["mappings"][mapping] = {
            "common_overlap_seconds": (end_ns - start_ns) / 1e9,
            "estimated_second_minus_first_lag_ms": lag_ns / 1e6,
            "peak_pearson_correlation": peak_correlation,
            "zero_lag_pearson_correlation": zero_correlation,
        }
        plotted[mapping] = grid_ns, first, second, start_ns, end_ns

    accepted_lag = result["mappings"]["accepted"]["estimated_second_minus_first_lag_ms"]
    candidate_lag = result["mappings"]["candidate"]["estimated_second_minus_first_lag_ms"]
    result["candidate_minus_accepted_lag_change_ms"] = candidate_lag - accepted_lag
    result["absolute_lag_reduction_ms"] = abs(accepted_lag) - abs(candidate_lag)
    result["interpretation_limit"] = (
        "This compares the recorded motion traces under two mappings; interpolation and "
        "cross-correlation have finite time resolution. It does not establish absolute "
        "accuracy because both mappings use delayed gateway arrivals as their reference."
    )

    fig, axes = plt.subplots(2, 1, figsize=(12, 8), layout="constrained", sharex=True)
    colors = ("tab:blue", "tab:orange")
    for axis, mapping in zip(axes, MAPPINGS):
        grid_ns, first, second, _, _ = plotted[mapping]
        x_seconds = (grid_ns - origin_ns) / 1e9
        lag_ms = result["mappings"][mapping]["estimated_second_minus_first_lag_ms"]
        correlation = result["mappings"][mapping]["peak_pearson_correlation"]
        axis.plot(x_seconds, first, color=colors[0], lw=1.2, label=traces[0].label)
        axis.plot(x_seconds, second, color=colors[1], lw=1.2, label=traces[1].label)
        axis.set(title=f"{mapping.title()} mapping: estimated relative lag {lag_ms:+.1f} ms "
                   f"(peak correlation {correlation:.4f})",
                 ylabel="Gyro magnitude (deg/s)")
        axis.grid(alpha=.2)
        axis.legend(loc="upper right", fontsize=8)
    axes[-1].set_xlabel("Mapped time since shared earliest-mapping origin (s)")
    fig.suptitle("Same captured rotations under accepted and candidate clock mappings",
                 fontsize=15, fontweight="bold")
    output.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fig.savefig(output, dpi=180)
    finally:
        plt.close(fig)
    summary_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"Traces: {traces[0].label} and {traces[1].label}")
    print(f"Grid: {sample_interval_ns / 1e6:g} ms; tested lag range: "
          f"+/-{result['maximum_tested_lag_ms']:g} ms")
    for mapping, values in result["mappings"].items():
        print(f"{mapping.title()}: second-minus-first lag "
              f"{values['estimated_second_minus_first_lag_ms']:+.1f} ms; "
              f"peak correlation {values['peak_pearson_correlation']:.4f}; "
              f"overlap {values['common_overlap_seconds']:.3f} s")
    print(f"Absolute lag reduction under candidate: {result['absolute_lag_reduction_ms']:.1f} ms")
    print(f"Plot saved: {output.resolve()}")
    print(f"Summary saved: {summary_path.resolve()}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations", type=Path)
    parser.add_argument("--output", type=Path,
                        help="PNG, PDF, or SVG plot; default: mapping_comparison.png beside CSV")
    parser.add_argument("--summary", type=Path,
                        help="JSON summary; default: mapping_comparison.json beside CSV")
    parser.add_argument("--sample-interval-ms", type=float, default=5.0,
                        help="Shared interpolation grid interval (default: 5 ms)")
    parser.add_argument("--max-lag-ms", type=float, default=500.0,
                        help="Maximum absolute cross-correlation lag (default: 500 ms)")
    args = parser.parse_args()
    output = args.output or args.observations.with_name("mapping_comparison.png")
    summary = args.summary or args.observations.with_name("mapping_comparison.json")
    try:
        compare_mappings(args.observations, output, summary,
                         args.sample_interval_ms, args.max_lag_ms)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Comparison failed: {exc}\n")


if __name__ == "__main__":
    main()
