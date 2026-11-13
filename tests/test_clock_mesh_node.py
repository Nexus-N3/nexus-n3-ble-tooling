"""Real ClockMesh models, binary serial read boundaries, and mocked capture I/O."""
from contextlib import nullcontext
from dataclasses import replace
import csv
import json
import struct
import statistics
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from clock_mesh import ClockAlignmentConfig
from ClockMeshTest.alignment import ExperimentFailure
from ClockMeshTest.node_alignment import GatewayNodeClock, NodeExperiment, NODE_FIELDS
from ClockMeshTest import stream_client_node as client
from NexusBLESdk import GatewayClient, SensorConnection, StreamFrame


def wire(frame):
    body = bytes([1, frame.sensor_id]) + struct.pack('<Q', frame.gateway_timestamp_us)
    body += bytes([len(frame.payload)]) + frame.payload
    return b'\xa5\x5a' + body + bytes([sum(body) & 255])


def config():
    return ClockAlignmentConfig(target_calibration_span_ns=1_000_000_000,
                                min_tracking_acceptance_span_ns=5_000_000_000,
                                reuse_calibration_history=True)


def frame(sensor=1, tick=0, *, receive_ns=None, gateway_us=None, batch=None):
    gateway_us = tick * 16_667 + 10_000_000 if gateway_us is None else gateway_us
    source = tick * 16_667 + sensor * 1_000_000
    receive_ns = gateway_us * 1000 + 10**15 if receive_ns is None else receive_ns
    return StreamFrame(sensor, gateway_us, struct.pack('<I10f', source, *range(10)),
                       receive_ns, batch, 174, sensor - 1)


def test_sdk_fragment_and_read_batch_timestamp_not_decode_time(monkeypatch):
    first, second, third = [frame(i) for i in range(1, 4)]
    chunks = [wire(first) + wire(second)[:9], wire(second)[9:] + wire(third)]
    serial = SimpleNamespace(read=Mock(side_effect=chunks))
    times = Mock(side_effect=[100, 200])
    monkeypatch.setattr('NexusBLESdk.client.time.monotonic_ns', times)
    gateway = GatewayClient(serial, verbose=False)
    a = gateway.read_item()[1]
    b = gateway.read_item()[1]
    c = gateway.read_item()[1]
    assert [f.node_receive_monotonic_ns for f in (a, b, c)] == [100, 200, 200]
    assert [f.receive_read_id for f in (a, b, c)] == [1, 2, 2]
    assert [f.receive_batch_index for f in (a, b, c)] == [0, 0, 1]
    assert [f.receive_read_size for f in (a, b, c)] == [len(chunks[0]), len(chunks[1]), len(chunks[1])]
    assert [f.payload for f in (a, b, c)] == [first.payload, second.payload, third.payload]
    assert times.call_count == 2
    assert not gateway._receive_spans


def test_sdk_cached_frames_keep_receive_time_through_json_and_resync(monkeypatch):
    first, second = frame(1), frame(2)
    damaged = bytearray(wire(frame(3)))
    damaged[-1] ^= 1
    chunks = [b'noise' + bytes(damaged) + wire(first) + wire(second) + b'{"type":"ack"}\n']
    gateway = GatewayClient(SimpleNamespace(read=Mock(side_effect=chunks)), verbose=False)
    times = Mock(return_value=10**18)
    monkeypatch.setattr('NexusBLESdk.client.time.monotonic_ns', times)
    assert gateway.read_json() == {'type': 'ack'}
    assert len(gateway.cached_stream_frames) == 2
    frames = [gateway.read_item()[1], gateway.read_item()[1]]
    assert [f.node_receive_monotonic_ns for f in frames] == [10**18, 10**18]
    assert [f.receive_batch_index for f in frames] == [0, 1]
    assert gateway.stream_checksum_failures == 1
    assert gateway.stream_resync_drop_bytes > 0
    assert times.call_count == 1


def test_direct_sdk_buffer_injection_has_unknown_time_not_late_fallback():
    gateway = GatewayClient(object(), verbose=False)
    gateway.buf.extend(wire(frame()))
    parsed = gateway._extract_item()[1]
    assert parsed.node_receive_monotonic_ns is None
    assert parsed == StreamFrame(parsed.sensor_id, parsed.gateway_timestamp_us, parsed.payload)
    with pytest.raises(ExperimentFailure, match='early node reception'):
        GatewayNodeClock(config()).observe(parsed, 'startup')


