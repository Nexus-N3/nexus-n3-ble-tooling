Product Features
================

RS Nexus BLE Tooling and the Nexus BLE Gateway currently provide a focused set of production-oriented capabilities for connected BLE sensor acquisition, validation, and operator workflows.

Current High-Level Features
---------------------------

- Multi-sensor BLE gateway control for scanning, connecting, subscribing, reading, writing, disconnecting, and status collection.
- Shared ``NexusBLESdk`` host API for serial transport, request/response handling, mixed JSON and binary parsing, and monitoring.
- Connected streaming workflows for supported sensors including Movella DOT, Nexus N3 Dot, Movesense, and MetaWear.
- High-rate binary notification forwarding for supported sensor streams, alongside human-readable JSON control commands.
- RF Survey workflows for single-target and mixed-sensor signal-quality mapping.
- Operator-facing capture workflow for supported sensor families with session manifests and output-file organization.
- Gateway and stream diagnostics for transport status, notification statistics, and stream-health visibility.
- Integration path into Nexus N3 Edge Intelligence for larger edge deployments and custom applications.

RF Survey
---------

RF Survey is a major product feature for validating deployment quality and mapping BLE signal behavior over time.

Current RF Survey capabilities include:

- one-time target discovery followed by survey execution against selected addresses
- pushed per-window ``rf_survey_status`` updates from the gateway
- per-target score, quality, trend, RSSI, and observation summaries during each survey window
- mixed-target surveys across supported sensor families
- final survey summaries with best, worst, mean, and sample-count scoring information

Supported Host Workflows
------------------------

The current tooling supports three broad host-side workflow categories:

- developer and integrator workflows through ``NexusBLESdk`` and the sensor-specific Python packages
- validation workflows through sensor sample clients and RF Survey clients
- operator workflows through the ``Capture`` client for guided session setup and recording

Design Direction
----------------

The current feature set is organized so teams can start with a working supported workflow, inspect the corresponding Python client, and then build a custom host integration on the same gateway protocol if needed.
