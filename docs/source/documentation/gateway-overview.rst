Gateway Overview
================

``nexus-ble-gateway`` is the embedded BLE-to-host bridge used by Nexus N3 BLE Tooling. The host toolkit does not talk to Zephyr directly; it talks to the gateway over a serial protocol that carries JSON control messages and optional binary stream frames.

Role In Nexus N3 Edge
---------------------

The BLE gateway is not a standalone customer product. It is an enabling component that integrates into **Nexus N3 Edge**, the main product of ``nexus-n3-core``. Within that product, the gateway handles BLE central responsibilities and exposes a stable host-side control and streaming interface to the Python tooling and higher-level Nexus software.

Gateway Responsibilities
------------------------

The gateway has three core jobs:

1. Discover and manage supported BLE sensor links on the embedded side.
2. Bridge control and streaming traffic over the host serial interface.
3. Surface enough diagnostics for host applications to reason about connectivity, throughput, and stream quality.

Host Protocol Summary
---------------------

The gateway currently exposes:

- newline-delimited JSON commands and status responses
- asynchronous lifecycle events such as sensor connect and disconnect notifications
- binary notification streaming for high-rate sensor data
- gateway transport statistics
- BLE notification receive statistics

The BLE receive statistics are intentionally transport-oriented. They report
what the gateway received and forwarded, such as notification counts, queue
accept/drop/flush counters, stream enqueue success or drop counters, and first
or last gateway arrival timestamps. They do not attempt to infer embedded
sensor timestamps or sensor-side packet loss from arbitrary payload bytes.

Common command families include:

- ``hello``
- ``reset_session``
- ``scan_start`` and ``scan_stop``
- ``connect_addresses`` and ``disconnect_addresses``
- ``subscribe`` and ``unsubscribe``
- ``gatt_read`` and ``gatt_write``
- ``get_status``

Why Customers Should Care
-------------------------

For customer integrations, the gateway provides a consistent control plane across supported sensors. The Python SDK in this repository exists to hide protocol details, enforce common run sequences, and make gateway diagnostics immediately usable.
