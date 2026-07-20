Tooling Introduction
====================

Nexus N3 BLE Tooling is organized around a clean split between a reusable gateway SDK and sensor-specific client packages.

The public code repository is `Nexus-N3/nexus-n3-ble-tooling <https://github.com/Nexus-N3/nexus-n3-ble-tooling>`_.

Repository Layout
-----------------

- ``NexusBLESdk`` contains the shared host-side transport and monitoring code.
- ``MovellaDot`` contains the Movella DOT client and sample CLI.
- ``NexusN3Dot`` contains the Nexus N3 Dot client and sample CLI.
- ``NexusN3HDRDot`` contains the Nexus N3 HDR Dot client and sample CLI.
- ``Movesense`` contains the Movesense client and sample CLI.
- ``MetaWear`` contains the MetaWear acceleration client and sample CLI.
- ``RFSurvey`` contains RF Survey clients for single-target, mixed-sensor, and marking survey workflows.

Repository paths:

- `NexusBLESdk/ <https://github.com/Nexus-N3/nexus-n3-ble-tooling/tree/main/NexusBLESdk>`_
- `MovellaDot/ <https://github.com/Nexus-N3/nexus-n3-ble-tooling/tree/main/MovellaDot>`_
- `NexusN3Dot/ <https://github.com/Nexus-N3/nexus-n3-ble-tooling/tree/main/NexusN3Dot>`_
- `NexusN3HDRDot/ <https://github.com/Nexus-N3/nexus-n3-ble-tooling/tree/main/NexusN3HDRDot>`_
- `Movesense/ <https://github.com/Nexus-N3/nexus-n3-ble-tooling/tree/main/Movesense>`_
- `MetaWear/ <https://github.com/Nexus-N3/nexus-n3-ble-tooling/tree/main/MetaWear>`_
- `RFSurvey/ <https://github.com/Nexus-N3/nexus-n3-ble-tooling/tree/main/RFSurvey>`_

Design Intent
-------------

The toolkit is structured so customers can start with a working sample application and then move down one layer into the reusable SDK when they need a custom host integration.

``NexusBLESdk`` is responsible for:

- serial transport setup
- JSON command send and receive handling
- binary stream frame parsing
- request and response correlation
- gateway status collection
- startup-gate evaluation
- stream statistics and summary reporting

The sensor-specific packages are responsible for:

- discovery filters
- GATT UUIDs and payload definitions
- sensor-specific configuration sequences
- timestamp parsing
- customer-facing CLI defaults and workflows
- RF Survey target selection and reporting workflows

Supported Customer Entry Points
-------------------------------

- ``MovellaDot/stream_client.py`` for Movella DOT streaming validation
- ``NexusN3Dot/stream_client.py`` for Nexus N3 Dot streaming validation
- ``NexusN3HDRDot/stream_client.py`` for Nexus N3 HDR Dot streaming validation
- ``NexusBLESdk`` for custom host software built on the same gateway protocol
- ``MetaWear/stream_client.py`` for MetaWear acceleration streaming validation
- ``Movesense/stream_client.py`` for Movesense streaming validation
- ``RFSurvey/client.py`` for single-target RF Survey validation
- ``RFSurvey/mixed_client.py`` for mixed-sensor RF Survey validation across supported sensor families
- ``RFSurvey/mark_client.py`` for mixed-sensor RF Survey validation with operator-driven marks
