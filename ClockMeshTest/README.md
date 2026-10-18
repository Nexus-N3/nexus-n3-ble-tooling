# ClockMesh one-DOT experiment

Runs exactly one Movella DOT at 60 Hz through a Nexus BLE gateway, using the
separately installed `clock-mesh` library with unknown source scale. The generic
model implementation is not copied or changed.

## Install and run

Install both sibling checkouts into the same virtual environment:

```bash
cd /home/mike/Desktop/apps/dev/rs-nexus-project/nexus-n3-ble-tooling
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ../clock-mesh -e '.[clock-mesh]'
nexus-n3-clock-mesh --port nexus_n3_gw
```

The gateway must have compatible firmware, and a powered DOT must be advertising.
Discovery selects the first matching DOT; keep other DOTs off when identity matters.
Use a serial path or the existing `nordic_dev` alias if appropriate. Also supported:

```bash
python -m ClockMeshTest.stream_client --port nexus_n3_gw --capture-seconds 30
```

For a five-minute experiment with exactly one DOT at the harness's fixed **60 Hz**:

```bash
nexus-n3-clock-mesh --port nexus_n3_gw --capture-seconds 300
```

The default remains 30 seconds. Both durations start immediately **before stream
start**, including startup/stability gating and calibration; they are not durations
of tracking alone.

Two-DOT mode, host/master alignment, quality classification, outlier rejection,
and automatic drift correction are not included. The live harness does not accept
candidates; the explicitly selected offline acceptance experiment is described below.

## Workflow and timing

Reset → hello → discover → connect → configure 60 Hz → start → existing stability
gate → calibrate → measure → stop → disconnect.

The default **30-second capture budget starts immediately before the stream start
command**. It includes command time, acquisition, stability gating, and calibration.
Connection/configuration settling occurs before it; shutdown takes additional time.
Host monotonic time controls watchdogs only, never model reference timestamps.

The mandatory existing `GenericStreamMonitor` gate uses its 60 Hz defaults:
five-second deadline, two-second gap grace, at least 60 packets across at least two
seconds, minimum 58 Hz, and zero gap events. It can pass before its deadline.
Frames are logged from the first frame processed in the acquisition loop, including
frames buffered by the existing client during start. The gate-passing frame is
`acquisition`; the next frame is the first `calibration` observation supplied to
ClockMesh. The model-acceptance frame remains `calibration`; subsequent frames are
`measurement`. Existing monitor counters named `measurement_*` include calibration
because they cover the whole post-gate interval.

Calibration duration starts at the **first supplied observation**, not stream start
or gate passage. The harness independently enforces the configured calibration
deadline (default five seconds), checking host elapsed time even without frames.
It also rejects calibration frames whose gateway reference span exceeds that
deadline; an exact reference-span deadline is allowed if the host watchdog has not
expired. Both host duration and reference span at alignment are saved. A separate
no-data watchdog (default three seconds) applies from stream start and after every
selected-DOT frame; unrelated messages do not reset it. Durations must be finite
and positive; capture time must exceed gate plus calibration deadline.

```bash
nexus-n3-clock-mesh --port /dev/serial/by-id/YOUR_GATEWAY \
  --capture-seconds 30 --calibration-deadline-seconds 5 --no-data-seconds 3
```

Stop and disconnect are attempted independently on completion, timeouts, exceptions,
and Ctrl+C, including partial connection/start failure. Files are closed and
summaries finalized. Exit codes: 0 completed, 1 failed (including cleanup errors),
130 Ctrl+C. Cleanup can take the configured command timeouts. Notifications cached
while stopping/disconnecting are outside the capture interval and are not model
observations.

## Timestamp provenance and continuity

The model receives unchanged raw DOT microseconds as `source_time`, and the integer
product `gateway_timestamp_us * 1000` as `reference_time_ns`. No nominal scale is
supplied; the learned scale is expected near **1,000 ns per source unit**.

Local firmware/protocol inspection established the following path in the sibling
`rs-nexus-ble-gateway` checkout:

1. `src/ble/ble_interface.c`, `notify_cb()`: supplies notification `data`, `length`,
   and `k_ticks_to_us_floor64(k_uptime_ticks())` in the same callback invocation.
