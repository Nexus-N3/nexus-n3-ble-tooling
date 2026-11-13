"""Mixed gateway frames, real ClockMesh fits, and serial-free capture lifecycle."""
from contextlib import nullcontext
import csv
import json
import struct
from unittest.mock import Mock

import pytest
from clock_mesh import ClockAlignmentConfig
from ClockMeshTest.alignment import DotExperiment, ExperimentFailure, RECORD_FIELDS, try_start_official
from ClockMeshTest.ecg_alignment import EcgExperiment, PACKET_FIELDS, SAMPLE_FIELDS, decode_ecg
from ClockMeshTest import stream_client_ecg as client
from Movesense.client import MovesenseClient
from Movesense.profile import MOVESENSE_WRITE_UUID, build_subscribe_command, build_stop_command
from NexusBLESdk import DiscoveredDevice, GatewayClient, SensorConnection, StreamFrame


def ecg_frame(first_ms=9_000_000, count=16, *, narrow=False, gateway_us=None):
    payload = bytes([2, 100]) + struct.pack('<I', first_ms)
    payload += struct.pack('<' + ('h' if narrow else 'i') * count, *range(count))
    anchor = first_ms * 1000 + (count - 1) * 5000
    return StreamFrame(4, anchor + 1_000_000 if gateway_us is None else gateway_us, payload)


def ecg_experiment():
    return EcgExperiment(SensorConnection('ECG', 4), started_at=0,
                         calibration_config=ClockAlignmentConfig(
                             target_calibration_span_ns=1_000_000_000,
                             min_tracking_acceptance_span_ns=5_000_000_000,
                             reuse_calibration_history=True), no_data_seconds=3)


@pytest.mark.parametrize('narrow', [False, True])
def test_packet_one_observation_sixteen_samples_and_large_source_time(narrow):
    exp = ecg_experiment()
    frame = ecg_frame(narrow=narrow)
    row, samples = exp.process(frame, 0)
    assert row['packet_timestamp_ms'] == 9_000_000
    assert row['source_anchor_us'] == 9_000_075_000  # exceeds DOT uint32 range
    assert row['gateway_timestamp_us'] == frame.gateway_timestamp_us
    assert exp.model.observation_count == 1
    assert len(samples) == 16
    assert list(samples[0]) == SAMPLE_FIELDS
    assert set(row) == set(PACKET_FIELDS)
    assert [s['source_sample_timestamp_us'] for s in samples] == list(range(9_000_000_000, 9_000_080_000, 5000))
    assert samples[-1]['ecg_mv'] == pytest.approx(15 * 0.00038147)
    assert all(s['mapped_timestamp_ns'] is None for s in samples)


@pytest.mark.parametrize('payload', [b'', b'\x02\x64', b'\x02\x64' + b'\0' * 4,
                                      ecg_frame().payload + b'x'])
def test_malformed_evidence_no_model_update(payload):
    exp = ecg_experiment()
    row, samples = exp.process(StreamFrame(4, 123, payload), 0)
    assert row['validation_error']
    assert row['payload_hex'] == payload.hex()
    assert not row['model_observation_supplied']
    assert exp.model.observation_count == 0
    assert exp.summary()['malformed_packets'] == 1
    assert not samples
    with pytest.raises(ExperimentFailure, match='No-data'):
        exp.check_deadlines(3)


def test_ack_and_other_streams_not_observations():
    exp = ecg_experiment()
    for payload in [bytes([1, 100]), bytes([2, 1]) + b'\0' * 8]:
        assert exp.process(StreamFrame(4, 123, payload), 0) == (None, [])
    assert exp.model.observation_count == 0


