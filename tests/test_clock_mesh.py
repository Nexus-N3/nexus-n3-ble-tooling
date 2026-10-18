"""Hardware-free evidence and lifecycle tests using the real ClockMesh model."""
from contextlib import nullcontext
from dataclasses import asdict
import csv
import json
from pathlib import Path
import struct
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from clock_mesh import ClockAlignmentConfig

from ClockMeshTest.alignment import DotExperiment, ExperimentFailure, RECORD_FIELDS
from ClockMeshTest import stream_client
from ClockMeshTest.evidence import ErrorStatistics, INTERVAL_NS, summarize_observations
from ClockMeshTest.provenance import timestamp_provenance
from ClockMeshTest.candidates import CANDIDATE_FIELDS, observe_with_candidate
from ClockMeshTest.replay import replay, load_configuration
from ClockMeshTest.acceptance_replay import AcceptanceEpoch, BiasTrend, replay_acceptance
from NexusBLESdk import GatewayClient, SensorConnection, StreamFrame


def frame(dot_us, gateway_us=None):
    return StreamFrame(1, dot_us + 10_000_000 if gateway_us is None else gateway_us,
                       struct.pack("<I", dot_us))


def experiment(**kwargs):
    return DotExperiment(SensorConnection("AA:BB", 1), started_at=0,
                         calibration_config=kwargs.pop("config", ClockAlignmentConfig()),
                         no_data_seconds=kwargs.pop("no_data_seconds", 3), **kwargs)


def align(exp):
    exp.monitor.measurement_active = True
    exp.stability_outcome = "passed"
    rows = [exp.process(frame(round(i * 1e6 / 60)), i / 60) for i in range(121)]
    return rows


def test_conversion_and_no_model_feed_before_gate():
    exp = experiment()
    row = exp.process(frame(0x12345678, 2**40 + 123), 0)
    assert row["raw_dot_timestamp_us"] == 0x12345678
    assert row["unwrapped_dot_timestamp_us"] == 0x12345678
    assert row["reference_time_ns"] == (2**40 + 123) * 1000
    assert row["stream_phase"] == "acquisition"
    assert row["observation_count"] == 0
    assert row["mapped_timestamp_ns"] is None
    assert row["latest_prediction_error_ns"] is None
    assert exp.model.config.nominal_scale_ns_per_source_unit is None


def test_binary_parser_pairs_timestamp_with_same_payload():
    gateway = GatewayClient(object(), verbose=False)
    raw, reference_us = 0xFFFFFFFF, 2**48 + 123
    body = bytes([1, 1]) + struct.pack("<Q", reference_us) + bytes([4]) + struct.pack("<I", raw)
    gateway.buf.extend(b"\xa5\x5a" + body + bytes([sum(body) & 0xFF]))
    kind, parsed = gateway._extract_item()
    assert kind == "stream_frame"
    row = experiment().process(parsed, 0)
    assert row["raw_dot_timestamp_us"] == raw
    assert row["gateway_timestamp_us"] == reference_us
    assert row["reference_time_ns"] == reference_us * 1000


def test_late_observation_is_saved_but_not_fed():
    exp = experiment(no_data_seconds=10)
    exp.monitor.measurement_active = True
    exp.stability_outcome = "passed"
    exp.process(frame(0), 0)
    row = exp.process(frame(100_000), 5)
    assert row["raw_dot_timestamp_us"] == 100_000
    assert not row["model_observation_supplied"]
    assert row["observation_count"] == 1
    assert row["latest_prediction_error_ns"] is None
    assert exp.calibration_outcome == "failed"


def test_actual_stability_gate_then_calibration_then_measurement():
    exp = experiment()
    rows = []
    for i in range(430):
        now = i / 60
        exp.check_deadlines(now)
        rows.append(exp.process(frame(round(now * 1e6)), now))
    first = next(i for i, r in enumerate(rows) if r["model_observation_supplied"])
    assert all(r["stream_phase"] == "acquisition" for r in rows[:first])
    assert rows[first]["stream_phase"] == "calibration"
    assert rows[first]["calibration_elapsed_seconds"] == 0
    assert rows[first]["observation_count"] == 1
    assert exp.stability_outcome == "passed"
    assert exp.calibration_duration_seconds == pytest.approx(2, abs=1 / 60)
    assert rows[-1]["stream_phase"] == "measurement"
    assert rows[-1]["estimated_scale_ns_per_source_unit"] == pytest.approx(1000)


def test_post_observe_diagnostics_and_no_stale_error_on_alignment():
    exp = experiment()
    rows = align(exp)
    assert rows[-1]["model_state"] == "aligned"
    assert rows[-1]["mapped_timestamp_ns"] == rows[-1]["reference_time_ns"]
    assert rows[-1]["latest_prediction_error_ns"] is None
    assert rows[-1]["mean_prediction_error_ns"] is None
    for i in range(121, 421):
        raw = round(i * 1e6 / 60)
        row = exp.process(frame(raw, raw + 10_000_005), i / 60)
    assert row["model_state"] == "tracking"
    assert row["latest_prediction_error_ns"] == 5000
    assert row["mean_prediction_error_ns"] == 5000
    assert row["prediction_error_stddev_ns"] == 0
    assert row["observation_count"] == 421
    assert row["retained_calibration_observation_count"] == 121
    assert row["retained_tracking_error_count"] == 256
    assert exp.summary()["tracking_error_count_full_epoch"] == 300