2. `src/app/gateway.c`: forwards these arguments to `ble_scheduler_on_notification()`.
   `src/ble/ble_scheduler.c` copies payload into `slot->payload` and stores the
   timestamp in `slot->gateway_time_us`; `flush_notification_queue()` passes both
   from that item to `gateway_interface_send_stream_frame()`.
3. `src/interface/gateway_interface.c`: serializes the timestamp as uint64
   little-endian. Tooling `NexusBLESdk/client.py` decodes header bytes 4–11 and the
   payload from the same checksummed binary frame into `StreamFrame`.

The reference is therefore gateway uptime during **GATT callback processing for the
same notification**, after the data-null check and receive-sequence increment,
while calling the notification handler. It precedes application notification
queueing, UART pacing, USB delivery, and host parsing. It is neither a radio hardware
reception timestamp nor a sensor measurement timestamp.

The supplied inspection of the gateway build established
`CONFIG_SYS_CLOCK_TICKS_PER_SEC=31250`: a **32 µs tick resolution**, despite timestamps
being represented in microseconds. `manifest.json.timestamp_provenance` records
source/reference semantics, capture expression, tick rate, derived resolution, and
how each value was obtained. The default tick metadata is labeled supplied
inspected-build information, **not runtime-verified capability**. Another inspected
build's tick rate can be supplied with `--inspected-gateway-tick-rate-hz`.

The current inspected hello protocol reports only a protocol version, not a build
identifier or tick rate. The running firmware identifier is therefore `unknown`
unless explicitly supplied; no local checkout revision is attributed to the running
gateway. For example:

```bash
nexus-n3-clock-mesh --port nexus_n3_gw --capture-seconds 300 \
  --gateway-firmware-id YOUR_KNOWN_FLASHED_BUILD_ID
```

That identifier is labeled user-supplied. If a device explicitly includes
`firmware_build_id` and/or positive integer `sys_clock_ticks_per_sec` fields in its
hello reply, those fields are labeled device-reported and take precedence, while
the supplied metadata is preserved separately. The harness does not add a firmware
protocol feature. The raw hello response is retained. A tick-derived resolution is
still a calculation, not a runtime measurement of clock accuracy or resolution.
`host_elapsed_seconds` remains a progress/watchdog field, not a host receive timestamp.

`MovellaDot/profile.py` decodes bytes 0–3 using `<I`: unsigned 32-bit microseconds,
with about 71.6 minutes of representable range. Local code does not establish the
DOT's actual rollover/reset semantics. The gateway uses unsigned 64-bit uptime;
reboot can restart it.

Accordingly, **rollover is not inferred or unwrapped**. Within each continuous
epoch, `unwrapped_dot_timestamp_us` equals the preserved raw source timestamp. A
non-increasing DOT timestamp, backward gateway timestamp, or source/reference delta
disagreement over one second creates a fresh epoch/model and ends the experiment.
The boundary record has an unobserved model and empty unwrapped/mapped/error values;
the previous epoch summary is retained. Restart after investigating. Even backward
jumps near the 32-bit boundary use this policy, so an unverified wrap/reset cannot
enter the existing fit. The forward-jump guard is an explicit continuity policy,
not outlier rejection or a guarantee that every reset is detectable.

## Output and interpretation

The printed unique capture directory is under `output-files/clock-mesh/`:

- `observations.csv`: device address/ID, epoch, phase, continuity, raw/unwrapped DOT
  time, raw gateway time, reference/mapped nanoseconds, model-feed flag, state,
  learned scale, current prediction error, retained error mean/population standard
  deviation, total model observation count, retained calibration/error counts, and
  elapsed durations.
- `manifest.json`: configuration, units, policy, software versions, imported source
  paths, both repository revisions and dirty flags, connection, gate configuration,
  gateway hello, timestamp provenance, and prediction-error output locations.
  Revision/dirty fields are null if no source checkout is found.
  Dirty flags are captured at run start, including Git-visible untracked files.
- `summary.json`: status/failure, cleanup errors, stability/calibration outcomes and
  durations, learned scale, tracking-error counts, retained-window mean and standard
  deviation, **full-epoch prediction-error statistics** under
  `prediction_error_statistics.epochs`, and existing stream/packet-gap diagnostics.
