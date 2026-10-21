"""Plot ClockMesh observations without importing ClockMesh or accessing hardware.

Usage:
    python -m ClockMeshTest.plot_observations path/to/observations.csv
    python -m ClockMeshTest.plot_observations path/to/observations.csv --output timeline.png

Requires matplotlib and numpy. Motion channels are overlaid per DOT against mapped
timestamps, without signal shifts. Times use one shared gateway origin; residuals
are arrival minus mapped time, not independently measured synchronization error.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

from .motion import MOTION_CHANNELS

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def plot_observations(source: Path, output: Path, bin_seconds: float = 1.0) -> None:
    if not np.isfinite(bin_seconds) or bin_seconds <= 0:
        raise ValueError("bin-seconds must be finite and positive")
    with source.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"address", "sensor_id", "connection_epoch", "reference_time_ns",
                    "mapped_timestamp_ns", "estimated_scale_ns_per_source_unit"}
        columns = set(reader.fieldnames or [])
        missing = required - columns
        if missing:
            raise ValueError(f"Missing CSV columns: {sorted(missing)}")
        rows = list(reader)
    if not rows:
        raise ValueError("Observations CSV is empty")
    origin = min(int(row["reference_time_ns"]) for row in rows)
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        key = row["address"], row["sensor_id"], row["connection_epoch"]
        groups.setdefault(key, []).append(row)
    if not any(row["mapped_timestamp_ns"] for row in rows):
        raise ValueError("No mapped timestamps available to plot")

    plt.rcParams.update({"font.size": 10, "axes.spines.top": False,
                         "axes.spines.right": False})
    channels = [(column, unit) for column, _, unit in MOTION_CHANNELS
                if column in columns and any(row.get(column, "") for row in rows)]
    fig, axes = plt.subplots(3 + len(channels), 1,
                             figsize=(12, 10 + 2.3 * len(channels)), layout="constrained",
                             gridspec_kw={"height_ratios": [1, 1.35, 1] + [.8] * len(channels)})
    for axis in axes[4:]:
        axis.sharex(axes[3])
    colors = plt.get_cmap("tab20")
    end = max((int(row["reference_time_ns"]) - origin) / 1e9 for row in rows)
    motion_start, motion_end = 0.0, end
    any_acceptance = False
    for index, (key, device_rows) in enumerate(sorted(groups.items())):
        mapped_rows = [row for row in device_rows if row["mapped_timestamp_ns"]]
        if not mapped_rows:
            print(f"Skipping {key}: no mapped timestamps")
            continue
        color = colors(index % 20)
        label = f"{key[0]} / ID {key[1]} / epoch {key[2]}"
        t = np.array([(int(row["reference_time_ns"]) - origin) / 1e9
                      for row in mapped_rows])
        mapped = np.array([(int(row["mapped_timestamp_ns"]) - origin) / 1e9
                           for row in mapped_rows])
        motion_start = min(motion_start, float(mapped.min()))
        motion_end = max(motion_end, float(mapped.max()))
        for axis, (column, unit) in zip(axes[3:], channels):
            values = np.array([float(row[column]) if row.get(column, "") else np.nan
                               for row in mapped_rows])
            axis.plot(mapped, values, color=color, lw=1.2, label=label)
        # Subtract integers before converting to floating point, preserving ns precision.
        residual = np.array([(int(row["reference_time_ns"]) -
                              int(row["mapped_timestamp_ns"])) / 1e6
                             for row in mapped_rows])
        axes[0].plot(t, mapped, color=color, lw=1.4, label=label)
        axes[1].scatter(t, residual, color=color, s=4, alpha=.16)
        bins = np.floor(t / bin_seconds).astype(int)
        centers, means = [], []
        previous_bin = None
        for current_bin in np.unique(bins):
            if previous_bin is not None and current_bin > previous_bin + 1:
                centers.append(np.nan)
                means.append(np.nan)
            mask = bins == current_bin
            centers.append(t[mask].mean())
            means.append(residual[mask].mean())
            previous_bin = current_bin
        axes[1].plot(centers, means, color=color, lw=2, label=label)
        scales = np.array([float(row["estimated_scale_ns_per_source_unit"])
                           if row["estimated_scale_ns_per_source_unit"] else np.nan
                           for row in mapped_rows])
        axes[2].plot(t, scales, color=color, lw=1.4, label=label)
        accepted = [row for row in mapped_rows
                    if row.get("candidate_accepted", "").lower() in {"true", "1"}]
        if accepted:
            first = (int(accepted[0]["reference_time_ns"]) - origin) / 1e9
            any_acceptance = True
            for axis in axes[1:3]:
                axis.axvline(first, color=color, ls="--", alpha=.65, lw=1)
            print(f"{label}: first acceptance {first:.3f} s; {len(accepted)} acceptances")
        else:
            print(f"{label}: no acceptance recorded")
        print(f"  mapped rows={len(mapped_rows)}, residual mean={residual.mean():.3f} ms, "
              f"population std={residual.std():.3f} ms")
    axes[0].plot([0, end], [0, end], color="black", ls=":", lw=1,
                 label="Equal mapped / arrival time")
    axes[0].set(title="Sensor clocks mapped onto one gateway reference", ylabel="Mapped time (s)")
    axes[1].set(title=f"Arrival residuals: samples and {bin_seconds:g} s binned means",
                ylabel="Arrival − mapped time (ms)")
    axes[1].axhline(0, color="gray", lw=.8)
    title = "Applied clock rate"
    if any_acceptance:
        title += " — dashed lines mark first acceptance per sensor / epoch"
    axes[2].set(title=title, ylabel="Accepted scale (ns/source unit)")
    for axis in axes[:3]:
        axis.set_xlabel("Gateway time since first captured notification (s)")
        axis.set_xlim(0, max(end, 1))
        axis.grid(alpha=.2)
        axis.legend(loc="upper left", fontsize=8)
    for axis, (column, unit) in zip(axes[3:], channels):
        axis.set(title=f"Motion: {column}", ylabel=unit,
                 xlabel="Mapped time since first captured notification (s)",
                 xlim=(motion_start, max(motion_end, motion_start + 1)))
        axis.grid(alpha=.2)
        axis.legend(loc="upper left", fontsize=8)
    fig.suptitle("ClockMesh common timeline", fontsize=17, fontweight="bold")
    fig.supxlabel("Shared gateway origin; arrival residuals do not establish physical-event synchronization.",
                  fontsize=10)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        fig.savefig(output, dpi=180)
    finally:
        plt.close(fig)
    print(f"Graph saved: {output.resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations", type=Path)
    parser.add_argument("--output", type=Path,
                        help="PNG, PDF, or SVG destination; default: common_timeline.png beside CSV")
    parser.add_argument("--bin-seconds", type=float, default=1.0,
                        help="Residual averaging bin width (default: 1 second)")
    args = parser.parse_args()
    output = args.output or args.observations.with_name("common_timeline.png")
    if output.resolve() == args.observations.resolve():
        parser.error("Output must not overwrite the observations CSV")
    try:
        plot_observations(args.observations, output, args.bin_seconds)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Plot failed: {exc}\n")


if __name__ == "__main__":
    main()