def test_calibration_watchdog_starts_at_first_supplied_observation():
    exp = experiment(no_data_seconds=10)
    exp.monitor.measurement_active = True
    exp.stability_outcome = "passed"
    exp.process(frame(0), 4)
    exp.process(frame(16_667), 8.99)
    exp.check_deadlines(8.999)
    with pytest.raises(ExperimentFailure, match="Calibration deadline"):
        exp.check_deadlines(9)
    assert exp.calibration_duration_seconds == 5
    assert exp.calibration_outcome == "failed"


def test_reference_deadline_does_not_accept_late_buffered_alignment():
    exp = experiment(config=ClockAlignmentConfig(calibration_deadline_ns=5_000_000_000))
    exp.monitor.measurement_active = True
    exp.stability_outcome = "passed"
    for i in range(4):
        exp.process(frame(i * 10_000), i * .01)
    # Many missing observations, but source/reference remain continuous.
    row = exp.process(frame(6_000_000), .04)
    assert not row["model_observation_supplied"]
    assert row["latest_prediction_error_ns"] is None
    with pytest.raises(ExperimentFailure, match="reference-span"):
        exp.check_deadlines(.04)


@pytest.mark.parametrize("receive_frame", [False, True])
def test_no_data_timeout_initial_and_midstream(receive_frame):
    exp = experiment()
    if receive_frame:
        exp.process(frame(0), 1)
    with pytest.raises(ExperimentFailure, match="No-data"):
        exp.check_deadlines(4 if receive_frame else 3)


def test_stability_deadline_with_data_but_insufficient_observations():
    exp = experiment()
    for now in range(5):
        exp.process(frame(now * 1_000_000), now)
    with pytest.raises(ExperimentFailure, match="Stability gate"):
        exp.check_deadlines(5)
    assert exp.stability_outcome == "failed"


@pytest.mark.parametrize("first,second", [
    (frame(2**32 - 10), frame(10, 2**32 + 10_000_010)),  # ambiguous wrap/reset
    (frame(1_000_000), frame(100)),  # reset
    (frame(100), frame(100)),  # duplicate
    (frame(100, 2000), frame(200, 1000)),  # reference reset
    (frame(100), frame(2_000_000, 10_000_200)),  # unexplained forward jump
])
def test_discontinuity_creates_unobserved_epoch_without_feeding(first, second):
    exp = experiment()
    exp.process(first, 0)
    row = exp.process(second, .1)
    assert row["connection_epoch"] == 2
    assert row["continuity"] == "discontinuity_new_epoch"
    assert row["model_state"] == "unobserved"
    assert row["observation_count"] == 0
    assert row["unwrapped_dot_timestamp_us"] is None
    assert not row["model_observation_supplied"]
    with pytest.raises(ExperimentFailure, match="discontinuity"):
        exp.check_deadlines(.1)