- `prediction_error_intervals.json`: full-epoch statistics and **non-overlapping
  five-second prediction-error intervals**, with reference-time boundaries,
  observation spans, counts, mean, population standard deviation, minimum, and
  maximum. Both manifest and summary identify this file.

Diagnostics are read **after** each supplied model observation. Unavailable values
are empty CSV fields or JSON null. The alignment frame has no prediction error;
errors start on the next observation. The original per-observation CSV mean and
standard deviation, and `summary.experiment`/`summary.ended_epochs`, still cover the
model's latest retained window (default last 256 errors).

After the observation writer closes, the harness streams the saved CSV to calculate
additional summaries. **Full-epoch summaries use every tracking error**, including
errors already evicted from the model window. Acquisition, calibration, unsupplied
observations, and empty errors are excluded. Epochs are separate, including empty
full-epoch statistics for recorded epochs with no tracking errors. Each summary
provides tracking-error count, signed mean, population standard deviation, minimum,
maximum, first/last observation reference times, and actual observation span.
Empty statistics are null; one observation has zero standard deviation/span.

Intervals use integer gateway-reference nanoseconds relative to the **first tracking
observation in each epoch**. They are half-open `[start, end)`: a point at exactly
five seconds belongs to interval 1. Empty intervening intervals are included; the
final partial interval is preserved, even if it contains only one boundary point.
Nominal interval boundaries are distinct from the actual observation span. Coverage
ends at the last tracking observation, not at the nominal end of that final bin.
The concise console summary labels calibration, full-epoch prediction errors, and
retained-window prediction errors separately. Previous epoch summaries remain saved.

These are **prediction errors**: observed gateway reference minus the accepted
fixed mapping. `TRACKING` evaluates errors without updating the fit. Mapped DOT time
is on the gateway timeline; BLE delivery delay and jitter remain in observations.
Alignment state and small errors do not prove absolute measurement synchronization.
An error trend alone does not establish clock drift. No host receive timestamp
instrumentation or gateway-to-host mapping is included.

## Tracking candidates and offline replay

The local ClockMesh library must expose `map_candidate`,
`candidate_scale_ns_per_source_unit`, `retained_tracking_observation_count`, and
`ClockAlignmentConfig.max_tracking_observations`. These APIs are present in the
current inspected source; older installations of the same package version may lack
them. Install the current local checkout in editable mode.

The live CSV appends four fields without changing existing fields or phase/epoch
handling:

- `candidate_predicted_reference_ns` and `candidate_prediction_error_ns` evaluate
  `map_candidate(raw_dot_timestamp_us)` **before** supplying the current observation.
- `candidate_scale_ns_per_source_unit` and `retained_tracking_observation_count`
  describe the updated candidate **after** `observe()`.

Unavailable predictions/errors/scales are empty. A retained count of zero is a
valid reported count. Rows not supplied to the model have empty candidate fields.
The accepted mapping remains fixed; candidates are never accepted or smoothed.

Replay the locally saved five-minute capture without hardware or sleeps:

```bash
python -m ClockMeshTest.replay \
  output-files/clock-mesh/20261016T100945.507658Z_48c8b15b/observations.csv
```

After reinstalling tooling, the equivalent dedicated command is:

```bash
python -m pip install -e ../clock-mesh -e '.[clock-mesh]'
nexus-n3-clock-mesh-replay \
  output-files/clock-mesh/20261016T100945.507658Z_48c8b15b/observations.csv
```

The default creates a new unique directory under `output-files/clock-mesh-replay/`.
`--output-dir NEW_DIRECTORY` chooses an exact destination that must not exist and
must be outside the source capture directory. Source CSV/manifest files are read
only; their absolute paths and SHA-256 identities are recorded.

