# Mixed DOT + Movesense ECG ClockMesh capture

From the tooling directory, using the existing environment with `clock-mesh`
installed from the local checkout:

```bash
python -m ClockMeshTest.stream_client_ecg \
  --port nexus_n3_gw \
  --capture-seconds 30 \
  --sensor-count 3
```

`--sensor-count` counts DOTs; one Movesense ECG sensor is always added. DOTs
stream at 60 Hz and ECG at 200 Hz. Add `--movesense-address AA:BB:CC:DD:EE:FF`
to select the chest sensor explicitly; otherwise discover the first matching
Movesense advertisement. All existing gateway timeouts, settling, calibration,
no-data and capture options apply. Each run creates a unique directory under
`output-files/clock-mesh-ecg` (override the parent with `--output-dir`).

One serial connection and one GatewayClient serve all sensors. Each connection
has its own ClockMesh model. The original `stream_client.py` is unchanged. The
only common alignment change is a timestamp-extraction hook whose DOT default
calls the original parser. MovesenseClient's new `ecg_only=True` constructor
option starts/stops only ECG; its default still starts ECG, HR and temperature.

## Timestamp policy and limits

The authoritative local `Movesense/profile.py:parse_ecg_sample_timestamps_ms`
explicitly defines the packet's uint32 millisecond timestamp (offset 2) as the
**first sample** time. The new client follows that definition and the existing
sample-value decoder. The vendor API/specification entry point is
https://www.movesense.com/docs/esw/api_reference/#measecg (linked ECG YAML:
https://bitbucket.org/movesense/movesense-device-lib/src/master/MovesenseCoreLib/resources/movesense-api/meas/ecg.yaml).
Verification here is against the local implementation, not an independent
measurement of the running firmware's acquisition timing.

At 200 Hz, source samples are `packet_ms * 1000 + i * 5000` microseconds.
The **last sample**, using the decoded actual count, supplies one observation
paired with that notification's gateway callback timestamp. A normal 16-sample
packet has its anchor 75 ms after its first sample and spans 80 ms until the
next packet's first sample. Every sample is mapped individually with the same
accepted per-sensor model; mapping samples adds no timing observations.
Startup mappings remain empty until provisional alignment is available. Saved
rows are never remapped after candidate acceptance.

The existing profile interprets 64 sample bytes as 16 int32 values and 32 bytes
as 16 int16 values; other whole int32 bodies use their actual count. The 32-byte
case is inherently ambiguous with eight int32 values; the local profile's
16-int16 convention is preserved. Values use the existing raw-count conversion
of 0.00038147 mV/count and the existing `/Meas/ECG/200/mv` subscription command.
Confirm this encoding/path with the sensor firmware if it differs from the
existing Movesense client. Empty or partial sample bodies are retained as
malformed evidence and never supplied to ClockMesh. GATT acknowledgements and
other stream IDs are counted as ignored notifications.

All four models must pass stability and ClockMesh candidate eligibility before
one shared official start. ECG uses the existing Movesense startup thresholds:
20 packets, at least 10 packets/s over 1.5 seconds after 0.5 seconds of grace,
with a five-second startup watchdog. DOT thresholds remain unchanged. All
models use a one-reference-second provisional fit, retained calibration history,
and at least five reference seconds for the first full-affine acceptance;
at 12.5 ECG packets/s, five seconds requires about 64 packets, rather than 301
DOT observations. Subsequent acceptance uses scale-only updates at the most
recent observation pivot and verifies exact pivot continuity. The total capture
budget starts immediately before the first start command and includes startup.

ECG gaps are checked between the previous packet's actual last sample and the
next first sample at 5 ms spacing, rather than using DOT packet-gap thresholds.
Variable packet sizes are counted. Missing samples and interior gap events are
inferred; missing packet counts at 16 samples are estimates because absent
packet sizes and losses before/after observed data are unknown. Overlapping,
duplicate, backward or off-grid source samples trigger the existing conservative
new-epoch/model reset and terminate the experiment. Gateway backward time or
source/gateway delta disagreement over one second also terminates it. No
rollover is inferred. Mapped sample monotonicity violations are reported
separately, including possible changes at full-affine startup acceptance.

Clock offsets and callback residuals include BLE scheduling and delivery latency.
They do **not** independently verify ECG measurement-time synchronisation or a
physical timing relationship between chest ECG and the three rigidly mounted
DOTs. There is no waveform-based alignment or physiological processing.

## Capture evidence

- `observations.csv`: original DOT-only schema, motion values and diagnostics.
- `ecg_packets.csv`: one row per ECG notification, including malformed ones;
  original timestamp, last-sample anchor, gateway callback, mapped anchor,
  pre-update prediction error, accepted/candidate scale, sample count, epoch,
  phase, validation error and raw payload. Only valid continuous packets add
  ClockMesh observations.
- `ecg_samples.csv`: one row per decoded sample, original source microseconds,
  mapped nanoseconds (empty before alignment or on discontinuity), ECG mV,
  packet/sample indices, epoch, phase and original gateway callback.
- `manifest.json`, `summary.json`, `mapping_updates.json`: identities/types,
  per-model results, shared boundaries for each CSV, acceptance pivots,
  timestamp policy and distinct packet/sample counts. Readiness is tracked for
  all four models; `after_startup_row_count` still refers only to DOT rows.
- `prediction_error_intervals.json`: original DOT prediction-error report.
  `ecg_prediction_error_intervals.json`: separate ECG **packet** report, using
  the same official-only, five-second interval/statistics definitions.
- `post_stop_frames.jsonl`: raw notification tail drained after stopping, outside
  the capture budget and without feeding models or changing measurement counts.

Cleanup attempts each DOT stop, the ECG stop, bounded draining and every selected
address's disconnect independently, including failure paths. Unexpected BLE
disconnections fail the experiment and are retained in the summary.

## Hardware-independent verification

```bash
PYTHONPATH=../clock-mesh/src python -m pytest -q \
  tests/test_clock_mesh_ecg.py tests/test_clock_mesh.py \
  tests/test_clock_mesh_motion.py tests/test_clock_mesh_mapping_comparison.py
```

These exercise synthetic interleaved binary gateway frames, real ClockMesh
models, packet decoding and sample spacing, shared readiness, individual sample
mapping, pivot continuity and capture/cleanup outputs. They do not constitute a
hardware capture.