class FakeClock:
    now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def mock_run(monkeypatch, tmp_path, mode="normal", *, capture_seconds=30):
    clock = FakeClock()
    gateway = GatewayClient(object(), verbose=False)
    gateway.reset_session = Mock()
    gateway.hello = Mock(return_value={"type": "hello_ack"})
    gateway.disconnect = Mock()
    movella = Mock()
    movella.connections = [SensorConnection("AA:BB", 1)]
    movella.discover.return_value = ["AA:BB"]
    movella.connect.return_value = movella.connections
    movella.start_streams.return_value = {"AA:BB": 0}
    index = 0

    def read_item(timeout_s):
        nonlocal index
        clock.sleep(1 / 60 if mode != "no-data" else .1)
        index += 1
        if mode == "no-data":
            raise TimeoutError()
        if index == 20:
            if mode == "interrupt":
                raise KeyboardInterrupt()
            if mode == "error":
                raise OSError("serial failure")
            if mode == "disconnect":
                return "json", {"type": "sensor_disconnected", "address": "AA:BB"}
        return "stream_frame", frame(round(clock.now * 1e6))

    gateway.read_item = read_item
    if mode == "configure-error":
        movella.configure.side_effect = RuntimeError("configure failed")
    if mode == "partial-connect":
        movella.connections = []
        movella.connect.side_effect = RuntimeError("connect failed")
    if mode == "start-error":
        movella.start_streams.side_effect = RuntimeError("start failed")
    if mode == "cleanup-error":
        movella.stop_streams.side_effect = RuntimeError("stop failed")
    monkeypatch.setattr(stream_client, "time", clock)
    monkeypatch.setattr(stream_client, "GatewayClient", lambda *a, **k: gateway)
    monkeypatch.setattr(stream_client, "MovellaDotClient", lambda *a, **k: movella)
    monkeypatch.setattr(stream_client, "open_gateway_serial", lambda port: nullcontext(object()))
    args = stream_client.build_parser().parse_args(["--output-dir", str(tmp_path),
                                                  "--capture-seconds", str(capture_seconds),
                                                  "--post-connect-settle-seconds", "0"])
    code = stream_client.run(args)
    output = next(tmp_path.iterdir())
    summary = json.loads((output / "summary.json").read_text())
    manifest = json.loads((output / "manifest.json").read_text())
    with (output / "observations.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    return code, summary, manifest, rows, movella, gateway


def test_saved_capture_records_and_metadata(monkeypatch, tmp_path):
    code, summary, manifest, rows, movella, _ = mock_run(monkeypatch, tmp_path)
    assert code == 0
    assert summary["status"] == "completed"
    assert summary["experiment"]["calibration_outcome"] == "aligned"
    assert summary["experiment"]["stability_gate_outcome"] == "passed"
    assert list(rows[0]) == RECORD_FIELDS
    assert rows[0]["observation_count"] == "0"
    assert rows[0]["mapped_timestamp_ns"] == ""
    assert rows[0]["mean_prediction_error_ns"] == ""
    assert rows[-1]["stream_phase"] == "measurement"
    assert int(rows[-1]["reference_time_ns"]) == int(rows[-1]["gateway_timestamp_us"]) * 1000
    assert rows[-1]["latest_prediction_error_ns"] == "0"
    assert manifest["configuration"]["clock_alignment"]["nominal_scale_ns_per_source_unit"] is None
    for repo in manifest["software"]["repositories"].values():
        assert len(repo["revision"]) == 40
        assert isinstance(repo["dirty"], bool)
    assert manifest["software"]["versions"]["pyserial"]
    assert manifest["timestamp_provenance"]["running_gateway_firmware_build_identifier"]["value"] == "unknown"
    assert manifest["timestamp_provenance"]["reference"]["tick_rate"]["device_reported"] is False
    full = summary["prediction_error_statistics"]["epochs"][0]
    assert full["tracking_error_count"] == summary["experiment"]["tracking_error_count_full_epoch"]
    assert full["tracking_error_count"] > summary["experiment"]["retained_tracking_error_count"]
    assert full["mean_prediction_error_ns"] == 0
    interval_file = next(tmp_path.iterdir()) / summary["prediction_error_statistics"]["interval_results_file"]
    intervals = json.loads(interval_file.read_text())["intervals"]
    assert sum(r["tracking_error_count"] for r in intervals) == full["tracking_error_count"]
    assert intervals[-1]["final_partial_interval"]
    movella.configure.assert_called_once_with(sampling_rate_hz=60, subscribe_timeout_s=10,
                                               write_timeout_s=10, without_response=False)
    movella.stop_streams.assert_called_once()
    movella.disconnect_all.assert_called_once()


@pytest.mark.parametrize("mode,expected", [
    ("no-data", 1), ("interrupt", 130), ("error", 1), ("disconnect", 1),
    ("configure-error", 1), ("partial-connect", 1), ("start-error", 1), ("cleanup-error", 1),
])
def test_cleanup_and_output_on_failure(monkeypatch, tmp_path, mode, expected):
    code, summary, manifest, rows, movella, gateway = mock_run(monkeypatch, tmp_path, mode)
    assert code == expected
    assert summary["status"] == manifest["status"]
    assert summary["status"] in {"failed", "cancelled"}
    if mode == "partial-connect":
        gateway.disconnect.assert_called_once()
    else:
        movella.disconnect_all.assert_called_once()
    if mode not in {"configure-error", "partial-connect"}:
        movella.stop_streams.assert_called_once()
    if mode == "cleanup-error":
        assert summary["cleanup_errors"]


def test_arg_validation_rejects_unbounded_or_impossible_durations():
    for flag, value in [("--no-data-seconds", "nan"), ("--capture-seconds", "inf"),
                        ("--calibration-deadline-seconds", "1"), ("--capture-seconds", "5"),
                        ("--inspected-gateway-tick-rate-hz", "0")]:
        args = stream_client.build_parser().parse_args([flag, value])
        with pytest.raises(ValueError):
            stream_client.validate_args(args)


def write_evidence(tmp_path, rows):
    path = tmp_path / "observations.csv"
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, RECORD_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def tracking_row(reference_ns, error, *, epoch=1, **overrides):
    return {"address": "AA:BB", "sensor_id": 1, "connection_epoch": epoch,
            "reference_time_ns": reference_ns, "stream_phase": "measurement",
            "model_state": "tracking", "model_observation_supplied": True,
            "latest_prediction_error_ns": error, **overrides}


def test_full_epoch_errors_survive_real_model_window_eviction(tmp_path):
    exp = experiment()
    rows = align(exp)
    errors = []
    for i in range(121, 721):
        raw = round(i * 1e6 / 60)
        offset_us = -60 if i < 465 else 30
        errors.append(offset_us * 1000)
        rows.append(exp.process(frame(raw, raw + 10_000_000 + offset_us), i / 60))
    report = summarize_observations(write_evidence(tmp_path, rows))
    full = report["epochs"][0]
    assert full["tracking_error_count"] == 600
    assert len(exp.model.recent_prediction_errors_ns) == 256
    assert exp.model.mean_prediction_error_ns == 30_000
    assert full["mean_prediction_error_ns"] == pytest.approx(sum(errors) / 600)
    mean = sum(errors) / 600
    assert full["prediction_error_stddev_ns"] == pytest.approx((sum((e - mean)**2 for e in errors) / 600)**.5)
    assert full["min_prediction_error_ns"] == -60_000
    assert full["max_prediction_error_ns"] == 30_000


def test_five_second_exact_boundaries_and_final_partial(tmp_path):
    origin = 10**18 + 123  # integer reference arithmetic despite large absolute times
    rows = [tracking_row(origin + offset, error) for offset, error in
            [(0, -2), (INTERVAL_NS - 1, 2), (INTERVAL_NS, 10),
             (2 * INTERVAL_NS - 1, 14), (2 * INTERVAL_NS, -5)]]
    report = summarize_observations(write_evidence(tmp_path, rows))
    intervals = report["intervals"]
    assert [r["interval_index"] for r in intervals] == [0, 1, 2]
    assert [r["tracking_error_count"] for r in intervals] == [2, 2, 1]
    assert [r["mean_prediction_error_ns"] for r in intervals] == [0, 12, -5]
    assert [r["prediction_error_stddev_ns"] for r in intervals] == [2, 2, 0]
    assert [r["actual_observation_span_ns"] for r in intervals] == [INTERVAL_NS - 1, INTERVAL_NS - 1, 0]
    assert [r["final_partial_interval"] for r in intervals] == [False, False, True]
    assert intervals[1]["interval_start_reference_time_ns"] == origin + INTERVAL_NS
    assert intervals[1]["interval_end_reference_time_ns_exclusive"] == origin + 2 * INTERVAL_NS
    assert report["epochs"][0]["actual_observation_span_ns"] == 2 * INTERVAL_NS


def test_population_statistics_empty_single_and_large_offset():
    stats = ErrorStatistics()
    empty = stats.record()
    assert empty["tracking_error_count"] == 0
    assert all(v is None for k, v in empty.items() if k != "tracking_error_count")
    stats.add(10**18 - 1, 123)
    assert stats.record()["prediction_error_stddev_ns"] == 0
    assert stats.record()["actual_observation_span_ns"] == 0
    stats.add(10**18 + 1, 456)
    assert stats.record()["prediction_error_stddev_ns"] == 1


def test_empty_intervals_and_empty_capture(tmp_path):
    report = summarize_observations(write_evidence(tmp_path, []))
    assert report["epochs"] == report["intervals"] == []
    report = summarize_observations(write_evidence(tmp_path, [tracking_row(0, 3), tracking_row(2 * INTERVAL_NS, 9)]))
    middle = report["intervals"][1]
    assert middle["tracking_error_count"] == 0
    assert middle["mean_prediction_error_ns"] is None
    assert middle["prediction_error_stddev_ns"] is None
    assert middle["actual_observation_span_ns"] is None


def test_epochs_separate_and_acquisition_calibration_unsupplied_excluded(tmp_path):
    rows = [tracking_row(0, 999, stream_phase="acquisition"),
            tracking_row(1, 999, stream_phase="calibration"),
            tracking_row(2, 999, model_observation_supplied=False),
            tracking_row(3, ""), tracking_row(4, 999, model_state="aligned"),
            tracking_row(10, -5), tracking_row(20, 5),
            tracking_row(0, 100, epoch=2),
            tracking_row(0, "", epoch=3, stream_phase="acquisition")]
    report = summarize_observations(write_evidence(tmp_path, rows))
    assert [r["tracking_error_count"] for r in report["epochs"]] == [2, 1, 0]
    assert [r["mean_prediction_error_ns"] for r in report["epochs"]] == [0, 100, None]
    assert [r["tracking_origin_reference_time_ns"] for r in report["intervals"]] == [10, 0]
    assert [r["connection_epoch"] for r in report["intervals"]] == [1, 2]


def test_provenance_supplied_inspection_is_not_runtime_verified():
    provenance = timestamp_provenance({"protocol_version": 1})
    reference = provenance["reference"]
    assert reference["tick_rate"]["value"] == 31_250
    assert not reference["tick_rate"]["device_reported"]
    assert reference["effective_tick_resolution"]["value"] == 32
    assert not reference["effective_tick_resolution"]["runtime_verified_resolution"]
    assert provenance["running_gateway_firmware_build_identifier"]["value"] == "unknown"
    assert provenance["source"]["unit"] == "microseconds"
    assert reference["capture_expression"]["value"] == "k_ticks_to_us_floor64(k_uptime_ticks())"
    supplied = timestamp_provenance(supplied_firmware_id="operator-build-123", inspected_tick_rate_hz=10_000)
    assert supplied["running_gateway_firmware_build_identifier"]["value"] == "operator-build-123"
    assert not supplied["running_gateway_firmware_build_identifier"]["device_reported"]
    assert supplied["reference"]["effective_tick_resolution"]["value"] == 100


def test_explicit_device_report_kept_separate_from_supplied_metadata():
    provenance = timestamp_provenance({"firmware_build_id": "running-456", "sys_clock_ticks_per_sec": 20_000},
                                     supplied_firmware_id="operator-build-123")
    assert provenance["running_gateway_firmware_build_identifier"]["value"] == "running-456"
    assert provenance["running_gateway_firmware_build_identifier"]["device_reported"]
    assert provenance["reference"]["tick_rate"]["device_reported"]
    assert provenance["reference"]["effective_tick_resolution"]["value"] == 50
    assert provenance["supplied_metadata"]["firmware_build_identifier"] == "operator-build-123"
    assert provenance["supplied_metadata"]["inspected_tick_rate_hz"] == 31_250


@pytest.mark.parametrize("invalid", [None, "31250", 0, -1, True])
def test_invalid_device_tick_report_does_not_verify_inspected_value(invalid):
    provenance = timestamp_provenance({"sys_clock_ticks_per_sec": invalid})
    assert not provenance["reference"]["tick_rate"]["device_reported"]
    assert provenance["reference"]["tick_rate"]["value"] == 31_250


def test_five_minute_mock_capture_and_concise_console(monkeypatch, tmp_path, capsys):
    code, summary, _, _, _, _ = mock_run(monkeypatch, tmp_path, capture_seconds=300)
    assert code == 0
    assert summary["capture_elapsed_seconds"] == pytest.approx(300, abs=1 / 60)
    stats = summary["prediction_error_statistics"]
    assert stats["epochs"][0]["tracking_error_count"] > 17_000
    assert stats["interval_record_count"] == 59
    text = capsys.readouterr().out
    assert "calibration=aligned" in text
    assert "Full-epoch prediction errors" in text
    assert "Retained-window prediction errors" in text
    assert "prediction_error_intervals.json" in text


def test_candidate_prediction_precedes_observe_and_metadata_follows_it():
    model = Mock()
    model.map_candidate.return_value = 100
    model.candidate_scale_ns_per_source_unit = None
    model.retained_tracking_observation_count = 1

    def observe(**kwargs):
        model.map_candidate.return_value = 999
        model.candidate_scale_ns_per_source_unit = 1001
        model.retained_tracking_observation_count = 2

    model.observe.side_effect = observe
    row = observe_with_candidate(model, 10, 150)
    assert row == dict(zip(CANDIDATE_FIELDS, [100, 50, 1001, 2]))
    assert [call[0] for call in model.mock_calls] == ["map_candidate", "observe"]


def test_live_candidate_missing_and_pre_update_error_preserves_accepted_fit():
    exp = experiment()
    calibration = align(exp)
    assert all(row["candidate_prediction_error_ns"] is None for row in calibration)
    assert all(row["candidate_scale_ns_per_source_unit"] is None for row in calibration)
    fixed = exp.model.map(9_000_000)
    for i in range(121, 131):
        source = round(i * 1e6 / 60)
        reference_us = source + 10_000_000 + i
        previous_candidate = exp.model.map_candidate(source)
        row = exp.process(frame(source, reference_us), i / 60)
        assert row["candidate_predicted_reference_ns"] == previous_candidate
        assert row["candidate_prediction_error_ns"] == (None if previous_candidate is None else reference_us * 1000 - previous_candidate)
        assert row["retained_tracking_observation_count"] == i - 120
        assert exp.model.map(9_000_000) == fixed
    assert row["candidate_scale_ns_per_source_unit"] is not None


def replay_fixture(tmp_path, *, samples=20, two_epochs=False, manifest=True):
    capture = tmp_path / "capture"
    capture.mkdir()
    config = asdict(ClockAlignmentConfig(target_calibration_span_ns=20_000_000,
                                        min_learned_observations=3, max_calibration_observations=100))
    config.pop("max_tracking_observations")  # pre-candidate capture compatibility
    if manifest:
        (capture / "manifest.json").write_text(json.dumps({"configuration": {"clock_alignment": config}}))
    rows = []
    for i in range(samples):
        for epoch in ([1, 2] if two_epochs else [1]):
            rows.append({"address": "A", "sensor_id": epoch, "connection_epoch": epoch,
                         "raw_dot_timestamp_us": i * 10_000,
                         "gateway_timestamp_us": 10_000_000 * epoch + i * 10_000 + (5 if i > 10 else 0),
                         "model_observation_supplied": i > 0,
                         "stream_phase": "acquisition" if i == 0 else "calibration",
                         "model_state": "saved-state-must-not-be-used",
                         "latest_prediction_error_ns": "not-an-input"})
    with (capture / "observations.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return capture / "observations.csv", config


def test_replay_excludes_acquisition_preserves_order_and_separates_epochs(tmp_path):
    source, _ = replay_fixture(tmp_path, two_epochs=True)
    result = replay(source, tmp_path / "replay", windows=(2, 3, 4))
    with (tmp_path / "replay/window_2/observations.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert [int(r["source_row"]) for r in rows] == list(range(1, 41))
    assert [r["connection_epoch"] for r in rows] == ["1", "2"] * 20
    assert rows[0]["observation_count"] == rows[1]["observation_count"] == "0"
    assert rows[2]["observation_count"] == rows[3]["observation_count"] == "1"
    assert rows[0]["candidate_prediction_error_ns"] == ""
    assert rows[-1]["observation_count"] == rows[-2]["observation_count"] == "19"
    epochs = result["configurations"]["2"]["epochs"]
    assert [e["connection_epoch"] for e in epochs] == [1, 2]
    assert all(e["full_tracking_period"]["accepted"]["tracking_error_count"] == 16 for e in epochs)
    assert all(e["common_evaluation_period"]["candidate_predictions_available"] == 12 for e in epochs)


def test_window_selection_actual_spans_and_accepted_predictions_invariant(tmp_path):
    source, config = replay_fixture(tmp_path)
    result = replay(source, tmp_path / "replay", windows=(2, 3, 4))
    accepted = []
    for n in (2, 3, 4):
        folder = tmp_path / f"replay/window_{n}"
        manifest = json.loads((folder / "manifest.json").read_text())
        selected = manifest["selected_model_configuration"]
        assert selected["max_tracking_observations"] == n
        assert selected["max_calibration_observations"] == config["max_calibration_observations"]
        assert manifest["status"] == "completed"
        with (folder / "observations.csv").open() as handle:
            rows = list(csv.DictReader(handle))
        accepted.append([r["accepted_predicted_reference_ns"] for r in rows])
        tracked = [r for r in rows if r["accepted_prediction_error_ns"] != ""]
        assert tracked[n-1]["retained_tracking_observation_count"] == str(n)
        assert tracked[n-1]["candidate_prediction_uses_full_window"] == "False"
        assert tracked[n]["candidate_prediction_uses_full_window"] == "True"
        assert int(tracked[n]["candidate_fit_reference_span_ns"]) == (n - 1) * 10_000_000
        assert int(tracked[n]["retained_tracking_reference_span_ns"]) == (n - 1) * 10_000_000
        summary = result["configurations"][str(n)]["epochs"][0]
        assert summary["full_tracking_period"]["candidate_predictions_available"] == 14
        assert summary["common_evaluation_period"]["candidate_predictions_available"] == 12
    assert accepted[0] == accepted[1] == accepted[2]


def test_replay_default_windows_and_source_not_overwritten(tmp_path):
    source, _ = replay_fixture(tmp_path)
    original = {p: p.read_bytes() for p in source.parent.iterdir()}
    result = replay(source, tmp_path / "new-destination")
    assert set(result["configurations"]) == {"300", "600", "1800"}
    assert all(e["common_evaluation_start_reference_ns"] is None for c in result["configurations"].values() for e in c["epochs"])
    assert all(p.read_bytes() == contents for p, contents in original.items())
    with pytest.raises(ValueError, match="outside"):
        replay(source, source.parent / "nested-result")
    with pytest.raises(ValueError, match="already exists"):
        replay(source, tmp_path / "new-destination")
    assert all(p.read_bytes() == contents for p, contents in original.items())


def test_replay_requires_configuration_and_records_explicit_override(tmp_path):
    source, config = replay_fixture(tmp_path, manifest=False)
    with pytest.raises(ValueError, match="configuration missing"):
        replay(source, tmp_path / "must-not-be-created")
    assert not (tmp_path / "must-not-be-created").exists()
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    replay(source, tmp_path / "override-result", windows=(2, 3, 4), config_override=config_path)
    manifest = json.loads((tmp_path / "override-result/manifest.json").read_text())
    assert manifest["source_capture"]["configuration_override"]["values"] == config
    assert manifest["source_capture"]["original_capture_manifest"] is None
    config.pop("max_calibration_observations")
    config_path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="incomplete"):
        load_configuration(source, config_path)


def test_replay_fails_explicitly_for_missing_raw_input(tmp_path):
    source, _ = replay_fixture(tmp_path)
    source.write_text("address,sensor_id\nA,1\n")
    with pytest.raises(ValueError, match="columns are missing"):
        replay(source, tmp_path / "bad-result")
    assert not (tmp_path / "bad-result").exists()


def acceptance_epoch():
    return AcceptanceEpoch(ClockAlignmentConfig(min_learned_observations=3,
                                               max_tracking_observations=2048,
                                               min_tracking_acceptance_span_ns=30_000_000_000))


def feed_acceptance_epoch(epoch, count=70):
    rows, events = [], []
    for i in range(count):
        source = i * 1_000_000
        reference = source * 1000 + max(i - 2, 0) * 1000
        row, event = epoch.process(source, reference, True)
        row["reference_time_ns"] = reference
        epoch.collect(row)
        rows.append(row)
        if event:
            events.append({"reference_time_ns": reference, **event})
    return rows, events


def test_acceptance_predictions_all_precede_both_observes(monkeypatch):
    epoch = acceptance_epoch()
    feed_acceptance_epoch(epoch, 5)
    calls = []
    for label, model in (("frozen", epoch.frozen), ("controlled", epoch.controlled)):
        for method in ("map", "observe", "map_candidate"):
            original = getattr(model, method)

            def spy(*args, _label=label, _method=method, _original=original, **kwargs):
                calls.append((_label, _method))
                return _original(*args, **kwargs)

            monkeypatch.setattr(model, method, spy)
    predicted = {"frozen": epoch.frozen.map(5_000_000), "controlled": epoch.controlled.map(5_000_000),
                 "candidate": epoch.controlled.map_candidate(5_000_000)}
    calls.clear()
    row, event = epoch.process(5_000_000, 5_000_004_000, True)
    assert calls[:5] == [("frozen", "map"), ("controlled", "map"), ("controlled", "map_candidate"),
                         ("frozen", "observe"), ("frozen", "map")]
    first_observe = calls.index(("frozen", "observe"))
    assert calls.index(("controlled", "map_candidate")) < first_observe
    for path, prediction in predicted.items():
        assert row[f"{path}_predicted_reference_ns"] == prediction
        assert row[f"{path}_prediction_error_ns"] == 5_000_004_000 - prediction
    assert event is None


def test_acceptance_cadence_no_jump_and_frozen_never_accepts(monkeypatch):
    epoch = acceptance_epoch()
    forbidden = Mock(side_effect=AssertionError("Frozen baseline must never accept"))
    monkeypatch.setattr(epoch.frozen, "accept_candidate", forbidden)
    rows, events = feed_acceptance_epoch(epoch)
    forbidden.assert_not_called()
    assert events
    assert events[0]["retained_tracking_observation_count"] == 31
    assert events[0]["retained_tracking_reference_span_ns"] >= 30_000_000_000
    assert events[0]["retained_tracking_observation_count"] < 2048  # capacity is not a required full count
    assert all(b["reference_time_ns"] - a["reference_time_ns"] >= 5_000_000_000 for a, b in zip(events, events[1:]))
    assert all(e["outcome"] == "accepted" and e["pivot_preserved_exactly"] for e in events)
    assert all(e["accepted_pivot_before_ns"] == e["accepted_pivot_after_ns"] for e in events)
    assert all(e["candidate_relative_offset_ns"] is None for e in events)
    assert events[0]["candidate_pivot_ns"] != events[0]["accepted_pivot_after_ns"]
    assert events[0]["new_accepted_scale_ns_per_source_unit"] != events[0]["old_accepted_scale_ns_per_source_unit"]
    assert epoch.frozen.estimated_scale_ns_per_source_unit == 1000
    assert all(r["controlled_mapped_increment_ns"] > 0 for r in rows if r["controlled_mapped_increment_ns"] is not None)
    assert epoch.trend.record()["mean_signed_prediction_error_ns"] > 0  # accumulated offset survives


@pytest.mark.parametrize("reject_with_exception", [False, True])
def test_unsuccessful_acceptance_uses_five_second_attempt_cadence(monkeypatch, reject_with_exception):
    epoch = acceptance_epoch()
    accept = Mock(side_effect=RuntimeError("test rejection")) if reject_with_exception else Mock(return_value=False)
    monkeypatch.setattr(epoch.controlled, "accept_candidate", accept)
    rows, events = feed_acceptance_epoch(epoch, 46)
    assert len(events) == 3
    assert all(e["outcome"] == ("error" if reject_with_exception else "rejected") for e in events)
    assert all(b["reference_time_ns"] - a["reference_time_ns"] >= 5_000_000_000 for a, b in zip(events, events[1:]))
    assert epoch.success_count == 0
    assert epoch.first_success_ns is None
    assert not any(r["common_comparison_period"] for r in rows)


def test_acceptance_reference_clock_not_sample_or_execution_cadence(monkeypatch):
    epoch = acceptance_epoch()
    # Execution is immediate and has no sleeps; an irregular reference gap leads
    # to one attempt, followed by a new deadline five seconds after that attempt.
    _, events = feed_acceptance_epoch(epoch, 34)
    assert len(events) == 1
    next_due = events[0]["next_attempt_reference_time_ns"]
    source = 34_000_000
    before = next_due - 1
    row, event = epoch.process(source, before, True)
    assert event is None
    row, event = epoch.process(source + 1, next_due, True)
    assert event is not None
    assert event["next_attempt_reference_time_ns"] == next_due + 5_000_000_000
    row, event = epoch.process(source + 2, next_due + 20_000_000_000, True)
    assert event is not None
    assert epoch.attempt_count == 3  # no catch-up attempts for missed slots


def test_bias_trend_population_and_empty_single_cases():
    trend = BiasTrend()
    assert trend.record()["error_time_slope_ns_per_reference_second"] is None
    trend.add(10, 10**18)
    assert trend.record()["mean_signed_prediction_error_ns"] == 10
    assert trend.record()["error_time_slope_ns_per_reference_second"] is None
    trend.add(12, 10**18 + 1_000_000_000)
    trend.add(14, 10**18 + 2_000_000_000)
    report = trend.record()
    assert report["error_time_slope_ns_per_reference_second"] == 2
    assert report["fitted_bias_at_period_start_ns"] == 10
    assert report["fitted_bias_at_period_end_ns"] == 14


def acceptance_capture(tmp_path):
    capture = tmp_path / "acceptance-capture"
    capture.mkdir()
    config = asdict(ClockAlignmentConfig(min_learned_observations=3))
    config.pop("min_tracking_acceptance_span_ns")  # old capture schema
    config.pop("max_tracking_observations")
    (capture / "manifest.json").write_text(json.dumps({"configuration": {"clock_alignment": config}}))
    # Three identities, including two epochs of one device, interleaved in input.
    keys = [("A", 1, 1), ("A", 1, 2), ("B", 2, 1)]
    rows = []
    for i in range(48):
        for address, sensor, epoch in keys:
            rows.append({"address": address, "sensor_id": sensor, "connection_epoch": epoch,
                         "raw_dot_timestamp_us": i * 1_000_000,
                         "gateway_timestamp_us": 10_000_000 + i * 1_000_000 + max(i-2, 0),
                         "model_observation_supplied": i > 0, "stream_phase": "ignored saved phase"})
    source = capture / "observations.csv"
    with source.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return source


def test_acceptance_replay_device_epoch_isolation_and_source_preservation(tmp_path):
    source = acceptance_capture(tmp_path)
    original = {p: p.read_bytes() for p in source.parent.iterdir()}
    previous_result = tmp_path / "previous-replay"
    previous_result.mkdir()
    marker = previous_result / "marker"
    marker.write_text("preserve this")
    output = tmp_path / "new-acceptance-result"
    result = replay_acceptance(source, output)
    assert all(p.read_bytes() == contents for p, contents in original.items())
    assert marker.read_text() == "preserve this"
    epochs = result["epochs"]
    assert [(e["address"], e["connection_epoch"]) for e in epochs] == [("A", 1), ("A", 2), ("B", 1)]
    assert len({e["first_successful_acceptance_reference_ns"] for e in epochs}) == 1
    assert all(e["successful_acceptance_count"] == 3 for e in epochs)
    assert result["successful_acceptance_count"] == 9
    for e in epochs:
        counts = [stats["tracking_error_count"] for stats in e["common_comparison_period"].values()]
        assert len(set(counts)) == 1 and counts[0] > 0
    with (output / "observations.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert [int(r["source_row"]) for r in rows] == list(range(1, 145))
    assert all(r["observation_count"] == "0" for r in rows[:3])
    assert all(r["controlled_prediction_error_ns"] == "" for r in rows[:3])
    assert all(r["frozen_predicted_reference_ns"] == r["controlled_predicted_reference_ns"]
               for r in rows if r["common_comparison_period"] == "False")
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["status"] == "completed"
    assert manifest["model_configurations"]["controlled"]["max_tracking_observations"] == 2048
    assert manifest["model_configurations"]["controlled"]["min_tracking_acceptance_span_ns"] == 30_000_000_000
    with (output / "acceptance_events.csv").open() as handle:
        events = list(csv.DictReader(handle))
    assert len(events) == 9
    assert all(e["accepted_pivot_before_ns"] == e["accepted_pivot_after_ns"] for e in events)
    with pytest.raises(ValueError, match="new output directory"):
        replay_acceptance(source, output)
    with pytest.raises(ValueError, match="new output directory"):
        replay_acceptance(source, previous_result)
    with pytest.raises(ValueError, match="new output directory"):
        replay_acceptance(source, source.parent / "nested")
    assert marker.read_text() == "preserve this"


def test_nonpositive_controlled_increment_is_recorded(monkeypatch):
    epoch = acceptance_epoch()
    feed_acceptance_epoch(epoch, 5)
    prior = epoch.previous_prediction_ns
    monkeypatch.setattr(epoch.controlled, "map", lambda source: prior)
    row, _ = epoch.process(5_000_000, 5_000_000_000, True)
    assert row["controlled_mapped_increment_ns"] == 0
    assert epoch.nonpositive_increment_count == 1


def test_acceptance_replay_logs_failed_pivot_verification(monkeypatch, tmp_path):
    from ClockMeshTest import acceptance_replay as module
    original = module.AffineClockModel.accept_candidate

    def broken_accept(model, source):
        success = original(model, source)
        if success:
            current_map = model.map
            monkeypatch.setattr(model, "map", lambda source: current_map(source) + 1)
        return success

    monkeypatch.setattr(module.AffineClockModel, "accept_candidate", broken_accept)
    source = acceptance_capture(tmp_path)
    output = tmp_path / "failed-pivot-result"
    with pytest.raises(ValueError, match="preserve mapped pivot"):
        replay_acceptance(source, output)
    assert json.loads((output / "manifest.json").read_text())["status"] == "failed"
    with (output / "acceptance_events.csv").open() as handle:
        events = list(csv.DictReader(handle))
    assert events[-1]["pivot_preserved_exactly"] == "False"
