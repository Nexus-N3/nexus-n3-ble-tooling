# Nexus BLE Tooling

This repository contains the Python tooling for the Nexus BLE gateway and supported sensor integrations.

The layout is split into two parts:

- `NexusBLESdk/`: shared serial transport, command handling, stream monitoring, startup-gate logic, and generic stream statistics
- `MovellaDot/`: the Movella DOT sample client built on top of `NexusBLESdk`
- `NexusN3Dot/`: the Nexus N3 Dot sample client built on top of `NexusBLESdk`

Additional sensor integrations can be added beside `MovellaDot/` using the same structure.


## Install from pip

Once this package is published to PyPI, users can install the BLE tooling with:

```bash
pip install nexus-n3-tooling
```

The install exposes a `nexus-n3` command that can run the main clients by module-style name or by the shorter aliases shown below. Arguments after the client name are passed through to that client.

Examples:

```bash
nexus-n3 MovellaDot/stream_client --sensor-count 1 --stream-seconds 10
nexus-n3 NexusN3Dot/stream_client --sensor-count 1 --stream-seconds 10
nexus-n3 Movesense/stream_client --sensor-count 1 --stream-seconds 10
nexus-n3 MetaWear/stream_client --sensor-count 1 --stream-seconds 10
nexus-n3 capture --sensor-type movelladot --sensor-count 2 --tag walk_trial
```

Dedicated console scripts are also installed:

```bash
nexus-n3-movella-dot --sensor-count 1 --stream-seconds 10
nexus-n3-nexus-n3-dot --sensor-count 1 --stream-seconds 10
nexus-n3-movesense --sensor-count 1 --stream-seconds 10
nexus-n3-metawear --sensor-count 1 --stream-seconds 10
nexus-n3-capture --sensor-type movelladot --sensor-count 2 --tag walk_trial
```

For local development, install from the repo root with:

```bash
pip install -e .
```

## Capture Client

The repository now includes an interactive capture workflow under `Capture/cli.py`.

This client is intended for operator-driven recording sessions where the user:

- chooses a supported sensor family
- chooses how many sensors to use
- assigns one location per connected sensor
- optionally uses guided identify for Movella DOT placement
- starts and stops the capture manually
- gets a dedicated session directory under `output-files/captures/`

Example:

- `python Capture/cli.py --sensor-type movelladot --sensor-count 2 --tag walk_trial`
- type `quit` at any interactive prompt, or press `Ctrl+C`, to cancel the workflow; if sensors are already connected, the client disconnects them before exiting

## Directory Layout

- `NexusBLESdk/`
  Reusable Python code for talking to the gateway over the serial link. This layer handles gateway protocol messages, mixed JSON and binary stream parsing, connection lifecycle operations, and generic monitoring utilities that can be reused across supported sensors.

- `MovellaDot/`
  The Movella DOT integration. This directory contains Movella DOT-specific constants, payload parsing, sensor operations, and a runnable `stream_client.py` example.

- `NexusN3Dot/`
  The Nexus N3 Dot integration. This directory contains Nexus N3 Dot-specific constants, payload parsing, sensor operations, and a runnable `stream_client.py` example.

- `Capture/`
  The operator-facing capture workflow. This directory contains the shared capture session CLI, sensor adapter layer, and manifest/session helpers.

## Getting Started

To start with the supported samples:

- run `nexus-n3 capture` after installing the package, or `python Capture/cli.py` from a source checkout
- see [MovellaDot/README.md](MovellaDot/README.md)
- run `nexus-n3 MovellaDot/stream_client --sensor-count 1 --stream-seconds 10` after installing the package, or `python MovellaDot/stream_client.py --sensor-count 1 --stream-seconds 10` from a source checkout
- see [NexusN3Dot/README.md](NexusN3Dot/README.md)
- run `nexus-n3 NexusN3Dot/stream_client --sensor-count 1 --stream-seconds 10` after installing the package, or `python NexusN3Dot/stream_client.py --sensor-count 1 --stream-seconds 10` from a source checkout
- run `nexus-n3 Movesense/stream_client --sensor-count 1 --stream-seconds 10` after installing the package, or `python Movesense/stream_client.py --sensor-count 1 --stream-seconds 10` from a source checkout

## Design Intent

The goal of this layout is to keep shared gateway behavior separate from sensor-specific workflows:

- use `NexusBLESdk` for shared gateway behavior
- keep each sensor CLI focused and sensor-specific
- keep reusable monitoring, startup-gate, and stream-health reporting generic where possible