Replay recovers `configuration.clock_alignment` from the adjacent `manifest.json`.
It requires all original model configuration fields. Older manifests may omit the
new tracking-observation limit because each comparison explicitly overrides it.
Older manifests may also omit `min_tracking_acceptance_span_ns`: its library default
is recorded for comparison replay, which never calls acceptance. The separate
acceptance experiment below explicitly overrides that policy to 30 seconds.
`--config-json PATH` explicitly replaces the model configuration with a complete
configuration JSON object when recovery is unavailable or a deliberate change is
needed. The override path, contents, and original configuration are recorded; missing
required input/configuration fails explicitly. Calibration storage is preserved
independently of tracking-window size, and exhaustion before alignment fails rather
than silently using truncated calibration evidence.

The default comparisons use **300, 600, and 1,800 tracking observations**, roughly
5, 10, and 30 seconds at 60 Hz. These are count limits, not duration windows.
`--windows 300 600 1800` states the selection explicitly. Actual retained count and
gateway-reference span are saved, both for the pre-update candidate fit and the
post-update window. A prediction is labeled full-window only if its **pre-update**
fit already contained the configured count. The row that first fills a window is
still evaluated against a partially filled fit.

Raw DOT timestamps and raw gateway microseconds regenerate all models, in source
row order and with independent models per address/sensor ID/connection epoch.
Only `model_observation_supplied=True` rows are fed. Saved model states, predictions,
and errors are never model inputs. A supplied row with backward/non-increasing
source time or backward reference time in one epoch fails explicitly. This replay
does not repeat acquisition gates, sleeps, or host watchdogs.

Each `window_N/` output contains:

- `manifest.json`: original capture identity/manifest, recovered model configuration,
  explicit overrides, current software revisions/dirty flags, and selected limit.
- `observations.csv`: original row index/order, device/epoch, raw timestamp pairs,
  regenerated states, accepted/candidate pre-update predictions and errors, candidate
  post-update scale/count/span, pre-update candidate count/span, and evaluation flags.
  This also records candidate scale evolution per supplied observation.
- `summary.json`: full tracking-period accepted/candidate errors; accepted errors on
  exactly the candidate-available rows; separate partially filled/full-window
  candidate results; common-period results; and candidate scale evolution statistics.
- `intervals.json`: five-second gateway-reference intervals from the first tracking
  observation in each epoch, including a final partial interval, with accepted and
  candidate error statistics and descriptive post-update candidate scale statistics.

Top-level `comparison.json` collects each configuration's summaries. All error
statistics include count, mean signed error, population standard deviation, minimum,
maximum, and actual observation span. Candidate availability counts are explicit.
The **common evaluation period** includes only observations where all compared
pre-update windows are full and candidates are available, separately per epoch.
If a capture is too short, its common-period count is zero with null statistics.

These are predictions made before the current pair can influence a candidate, not
in-window fit residuals. Candidate scale evolution describes fits, not proof of
improvement, clock drift, or absolute measurement synchronization. Accepted and
candidate errors both retain BLE delivery effects. This window-comparison replay
does not accept candidates.

## Offline scale-only acceptance experiment

This is a separate, explicitly selected replay experiment. The live harness and the
window-comparison replay above still never accept candidates. The experiment uses
the library's public `accept_candidate(source_time)` API without modifying it.

After the editable installations are active, replay the saved five-minute capture:

```bash
python -m ClockMeshTest.acceptance_replay \
  output-files/clock-mesh/20261016T100945.507658Z_48c8b15b/observations.csv
```

After reinstalling tooling, `nexus-n3-clock-mesh-acceptance-replay` is an equivalent
entry point. `--output-dir NEW_DIRECTORY` selects an exact new destination; the
default creates a unique directory under `output-files/clock-mesh-acceptance-replay/`.
Existing destinations and paths inside the original capture directory are rejected.
Source captures and earlier replay outputs are never overwritten. Original model
configuration is recovered as above; `--config-json` can explicitly provide a
complete replacement configuration, which is recorded alongside the original.

Each device/sensor ID/epoch has two fresh independent models:

- **Frozen:** receives all supplied timestamp pairs, but never calls acceptance.
- **Controlled:** uses `max_tracking_observations=2048` and
  `min_tracking_acceptance_span_ns=30000000000`. The same configuration is used for
  the frozen model, isolating the effect of explicit acceptance. Original calibration
  capacity and all other recovered configuration are preserved.

