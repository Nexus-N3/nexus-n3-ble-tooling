"""Payload layout and motion timelines, independent of ClockMesh/hardware."""
import csv

import pytest

from ClockMeshTest.motion import decode_motion, MOTION_FIELDS
from MovellaDot.profile import MOVELLA_START_HEX, MOVELLA_IMU_PREFIX, parse_packet


# uint32 0x12345678, then float32 values 1,-2,3,-4,5,-6,7,-8,9,-10.
PAYLOAD = bytes.fromhex(
    "78563412 0000803f 000000c0 00004040 000080c0 0000a040 "
    "0000c0c0 0000e040 000000c1 00001041 000020c1")


def test_motion_uses_existing_configured_payload_decoder():
    assert MOVELLA_START_HEX == "01011A"
    assert MOVELLA_IMU_PREFIX.format == "<I10f"
    assert len(PAYLOAD) == MOVELLA_IMU_PREFIX.size == 44
    assert parse_packet(PAYLOAD)["timestamp_us"] == 0x12345678
    expected = dict(zip(MOTION_FIELDS, [1, -2, 3, -4, 5, -6, 7, -8, 9, -10]))
    assert decode_motion(PAYLOAD) == expected
    assert decode_motion(PAYLOAD + b"trailing bytes") == expected


@pytest.mark.parametrize("length", [0, 4, 20, 43])
def test_motion_rejects_truncated_payload(length):
    with pytest.raises(ValueError, match="too short for IMU decode"):
        decode_motion(PAYLOAD[:length])


def plot_fixture(tmp_path, *, motion=True):
    origin = 10**18 + 123
    rows = []
    for address, sensor_id, epoch, reference, mapped, value in [
        ("A", 1, 1, 0, None, 0),
        ("A", 1, 1, 1_100_000_000, 1_000_000_000, 1),
        ("B", 2, 1, 1_350_000_000, 1_250_000_000, 1),
        ("A", 1, 1, 2_100_000_000, 2_000_000_000, -1),
        ("B", 2, 1, 2_350_000_000, 2_250_000_000, -1),
        ("A", 1, 2, 3_000_000_000, -250_000_000, 2),
    ]:
        row = dict(address=address, sensor_id=sensor_id, connection_epoch=epoch,
                   reference_time_ns=origin + reference,
                   mapped_timestamp_ns=origin + mapped if mapped is not None else "",
                   estimated_scale_ns_per_source_unit=1000, candidate_accepted=False)
        if motion:
            row.update(accel_x_m_s2=value, gyro_z_deg_s=value * 10)
        rows.append(row)
    source = tmp_path / "observations.csv"
    with source.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return source


def capture_figure(monkeypatch):
    pytest.importorskip("matplotlib")
    pytest.importorskip("numpy")
    from matplotlib.figure import Figure
    from ClockMeshTest import plot_observations as module
    saved = []
    original = Figure.savefig

    def savefig(figure, *args, **kwargs):
        saved.append(figure)
        return original(figure, *args, **kwargs)

    monkeypatch.setattr(Figure, "savefig", savefig)
    return module, saved


def test_plot_motion_preserves_shared_mapped_time_and_epoch_identity(monkeypatch, tmp_path):
    module, saved = capture_figure(monkeypatch)
    source = plot_fixture(tmp_path)
    output = tmp_path / "timeline.svg"
    module.plot_observations(source, output)
    assert output.stat().st_size > 0
    figure, = saved
    assert len(figure.axes) == 5  # three existing timing diagnostics, two motion channels
    assert "Arrival residuals" in figure.axes[1].get_title()
    assert figure.axes[2].get_ylabel() == "Accepted scale (ns/source unit)"
    for axis in figure.axes[3:]:
        assert len(axis.lines) == 3  # two DOTs, with A's second epoch separate
        by_label = {line.get_label(): line for line in axis.lines}
        assert list(by_label["A / ID 1 / epoch 1"].get_xdata()) == [1, 2]
        assert list(by_label["B / ID 2 / epoch 1"].get_xdata()) == [1.25, 2.25]
        assert list(by_label["A / ID 1 / epoch 2"].get_xdata()) == [-.25]
        assert axis.get_xlim()[0] == -.25
        assert "Mapped time" in axis.get_xlabel()
    assert list(figure.axes[3].lines[0].get_ydata()) == [1, -1]
    assert list(figure.axes[4].lines[0].get_ydata()) == [10, -10]
    assert figure.axes[3].get_ylabel() == "m/s²"
    assert figure.axes[4].get_ylabel() == "deg/s"
    assert not module.plt.get_fignums()


def test_plot_older_timing_only_csv(monkeypatch, tmp_path):
    module, saved = capture_figure(monkeypatch)
    module.plot_observations(plot_fixture(tmp_path, motion=False), tmp_path / "timing.svg")
    assert len(saved[0].axes) == 3


def test_plot_all_decoded_motion_channels(monkeypatch, tmp_path):
    module, saved = capture_figure(monkeypatch)
    source = plot_fixture(tmp_path)
    with source.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        for column in MOTION_FIELDS:
            row.setdefault(column, "0.5")
    with source.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    module.plot_observations(source, tmp_path / "all_motion.png")
    assert len(saved[0].axes) == 13
    assert [axis.get_title() for axis in saved[0].axes[3:]] == [
        f"Motion: {column}" for column in MOTION_FIELDS]
    assert all(len(axis.lines) == 3 for axis in saved[0].axes[3:])