def experiment(count=3):
    return NodeExperiment([SensorConnection(f'DOT{i}', i) for i in range(1, count + 1)],
                          started_at=0, config=config(), no_data_seconds=3)


def feed(exp, ticks=450, *, start=True, drift=False):
    rows = []
    for tick in range(ticks):
        now = tick / 60
        batch_receive = round(now * 1e9) + 10**15 + (max(tick - 250, 0) * 100 if drift else 0)
        for sensor in exp.dots:
            f = frame(sensor, tick, receive_ns=batch_receive, batch=tick)
            rows.append(exp.process(f, now, batch_receive + 50_000))
            if start:
                exp.try_start(now, batch_receive + 50_000)
    return rows


def test_three_sensors_share_one_gateway_node_model_and_compose_units():
    exp = experiment()
    rows = feed(exp)
    shared = exp.gateway_node
    assert shared.config.nominal_scale_ns_per_source_unit == 1.0
    assert shared.model.observation_count == 450 * 3
    assert len({id(dot.model) for dot in exp.dots.values()}) == 3
    assert all(dot.model is not shared.model for dot in exp.dots.values())
    official = [r for r in rows if r['stream_phase'] == 'measurement']
    assert official
    for r in official[-3:]:
        sid = r['sensor_id']
        assert r['reference_time_ns'] == r['gateway_timestamp_us'] * 1000
        assert r['mapped_gateway_ns'] == exp.dots[sid].model.map(r['raw_dot_timestamp_us'])
        assert r['mapped_node_ns'] == shared.model.map(r['mapped_gateway_ns'])
        assert r['session_time_ns'] == r['mapped_node_ns'] - exp.official_start_node_monotonic_ns
    assert all(r['session_time_ns'] is None for r in rows if r['stream_phase'] == 'startup')
    assert set(rows[0]) == set(NODE_FIELDS)
    stats = shared.summary()['phase_statistics']
    assert stats['measurement']['counts']['observations'] == len(official)
    assert stats['measurement']['receive_interval']['min_ns'] == 0
    assert stats['measurement']['prediction_error']['population_stddev_ns'] is not None
    assert shared.summary()['batch_frame_count_histogram'] == {3: 450}


def test_shared_model_candidate_gates_official_start_and_nominal_is_not_ready():
    exp = experiment()
    feed(exp, ticks=310, start=False)
    assert all(dot.ready_for_official_start for dot in exp.dots.values())
    assert exp.gateway_node.ready
    exp.gateway_node.model = GatewayNodeClock(config()).model
    assert not exp.gateway_node.ready
    assert not exp.try_start(6, 10**15 + 6_000_000_000)
    assert exp.official_start_node_monotonic_ns is None
    exp = experiment()
    feed(exp, ticks=30, start=True)
    assert exp.gateway_node.model.mapping_available  # nominal provisional
    assert not exp.gateway_node.ready
    assert exp.official_start_node_monotonic_ns is None


def test_full_candidate_then_scale_updates_preserve_each_model_pivot_with_drift():
    exp = experiment()
    rows = feed(exp, drift=True)
    assert exp.official_start_node_monotonic_ns is not None
    for updates in [exp.gateway_node.mapping_updates] + [d.mapping_updates for d in exp.dots.values()]:
        assert updates[0]['kind'] == 'official_start_full_affine'
        assert updates[1:]
        assert all(u['mapped_pivot_before_ns'] == u['mapped_pivot_after_ns'] for u in updates[1:])
    assert any(u['old_scale'] != u['new_scale'] for u in exp.gateway_node.mapping_updates[1:])
    assert all(r['gateway_node_prediction_error_ns'] is not None
               for r in rows if r['stream_phase'] == 'measurement')