The first attempt occurs after an observation when the retained candidate has at
least `min_learned_observations`, spans at least 30 gateway-reference seconds, and
has a finite positive scale. **2,048 is a capacity, not a required full count**:
the inspected library checks `min_learned_observations` and the span. At 60 Hz a
30-second span usually requires about 1,801 observations. After any attempt,
including rejection or exception, the next attempt is scheduled no sooner than
five gateway-reference seconds later. The next supplied observation at/after that
deadline triggers one attempt; gaps do not cause catch-up loops. Schedules, counts,
models, and previous mapped timestamps reset separately per device/epoch.

Processing order for every supplied observation is explicit in the manifest:

1. Call frozen `map()`, controlled `map()`, and controlled `map_candidate()` before
   either model receives the observation. Calculate all evaluation prediction errors
   from these pre-update predictions. Calibration/provisional mappings are excluded
   from tracking-period error statistics.
2. Supply the raw source/reference pair to both models.
3. If scheduled, record the updated controlled candidate and accepted pivot, then
   call `accept_candidate()` at the just-observed source timestamp.
4. Record the outcome and post-attempt state. Verify that every successful acceptance
   preserves the accepted mapped pivot exactly; a mismatch saves the event and fails
   replay. Never recompute evaluation errors from the updated fit or accepted scale.

Only rows marked `model_observation_supplied=True` enter either model. Saved model
states/errors/predictions are not inputs. Original row order and raw timestamp pairs
are preserved; there are no hardware operations, acquisition sleeps, or host-time
scheduling inputs.

The new destination contains:

- `manifest.json`: source capture/manifest identities and hashes, current software
  revisions and dirty flags, both model configurations, policy overrides, processing
  order, and output names.
- `observations.csv`: all three pre-update prediction/error paths, raw timestamps,
  states, pre/post accepted scale, updated candidate scale/count/span, attempt flags,
  successful acceptance count, and successive controlled mapped-time increments.
- `acceptance_events.csv`: device/epoch, source/reference pivot, attempt outcome,
  old/candidate/new scales, candidate count/span, accepted pivot before/after,
  candidate pivot, and the current observation's **pre-update** controlled prediction
  error. The inspected public API does not expose the candidate's relative fit offset;
  that field is empty. Candidate minus accepted time at the pivot is separately
  calculated from public mappings and is not labeled the relative fit offset.
- `summary.json`: full tracking-period and common-period error count, signed mean,
  population standard deviation, minimum, maximum, and actual observation span for
  each path; successful/failed attempt counts; mapped increment statistics and
  nonpositive increment counts; remaining controlled bias and its descriptive trend.
- `intervals.json`: non-overlapping five-second gateway-reference intervals from
  the first tracking observation per epoch, with full/common statistics for all
  three paths, empty intervening intervals, and final partial intervals preserved.

The **common period starts with the first successful acceptance's pivot observation**
in each epoch. That observation's prediction was made before acceptance, but pivot
continuity means its accepted mapped time is unchanged. With no successful acceptance,
common-period counts are zero and statistics are null. Increment diagnostics compare
successive available **pre-update controlled predictions** in the same epoch; the
individual CSV increments preserve zero or negative values rather than dropping them.

Remaining bias is the controlled common-period mean signed prediction error. Its
trend is an ordinary least-squares error-versus-gateway-reference-time slope, with
fitted bias at the period start/end and interval means available separately. This
describes the observed errors; it is not a clock update, proof of clock drift, or
a guarantee of future behavior.

**Scale-only acceptance differs from applying the full candidate mapping.** The
candidate fits scale and offset. Acceptance changes the accepted scale while keeping
the mapped pivot time, so it does **not** correct accumulated offset. A candidate's
near-zero mean arrival prediction error does not establish sensor measurement
synchronization. BLE delivery effects remain in every path's observation relationship.

## Hardware-free validation

With both editable installations active:

```bash
python -m pip install pytest
python -m pytest tests/test_clock_mesh.py tests/test_capture_workflow.py -q
python -m pytest ../clock-mesh/tests -q
```

Tests use synthetic frames and mocked serial acquisition with the real ClockMesh
library. They do not exercise BLE or validate a physical DOT.