def test_variable_packet_counts_gap_and_overlap_reset():
    exp = ecg_experiment()
    exp.process(ecg_frame(1000, 4), 0)
    row, samples = exp.process(ecg_frame(1020, 3), .02)
    assert len(samples) == 3
    assert row['source_anchor_us'] == 1_030_000
    assert row['sample_count_changed']
    assert not row['missing_samples_before_packet']
    row, _ = exp.process(ecg_frame(1050, 4), .05)
    assert row['missing_samples_before_packet'] == 3  # 1035,1040,1045
    assert exp.summary()['missing_packet_gap_events'] == 1
    old_model = exp.model
    row, samples = exp.process(ecg_frame(1060, 4), .06)  # overlaps previous end at 1065
    assert exp.epoch == 2
    assert exp.model is not old_model
    assert not row['model_observation_supplied']
    assert all(sample['mapped_timestamp_ns'] is None for sample in samples)
    assert exp.summary()['model_reset_count'] == 1
    with pytest.raises(ExperimentFailure, match='discontinuity'):
        exp.check_deadlines(.06)


def mixed_events(seconds=8):
    events = []
    for sensor_id in range(1, 4):
        for i in range(round(seconds * 60)):
            now = i / 60
            source = round(now * 1e6) + sensor_id * 1_000_000
            payload = struct.pack('<I10f', source, *range(10))
            events.append((now, StreamFrame(sensor_id, round(now * 1e6) + 10_000_000, payload)))
    for i in range(int(seconds / .08)):
        now = i * .08 + .08
        frame = ecg_frame(9_000_000 + i * 80, gateway_us=round(now * 1e6) + 10_000_000)
        events.append((now, frame))
    return sorted(events, key=lambda entry: (entry[0], entry[1].sensor_id))


def test_interleaved_gateway_frames_shared_start_sample_maps_and_pivot_continuity():
    ecg = ecg_experiment()
    experiments = {i: DotExperiment(SensorConnection(f'DOT{i}', i), started_at=0,
                                   calibration_config=ecg.config, no_data_seconds=3)
                   for i in range(1, 4)}
    experiments[4] = ecg
    assert len({id(exp.model) for exp in experiments.values()}) == 4
    gateway = GatewayClient(object(), verbose=False)
    ecg_rows = []
    sample_rows = []
    boundary = None
    for now, frame in mixed_events():
        # Round trip actual interleaved binary stream frames through SDK decoding.
        body = bytes([1, frame.sensor_id]) + struct.pack('<Q', frame.gateway_timestamp_us)
        body += bytes([len(frame.payload)]) + frame.payload
        gateway.buf.extend(b'\xa5\x5a' + body + bytes([sum(body) & 255]))
        kind, decoded = gateway._extract_item()
        assert kind == 'stream_frame'
        exp = experiments[decoded.sensor_id]
        before_count = exp.model.observation_count
        if exp is ecg:
            row, samples = exp.process(decoded, now)
            ecg_rows.append(row)
            sample_rows.extend(samples)
            assert exp.model.observation_count == before_count + 1
            assert len(samples) == 16
            for sample in samples:
                assert sample['mapped_timestamp_ns'] == exp.model.map(sample['source_sample_timestamp_us'])
        else:
            row = exp.process(decoded, now)
            assert exp.model.observation_count == before_count + 1
        if now <= 5:
            assert ecg.official_started_at is None
        if try_start_official(list(experiments.values()), now):
            assert boundary is None
            boundary = len(ecg_rows)
            assert all(exp.ready_for_official_start for exp in experiments.values())
    assert boundary is not None
    assert len({exp.official_started_at for exp in experiments.values()}) == 1
    assert ecg.official_started_at >= 5.08  # five full reference seconds at packet cadence
    assert all(row['stream_phase'] == 'startup' for row in ecg_rows[:boundary])
    assert all(row['stream_phase'] == 'measurement' for row in ecg_rows[boundary:])
    assert ecg.model.observation_count == len(ecg_rows)
    assert ecg.sample_count == 16 * len(ecg_rows)
    assert ecg.summary()['mapped_sample_monotonicity_errors'] == 0
    for exp in experiments.values():
        assert exp.mapping_updates[0]['kind'] == 'official_start_full_affine'
        updates = exp.mapping_updates[1:]
        assert updates
        assert all(update['mapped_pivot_before_ns'] == update['mapped_pivot_after_ns'] for update in updates)
    mapped = [s for s in sample_rows if s['stream_phase'] == 'measurement']
    assert all(b['mapped_timestamp_ns'] - a['mapped_timestamp_ns'] == 5_000_000
               for a, b in zip(mapped, mapped[1:]))