@pytest.mark.parametrize('reverse', ['gateway', 'node'])
def test_shared_timestamp_reversal_new_epoch_is_not_observed(reverse):
    exp = experiment()
    exp.process(frame(), 0, 10**15)
    f = frame(2, 1)
    if reverse == 'gateway':
        f = replace(f, gateway_timestamp_us=1)
    else:
        f = replace(f, node_receive_monotonic_ns=1)
    row = exp.process(f, .01, 10**15 + 1)
    assert not row['gateway_node_observation_supplied']
    assert row['session_time_ns'] is None
    assert exp.gateway_node.epoch == 2
    assert exp.gateway_node.model.observation_count == 0
    assert exp.gateway_node.ended_epochs[0]['observation_count'] == 1
    assert exp.dots[2].model.observation_count == 0
    with pytest.raises(ExperimentFailure, match='reversal'):
        exp.check_deadlines(.02, 10**15 + 2)


class Clock:
    now = 0.

    def monotonic(self):
        return self.now

    def monotonic_ns(self):
        return 10**15 + round(self.now * 1e9)

    def sleep(self, seconds):
        self.now += seconds


def capture(monkeypatch, tmp_path, mode='normal'):
    clock = Clock()
    gateway = GatewayClient(object(), verbose=False)
    gateway.reset_session = Mock()
    gateway.hello = Mock(return_value={'type': 'hello_ack'})
    gateway.disconnect = Mock()
    gateway.get_connection_params = Mock(return_value={'interval_units': 6})
    movella = Mock()
    movella.connections = [SensorConnection(f'DOT{i}', i) for i in range(1, 4)]
    movella.discover.return_value = ['DOT1', 'DOT2', 'DOT3']
    movella.connect.return_value = movella.connections
    movella.start_streams.return_value = dict.fromkeys(movella.discover.return_value, 0)
    if mode == 'partial-connect':
        movella.connect.side_effect = RuntimeError('connect failed')
    if mode == 'stop-error':
        movella.stop_streams.side_effect = RuntimeError('stop failed')
    index = 0
    def read_item(timeout_s):
        nonlocal index
        if gateway.phase == 'post_stop_drain':
            clock.sleep(timeout_s)
            raise TimeoutError
        if mode == 'no-data':
            clock.sleep(timeout_s)
            raise TimeoutError
        index += 1
        sensor, tick = (index - 1) % 3 + 1, (index - 1) // 3
        clock.now = tick / 60
        if mode == 'transport-error' and tick > 30:
            raise OSError('USB lost')
        if mode == 'disconnect' and tick > 30:
            gateway.disconnected_addresses.add('DOT2')
            return 'json', {'type': 'sensor_disconnected', 'address': 'DOT2'}
        f = frame(sensor, tick, receive_ns=clock.monotonic_ns(), batch=tick)
        if mode == 'gateway-reset' and tick > 30:
            f = replace(f, gateway_timestamp_us=1)
        if mode == 'missing-metadata':
            f = replace(f, node_receive_monotonic_ns=None)
        if mode == 'unready-shared':
            # A nondecreasing but frozen early receive clock cannot satisfy the
            # library's candidate reference-span check, despite nominal mapping.
            f = replace(f, node_receive_monotonic_ns=10**15)
        return 'stream_frame', f
    gateway.read_item = read_item
    monkeypatch.setattr(client, 'time', clock)
    factory = Mock(return_value=gateway)
    monkeypatch.setattr(client, 'GatewayClient', factory)
    monkeypatch.setattr(client, 'MovellaDotClient', lambda *a, **kw: movella)
    monkeypatch.setattr(client, 'open_gateway_serial', lambda port: nullcontext(object()))
    args = client.build_parser().parse_args(['--output-dir', str(tmp_path), '--capture-seconds', '2',
                                           '--startup-timeout-seconds', '7', '--post-connect-settle-seconds', '0'])
    code = client.run(args)
    output, = tmp_path.iterdir()
    summary = json.loads((output / 'summary.json').read_text())
    manifest = json.loads((output / 'manifest.json').read_text())
    with (output / 'observations.csv').open() as handle:
        rows = list(csv.DictReader(handle))
    factory.assert_called_once()
    assert gateway.disconnect.call_count == 3
    return code, summary, manifest, rows, output, movella


