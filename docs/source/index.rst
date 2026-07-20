Nexus N3 BLE Tooling
====================

Nexus N3 BLE Tooling is the customer-facing Python host toolkit for the Nexus BLE Gateway. It provides a shared gateway SDK, supported sensor clients, and runnable sample applications for bringing BLE sensors online from a host computer.

The BLE gateway is the embedded bridge between supported sensors and the Nexus N3 host stack. In production deployments it integrates into **Nexus N3 Edge**, the primary edge product in ``nexus-n3-core``.

Public repository: `Nexus-N3/nexus-n3-ble-tooling <https://github.com/Nexus-N3/nexus-n3-ble-tooling>`_.

Two documentation entry points are provided so a website can link directly to either the hands-on tutorial or the broader product and API documentation:

.. toctree::
   :maxdepth: 2
   :caption: Entry Points

   tutorial/index
   documentation/index

Quick Highlights
----------------

- Shared ``NexusBLESdk`` API for transport, command handling, stream parsing, and monitoring.
- RF Survey workflows for single-target and mixed-sensor BLE signal mapping.
- Sensor-specific clients for Movella DOT, Nexus N3 Dot, Movesense, and MetaWear.
- Customer-facing sample CLIs for discovery, connection, configuration, streaming, and diagnostics.
- Product feature overview material for the currently implemented gateway and tooling capabilities.
- Gateway overview material that explains where the embedded BLE gateway fits inside Nexus N3 Edge.
