SDK Usage
=========

The shared SDK lives in ``NexusBLESdk``. Most custom integrations start with ``open_gateway_serial`` and ``GatewayClient``, then layer a sensor-specific client or custom workflow on top.

Public source reference: `NexusBLESdk/ <https://github.com/Nexus-N3/nexus-n3-ble-tooling/tree/main/NexusBLESdk>`_.

Basic Connection Pattern
------------------------

.. code-block:: python

   from NexusBLESdk import GatewayClient, open_gateway_serial

   with open_gateway_serial() as ser:
       gateway = GatewayClient(ser, client_name="customer_example")
       gateway.reset_session()
       gateway.hello()
       devices = gateway.scan(timeout_ms=5000, name_filter="Movella DOT")
       connections = gateway.connect([devices[0].address], timeout_s=30.0)

Core SDK APIs
-------------

``open_gateway_serial``
   Opens the serial transport to the gateway using the validated default baud rate and serial settings.

``GatewayClient``
   Provides request helpers for discovery, connection management, GATT operations, status collection, and mixed JSON/binary stream parsing.

``StartupGateConfig``
   Defines the thresholds used before stream data is considered stable enough for official measurement.

``GenericStreamMonitor``
   Tracks stream health, startup-gate state, packet counts, and rate or gap metrics across one or more active sensors.

Typical GatewayClient Methods
-----------------------------

- ``hello()`` validates protocol compatibility.
- ``reset_session()`` clears previous gateway state before a new run.
- ``scan()`` discovers nearby devices, optionally filtered by sensor name.
- ``connect()`` requests explicit BLE connections by address.
- ``subscribe()`` or ``subscribe_with_retry()`` enables notifications for a characteristic.
- ``write_gatt()`` and ``write_gatt_nowait()`` send configuration or control writes.
- ``read_gatt()`` reads a characteristic value.
- ``disconnect()`` closes one or more active links.
- ``get_status_snapshot()`` collects transport and BLE receive diagnostics.
  The BLE receive portion is gateway-transport-oriented, while sensor-specific
  continuity checks remain the responsibility of the host sensor profile.

Monitoring Pattern
------------------

The stream clients use ``GenericStreamMonitor`` with a sensor-specific timestamp
parser or frame timestamp source. This keeps transport handling generic while
allowing each sensor package to define how packet timestamps are interpreted.

In practice, this means sensors such as ``NexusN3Dot`` can use embedded payload
timestamps, while sensors such as ``MetaWear`` can fall back to gateway arrival
timestamps for rate and startup timing without pretending the payload contains a
sensor-side timestamp.

When startup gating is enabled, the monitor:

- waits for an observation window
- checks packet volume
- checks observed rate
- checks for gap events
- activates official measurement only after the stream is stable

Build Your Own Client
---------------------

Customers who need a custom host application can either:

- reuse ``GatewayClient`` directly and implement their own sensor workflow, or
- follow the repository pattern and create a new sensor package beside ``MovellaDot`` and ``NexusN3Dot``.

For implementation details, inspect the public source for `GatewayClient <https://github.com/Nexus-N3/nexus-n3-ble-tooling/blob/main/NexusBLESdk/client.py>`_, `GenericStreamMonitor <https://github.com/Nexus-N3/nexus-n3-ble-tooling/blob/main/NexusBLESdk/monitoring.py>`_, and `transport helpers <https://github.com/Nexus-N3/nexus-n3-ble-tooling/blob/main/NexusBLESdk/transport.py>`_.
