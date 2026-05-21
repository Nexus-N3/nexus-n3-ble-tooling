# Nexus BLE Tooling

This repository contains the Python tooling for the Nexus BLE gateway and supported sensor integrations.

The layout is split into two parts:

- `NexusBLESdk/`: shared serial transport, command handling, stream monitoring, startup-gate logic, and generic stream statistics
- `MovellaDot/`: the Movella DOT sample client built on top of `NexusBLESdk`

Additional sensor integrations can be added beside `MovellaDot/` using the same structure.

## Directory Layout

- `NexusBLESdk/`
  Reusable Python code for talking to the gateway over the serial link. This layer handles gateway protocol messages, mixed JSON and binary stream parsing, connection lifecycle operations, and generic monitoring utilities that can be reused across supported sensors.

- `MovellaDot/`
  The Movella DOT integration. This directory contains Movella DOT-specific constants, payload parsing, sensor operations, and a runnable `stream_client.py` example.

## Getting Started

To start with the Movella DOT sample:

- see [MovellaDot/README.md](/home/mike/Desktop/apps/dev/rs-nexus-project/rs-nexus-ble/rs-nexus-ble-tooling/MovellaDot/README.md)
- run `python MovellaDot/stream_client.py --sensor-count 1 --stream-seconds 10`

## Design Intent

The goal of this layout is to keep shared gateway behavior separate from sensor-specific workflows:

- use `NexusBLESdk` for shared gateway behavior
- keep each sensor CLI focused and sensor-specific
- keep reusable monitoring, startup-gate, and stream-health reporting generic where possible
