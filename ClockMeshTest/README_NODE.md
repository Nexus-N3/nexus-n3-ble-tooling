# DOT → gateway → node monotonic capture

Run from `nexus-n3-ble-tooling`, with the local ClockMesh checkout installed in
the active Python environment:

```bash
python ClockMeshTest/stream_client_node.py \
  --port nexus_n3_gw --sensor-count 3 --capture-seconds 30
```

`python -m ClockMeshTest.stream_client_node` also works. The defaults are three
Movella DOTs at 60 Hz. `--capture-seconds` means **official measurement duration**,
excluding startup. Startup is bounded by `--startup-timeout-seconds` (default 15,
strictly greater than five). The existing gateway, settling, no-data and
calibration timeout options remain available. Each capture uses a unique
`output-files/clock-mesh-node/<capture-id>/` directory; `--output-dir` changes its
parent. No ECG stream is configured.

## Reception boundary

`NexusBLESdk.GatewayClient` calls `time.monotonic_ns()` immediately after each
`serial.read(256)` returns. Buffered byte spans retain that read's timestamp, ID
and byte count. A decoded frame inherits the span containing its final checksum
byte: the read that made the complete frame available. Parsing, JSON processing,
resynchronisation, command-time caching, and delayed `read_item()` calls preserve
that provenance. A fragmented frame gets the timestamp of its completing read.
Frames completed in the same read share the same timestamp and read ID, with a
zero-based completion index. No artificial intra-batch spacing is added.

The new StreamFrame metadata fields are optional, keeping existing three-argument
construction valid. Frames injected directly into the parser buffer have unknown
reception metadata; the node experiment rejects missing reception timestamps
rather than substituting application processing time. Metadata does not affect
frame equality, preserving existing payload/frame comparisons.

This is a **read-completion timestamp**, not a physical USB/serial byte arrival
time. OS/USB buffering and the serial read's size/timeout affect this boundary.
The firmware and binary protocol are unchanged.

## Mapping lifecycle

Each DOT reuses the existing DotExperiment, configuration, motion parser,
stability gate and continuity policy:

```python
sensor_model.observe(raw_dot_timestamp_us, gateway_timestamp_us * 1000)
```

Exactly one shared AffineClockModel observes all selected DOT notifications:

```python
gateway_node_model.observe(gateway_timestamp_us * 1000, node_receive_monotonic_ns)
```

Its source units are nanoseconds with nominal scale 1.0. Equal gateway ticks and
equal node receive times are allowed, preserving callback resolution and read
batching; backward timestamps record a new shared epoch/model and end the run.
No timestamp is unwrapped or reordered.

Both stages use the existing ClockMesh estimator: one reference second for
initial calibration, retained calibration history, and five reference seconds
of candidate history. The shared nominal mapping can exist before its learned
fit; that alone is **not readiness**. Official measurement requires the learned,
eligible shared model and every DOT's stability and candidate eligibility.
Full-affine candidates are accepted at one shared boundary. Subsequent scale-only
acceptance uses each model's latest observed source pivot and checks exact mapped
pivot continuity. No transport scheduling correction or new fit is introduced.

For each sample, the accepted models compose as:

```python
mapped_gateway_ns = sensor_model.map(raw_dot_timestamp_us)
mapped_node_ns = gateway_node_model.map(mapped_gateway_ns)
session_time_ns = mapped_node_ns - official_start_node_monotonic_ns
```

Startup rows have no session time. One node monotonic origin is recorded at the
application's official phase boundary after readiness; subsequent rows belong
to official measurement. Previously buffered frames retain their earlier
receive times. Session times can therefore be negative near the boundary; they
are not clamped or retroactively aligned. Full-affine acceptance may change
mapping offset at the startup boundary. Scale-only pivot continuity is per model;
a composed query away from the shared model's observation pivot can still move
slightly when its scale changes. Per-DOT and acquisition-order monotonicity
checks expose changes rather than rewriting saved rows.

Clock offset and gateway-to-node prediction error can include one-way transport
latency. Neither stage independently verifies true physical synchronisation.
`node_receive_monotonic_ns` is not UTC or another computer's clock.

## Outputs and diagnostics

- `observations.csv` retains the original DOT timing/motion columns and adds
  early node reception/read provenance, processing time, both mapped timestamps,
  session time, both models' pre-update prediction residuals, state/acceptance,
  and host frame/per-sensor sequence indices. These indices are not device
  sequence counters. `mapped_timestamp_ns` remains the original DOT-to-gateway
  result and is also explicitly named `mapped_gateway_ns`.
- `mapping_updates.json` identifies every DOT model and the one shared model,
  with source pivots in their actual units, acceptance decisions and before/after
  pivot mapping/scale. DOT successful acceptance events reuse the original
  history; rejected continuous decisions are visible in CSV `candidate_accepted`.
  Shared acceptance attempts, including rejection, are retained in the history.
- `manifest.json` records configuration, domains, timestamp boundary, topology,
  phase/continuity policies, software provenance and the official start origin.
- `summary.json` includes per-DOT observation/gap/reset diagnostics, per-phase
  received/model/missing counts, shared affine scale, separate startup/measurement
  prediction-error means and population standard deviations, callback/reception
  interval moments and fixed-bin histograms, equal-timestamp/read-batch counts,
  batch-size histograms, composed monotonicity, mapping availability, epochs,
  BLE disconnections and transport checksum/resynchronisation/drop statistics.
  Interval and gap diagnostics crossing a phase boundary belong to the later
  observation's phase. Batch-size histograms by phase count the selected portion
  of a read in that phase; a read can span the official boundary. Original DOT
  monitor counters retain their documented post-stability scope; the added
  phase counters use the shared official boundary.
- `events.jsonl` preserves gateway messages, failures, malformed/rejected raw
  frames, the official start event and raw post-stop tails. Frames read after
  the official deadline are recorded separately, without observing models.
  Stop, bounded drain and each address's disconnect are attempted independently.

## Hardware-independent checks

```bash
PYTHONPATH=../clock-mesh/src python -m pytest -q tests
```

Tests exercise fragmented/coalesced serial reads, checksum resynchronisation,
JSON/cached frames, real ClockMesh composition and units, three DOTs sharing one
transport model, candidate gating, drift/pivot continuity, batched receive times,
reversals/epochs, official duration and failure cleanup. Existing clients and
ClockMesh itself are unchanged. These tests do not constitute a hardware capture.