def test_capture_duration_excludes_startup_and_writes_all_models(monkeypatch, tmp_path):
    code, summary, manifest, rows, output, movella = capture(monkeypatch, tmp_path)
    assert code == 0
    assert summary['status'] == manifest['status'] == 'completed'
    assert summary['official_start']['startup_elapsed_seconds'] >= 5
    assert summary['official_elapsed_seconds'] == pytest.approx(2, abs=.02)
    assert summary['row_count'] == len(rows)
    assert list(rows[0]) == NODE_FIELDS
    assert len(summary['experiments']) == 3
    assert summary['gateway_node']['observation_count'] == len(rows)
    boundary = summary['official_start']['after_startup_row_count']
    assert all(r['session_time_ns'] == '' for r in rows[:boundary])
    assert all(r['session_time_ns'] != '' for r in rows[boundary:])
    assert int(rows[-1]['node_receive_monotonic_ns']) - int(rows[boundary]['node_receive_monotonic_ns']) > 1_900_000_000
    updates = json.loads((output / 'mapping_updates.json').read_text())['updates']
    assert {u['model_id'] for u in updates} == {'dot_1_to_gateway', 'dot_2_to_gateway', 'dot_3_to_gateway', 'gateway_to_node'}
    assert summary['gateway_transport']['checksum_failures'] == 0
    residuals = [int(row['gateway_node_prediction_error_ns']) for row in rows if row['stream_phase'] == 'measurement']
    assert summary['gateway_node']['phase_statistics']['measurement']['prediction_error']['population_stddev_ns'] == pytest.approx(statistics.pstdev(residuals))
    movella.configure.assert_called_once_with(sampling_rate_hz=60, subscribe_timeout_s=10,
                                               write_timeout_s=10, without_response=False)
    assert movella.stop_streams.call_count == 3


@pytest.mark.parametrize('mode,reason', [('partial-connect', 'connect failed'), ('no-data', 'No-data'),
    ('stop-error', None), ('transport-error', 'USB lost'), ('disconnect', 'sensor_disconnected'),
    ('gateway-reset', 'reversal'), ('missing-metadata', 'early node reception'),
    ('unready-shared', 'calibration deadline')])
def test_capture_failures_are_explicit_and_cleanup_continues(monkeypatch, tmp_path, mode, reason):
    code, summary, manifest, rows, output, _ = capture(monkeypatch, tmp_path, mode)
    assert code == 1
    assert summary['status'] == manifest['status'] == 'failed'
    if reason:
        assert reason in summary['failure']
    else:
        assert summary['cleanup_errors']
    assert (output / 'events.jsonl').exists()


def test_cli_official_duration_defaults_and_startup_validation():
    args = client.build_parser().parse_args([])
    assert args.sensor_count == 3
    assert args.capture_seconds == 30
    assert str(args.output_dir) == 'output-files/clock-mesh-node'
    for bad in ['5', '0', 'nan', 'inf']:
        with pytest.raises(ValueError, match='startup_timeout'):
            client.validate_args(client.build_parser().parse_args(['--startup-timeout-seconds', bad]))


def test_rejected_shared_full_candidate_does_not_create_session_origin(monkeypatch):
    exp = experiment()
    feed(exp, ticks=310, start=False)
    monkeypatch.setattr(exp.gateway_node.model, 'accept_full_candidate', lambda pivot: False)
    with pytest.raises(ExperimentFailure, match='full candidate rejected'):
        exp.try_start(6, 10**15 + 6_000_000_000)
    assert exp.official_start_node_monotonic_ns is None
    last = exp.gateway_node.mapping_updates[-1]
    assert not last['accepted']
    assert last['processing_monotonic_ns'] == 10**15 + 6_000_000_000


def test_phase_specific_dot_gaps_and_monotonicity_diagnostics():
    exp = experiment(1)
    rows = feed(exp, ticks=310)
    f = frame(1, 312, batch=312)
    exp.process(f, 312 / 60, f.node_receive_monotonic_ns)
    counts = exp.summary()['dot_counts_by_phase']
    assert counts['startup'][1]['inferred_missing_samples'] == 0
    assert counts['measurement'][1]['inferred_missing_samples'] == 2
    # Inject a mapping reversal to exercise diagnostic reporting without changing
    # any timestamps supplied to ClockMesh.
    exp.gateway_node.model.map = lambda timestamp: -1
    f = frame(1, 313, batch=313)
    row = exp.process(f, 313 / 60, f.node_receive_monotonic_ns)
    assert row['composed_monotonicity_violation']
    assert exp.summary()['composed_monotonicity_violations']['measurement'][1] > 0