def test_ecg_only_is_opt_in_and_preserves_default_commands(monkeypatch):
    monkeypatch.setattr('Movesense.client.time', Mock(sleep=Mock()))
    for ecg_only in (False, True):
        gateway = Mock()
        movesense = MovesenseClient(gateway, ecg_only=ecg_only)
        movesense.connections = [SensorConnection('ECG', 4)]
        movesense.start_streams(write_timeout_s=1, without_response=False)
        commands = [call.args[2] for call in gateway.write_gatt.call_args_list]
        assert commands[0] == build_subscribe_command(100, '/Meas/ECG/200/mv')
        assert len(commands) == (1 if ecg_only else 3)
        gateway.write_gatt.reset_mock()
        movesense.stop_streams(write_timeout_s=1, without_response=False)
        assert [call.args[2] for call in gateway.write_gatt.call_args_list] == [
            build_stop_command(i) for i in ((100,) if ecg_only else (100, 1, 2))]


class Clock:
    now = 0.

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.mark.parametrize('mode', ['normal', 'explicit', 'disconnect', 'stop-error', 'partial-connect', 'short-budget', 'silent-ecg'])
def test_capture_outputs_and_cleanup(monkeypatch, tmp_path, mode):
    clock = Clock()
    gateway = GatewayClient(object(), verbose=False)
    gateway.reset_session = Mock()
    gateway.hello = Mock(return_value={'type': 'hello_ack'})
    gateway.scan = Mock(side_effect=lambda timeout, **kwargs: (
        [DiscoveredDevice('ECG', 'Movesense test')] if 'name_prefix_filter' in kwargs else
        [DiscoveredDevice(f'DOT{i}', 'Movella DOT') for i in range(1, 4)]))
    connections = [SensorConnection(f'DOT{i}', i) for i in range(1, 4)] + [SensorConnection('ECG', 4)]
    gateway.connect = Mock(return_value=connections)
    if mode == 'partial-connect':
        gateway.connect.side_effect = RuntimeError('partial connect')
    gateway.subscribe_with_retry = Mock()
    origin = None
    def write_gatt(address, uuid, command, **kwargs):
        nonlocal origin
        if gateway.phase == 'start_streams' and origin is None:
            origin = clock.now
        if mode == 'stop-error' and gateway.phase == 'stop_streams' and address == 'DOT1':
            raise RuntimeError('stop failed')
        return clock.now
    gateway.write_gatt = Mock(side_effect=write_gatt)
    gateway.disconnect = Mock()
    events = iter(mixed_events(9))
    def read_item(timeout_s):
        while True:
            if gateway.phase == 'post_stop_drain':
                clock.sleep(timeout_s)
                raise TimeoutError
            now, frame = next(events)
            clock.now = max(clock.now, origin + now)
            if mode == 'silent-ecg' and frame.sensor_id == 4:
                continue
            if mode == 'disconnect' and now > 1:
                msg = {'type': 'sensor_disconnected', 'address': 'ECG', 'reason': 19}
                gateway._observe_json(msg)
                return 'json', msg
            return 'stream_frame', frame
    gateway.read_item = read_item
    monkeypatch.setattr(client, 'time', clock)
    monkeypatch.setattr('MovellaDot.client.time', clock)
    monkeypatch.setattr('Movesense.client.time', clock)
    factory = Mock(return_value=gateway)
    monkeypatch.setattr(client, 'GatewayClient', factory)
    monkeypatch.setattr(client, 'open_gateway_serial', lambda port: nullcontext(object()))
    options = ['--output-dir', str(tmp_path), '--capture-seconds', '4' if mode == 'short-budget' else '8',
               '--post-connect-settle-seconds', '0']
    if mode == 'explicit':
        options += ['--movesense-address', 'ecg']
    assert client.run(client.build_parser().parse_args(options)) == (0 if mode in ('normal', 'explicit') else 1)
    output, = tmp_path.iterdir()
    summary = json.loads((output / 'summary.json').read_text())
    manifest = json.loads((output / 'manifest.json').read_text())
    assert summary['status'] == manifest['status']
    factory.assert_called_once()
    gateway.connect.assert_called_once_with(['DOT1', 'DOT2', 'DOT3', 'ECG'], timeout_s=30)
    assert [call.args[0] for call in gateway.disconnect.call_args_list] == [['DOT1'], ['DOT2'], ['DOT3'], ['ECG']]
    with (output / 'observations.csv').open() as handle:
        dots = list(csv.DictReader(handle))
    with (output / 'ecg_packets.csv').open() as handle:
        packets = list(csv.DictReader(handle))
    with (output / 'ecg_samples.csv').open() as handle:
        samples = list(csv.DictReader(handle))
    assert manifest['ecg_timestamp_policy']['packet_timestamp'].endswith('first sample')
    assert len(samples) == 16 * len(packets)
    assert summary['row_counts'] == {'dot_observations': len(dots), 'ecg_packet_rows': len(packets), 'ecg_sample_rows': len(samples)}
    if mode in ('normal', 'explicit'):
        assert list(dots[0]) == RECORD_FIELDS
        assert list(packets[0]) == PACKET_FIELDS
        assert list(samples[0]) == SAMPLE_FIELDS
        assert len(summary['experiments']) == 4
        assert len(manifest['sensor_types_by_id']) == 4
        boundary = summary['official_start']
        assert all(row['stream_phase'] == 'startup' for row in packets[:boundary['after_startup_ecg_packet_row_count']])
        assert all(row['stream_phase'] == 'measurement' for row in packets[boundary['after_startup_ecg_packet_row_count']:])
        updates = json.loads((output / 'mapping_updates.json').read_text())['updates']
        assert {u['sensor_id'] for u in updates} == {1, 2, 3, 4}
        assert {u['sensor_type'] for u in updates} == {'movella_dot', 'movesense_ecg'}
        ecg_stats, = summary['ecg_prediction_error_statistics']['epochs']
        assert ecg_stats['tracking_error_count'] == sum(row['stream_phase'] == 'measurement' for row in packets)
        assert (output / 'ecg_prediction_error_intervals.json').exists()
        assert (output / 'post_stop_frames.jsonl').exists()
        assert summary['capture_elapsed_seconds'] <= 8.02
    elif mode == 'short-budget':
        assert summary['official_start'] is None
        assert 'budget exhausted' in summary['failure']
    elif mode == 'silent-ecg':
        assert 'No-data' in summary['failure']
    elif mode == 'disconnect':
        assert summary['ble_disconnections'] == ['ECG']
    if mode != 'partial-connect':
        ecg_commands = [call.args[2] for call in gateway.write_gatt.call_args_list if call.args[1] == MOVESENSE_WRITE_UUID]
        assert ecg_commands == [build_subscribe_command(100, '/Meas/ECG/200/mv'), build_stop_command(100)]


def test_ecg_changing_scale_keeps_pivot_and_errors_are_pre_update():
    exp = ecg_experiment()
    for i in range(100):
        frame = ecg_frame(9_000_000 + i * 80)
        frame = StreamFrame(4, frame.gateway_timestamp_us + max(i - 20, 0) * 3, frame.payload)
        source_anchor = decode_ecg(frame.payload)[0][-1]
        before = exp.model.map(source_anchor)
        row, samples = exp.process(frame, i * .08)
        if before is not None:
            assert row['latest_prediction_error_ns'] == frame.gateway_timestamp_us * 1000 - before
        assert samples[-1]['mapped_timestamp_ns'] == row['mapped_timestamp_ns']
        try_start_official([exp], i * .08)
    updates = exp.mapping_updates[1:]
    assert updates
    assert any(u['old_scale_ns_per_source_unit'] != u['new_scale_ns_per_source_unit'] for u in updates)
    assert all(u['mapped_pivot_before_ns'] == u['mapped_pivot_after_ns'] for u in updates)
