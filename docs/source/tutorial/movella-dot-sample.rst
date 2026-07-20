Movella DOT Tutorial
====================

This tutorial is the repository's main Movella DOT sample workflow and is intended to get a user from a connected gateway to a verified data stream with minimal setup.

Source references:

- `Repository root <https://github.com/Nexus-N3/nexus-n3-ble-tooling>`_
- `MovellaDot/stream_client.py <https://github.com/Nexus-N3/nexus-n3-ble-tooling/blob/main/MovellaDot/stream_client.py>`_

What You Will Use
-----------------

- A programmed Nexus BLE Gateway board connected over USB.
- At least one powered Movella DOT sensor advertising nearby.
- Python 3 with the repository dependencies installed.

From the repository root, install the runtime prerequisites:

.. code-block:: bash

   python -m pip install -r requirements.txt

Run The Sample
--------------

The main customer entry point is ``MovellaDot/stream_client.py``:

.. code-block:: bash

   python MovellaDot/stream_client.py \
     --sensor-count 1 \
     --stream-seconds 10

You can inspect the public sample implementation at `MovellaDot/stream_client.py <https://github.com/Nexus-N3/nexus-n3-ble-tooling/blob/main/MovellaDot/stream_client.py>`_.

What The Sample Does
--------------------

The Movella sample follows this sequence:

1. Open the gateway serial port.
2. Reset the gateway session and confirm protocol compatibility with ``hello``.
3. Scan for Movella DOT sensors.
4. Select the requested number of addresses.
5. Connect the selected sensors.
6. Wait briefly for BLE links and parameters to settle.
7. Subscribe to the high-rate streaming characteristic in binary mode.
8. Configure the Movella output rate.
9. Start streaming and apply the startup stability gate.
10. Collect frames, print status, and stop the stream cleanly.

Common Options
--------------

Choose a different serial device:

.. code-block:: bash

   python MovellaDot/stream_client.py \
     --port /dev/serial/by-id/your-gateway-device \
     --sensor-count 1

Test at 20 Hz instead of the default 60 Hz:

.. code-block:: bash

   python MovellaDot/stream_client.py \
     --sensor-count 1 \
     --sampling-rate-hz 20 \
     --stream-seconds 10

Keep the startup gate enabled explicitly:

.. code-block:: bash

   python MovellaDot/stream_client.py \
     --sensor-count 1 \
     --stream-seconds 10 \
     --use-startup-gate

What Success Looks Like
-----------------------

For a healthy run, expect console messages for:

- scanning and matched devices
- connection progress
- post-connect settle time
- configuration steps such as pre-stop, subscribe, and set-rate
- startup-gate evaluation
- official measurement activation
- stream stop and final summaries

When the stream is stable, the client prints:

.. code-block:: text

   Startup stability gate passed. Official measurement is now active.

Troubleshooting
---------------

- If no sensors are found, confirm the sensor is powered and advertising nearby.
- If the serial device path is wrong, pass ``--port`` explicitly.
- If configuration fails after connect, re-run the sample after power-cycling the sensor and gateway.
- If startup-gate validation fails, shorten the RF path, reduce interference, or retry with a single sensor first.

Next Steps
----------

After the sample is working:

- Continue to :doc:`../documentation/sdk-usage` for programmatic SDK usage.
- Continue to :doc:`../documentation/cli-clients` for the supported sample client surfaces.
- Continue to :doc:`../documentation/gateway-overview` for the embedded gateway role inside Nexus N3 Edge.
