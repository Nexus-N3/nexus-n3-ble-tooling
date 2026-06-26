CLI Clients
===========

The repository includes customer-facing sample clients built on the shared SDK.

Public repository root: `Nexus-N3/rs-nexus-ble-tooling <https://github.com/Nexus-N3/rs-nexus-ble-tooling>`_.


RF Survey Clients
-----------------

``RFSurvey/client.py``, ``RFSurvey/mixed_client.py``, and ``RFSurvey/mark_client.py`` provide RF Survey workflows on top of the shared gateway SDK.

Primary behavior:

- discover and select RF Survey target addresses before the survey starts
- start RF Survey against the selected BLE target addresses
- listen for pushed ``rf_survey_status`` window updates from the gateway
- report per-target score, quality, trend, and RSSI during the survey
- stop RF Survey and print the final per-target summary returned by ``rf_survey_stop()``

During the survey phase, the gateway still uses duplicate advertisements internally so RF Survey can compute observations, RSSI statistics, scores, and trends. Those repeated advertisements are not forwarded to the host as normal ``scan_result`` discovery JSON; the host only receives RF Survey status and final summary messages once the survey is running.

``RFSurvey/client.py`` is the single-target smoke-test path.

``RFSurvey/mixed_client.py`` is the mixed-sensor path. It performs one scan, selects a combined target set across the supported sensor families, and starts RF Survey on that merged list.

``RFSurvey/mark_client.py`` is the mixed-sensor marking path. It uses the same target selection flow as ``RFSurvey/mixed_client.py``, but it keeps one operator-entered location mark active at a time. When the gateway emits ``rf_survey_mark_button``, the current mark is closed, the segment summary is printed, and the client prompts for the next mark. While no mark is active, incoming RF Survey status updates are intentionally ignored.

Primary options for ``RFSurvey/mixed_client.py``:

- ``--movella-count`` to select how many Movella DOT sensors to include
- ``--movesense-count`` to select how many Movesense sensors to include
- ``--metawear-count`` to select how many MetaWear sensors to include
- ``--nexus-n3-dot-count`` to select how many Nexus N3 Dot sensors to include
- ``--scan-timeout-ms`` to control discovery duration
- ``--window-ms`` to control the RF Survey rolling window
- ``--duration-ms`` to control the RF Survey duration

Primary options for ``RFSurvey/mark_client.py``:

- ``--movella-count`` to select how many Movella DOT sensors to include
- ``--movesense-count`` to select how many Movesense sensors to include
- ``--metawear-count`` to select how many MetaWear sensors to include
- ``--nexus-n3-dot-count`` to select how many Nexus N3 Dot sensors to include
- ``--scan-timeout-ms`` to control discovery duration
- ``--window-ms`` to control the RF Survey rolling window
- ``--duration-ms`` to control the RF Survey duration
- enter the first mark before RF Survey starts
- type ``stop`` at a mark prompt to end the survey

Example:

- ``python -m RFSurvey.mixed_client --movella-count 2 --movesense-count 1 --window-ms 3000 --duration-ms 15000``
- ``python -m RFSurvey.client --window-ms 5000 --duration-ms 20000``
- ``python -m RFSurvey.mark_client --movella-count 2 --window-ms 3000 --duration-ms 15000``


Capture Client
--------------

``Capture/cli.py`` provides the operator-facing capture session workflow across the supported sensor families.

Primary behavior:

- choose a supported sensor family
- choose the sensor count
- assign one location per connected sensor
- optionally use guided identify for Movella DOT placement
- start and stop the capture manually
- type ``quit`` at any interactive setup prompt, or press ``Ctrl+C``, to cancel and disconnect connected sensors
- write a dedicated session directory with manifest and output files under ``output-files/captures/``

Primary options:

- ``--sensor-type`` to choose ``movelladot``, ``nexusn3dot``, ``movesense``, or ``metawear``
- ``--sensor-count`` to choose how many sensors to connect
- ``--tag`` to label the capture session
- ``--location`` to predefine locations non-interactively
- ``--identify`` to enable guided identify for supported sensors
- ``--sampling-rate-hz`` to override the family default
- ``--duration-seconds`` to stop automatically instead of waiting for manual stop


Movella DOT Client
------------------

``MovellaDot/stream_client.py`` is the current tutorial path and the most direct example of a supported sensor workflow.

Public source links:

- `MovellaDot/stream_client.py <https://github.com/Nexus-N3/rs-nexus-ble-tooling/blob/main/MovellaDot/stream_client.py>`_
- `MovellaDot/client.py <https://github.com/Nexus-N3/rs-nexus-ble-tooling/blob/main/MovellaDot/client.py>`_

Primary options:

- ``--port`` to select the gateway serial device
- ``--sensor-count`` to choose how many sensors to connect
- ``--scan-timeout-ms`` to control discovery duration
- ``--sampling-rate-hz`` to select the supported Movella rate
- ``--stream-seconds`` to control capture duration
- ``--use-startup-gate`` or ``--no-startup-gate`` to control startup validation

Programmatic API:

- ``MovellaDot.MovellaDotClient.discover()``
- ``MovellaDot.MovellaDotClient.connect()``
- ``MovellaDot.MovellaDotClient.configure()``
- ``MovellaDot.MovellaDotClient.start_streams()``
- ``MovellaDot.MovellaDotClient.stop_streams()``
- ``MovellaDot.MovellaDotClient.disconnect_all()``

Nexus N3 Dot Client
-------------------

``NexusN3Dot/stream_client.py`` follows the same overall lifecycle, with Nexus N3 Dot-specific UUIDs, output-rate settings, and device-status reads.

Public source links:

- `NexusN3Dot/stream_client.py <https://github.com/Nexus-N3/rs-nexus-ble-tooling/blob/main/NexusN3Dot/stream_client.py>`_
- `NexusN3Dot/client.py <https://github.com/Nexus-N3/rs-nexus-ble-tooling/blob/main/NexusN3Dot/client.py>`_

Primary options:

- ``--port`` to select the gateway serial device
- ``--sensor-count`` to choose how many sensors to connect
- ``--scan-timeout-ms`` to control discovery duration
- ``--sampling-rate-hz`` to select a supported output data rate
- ``--stream-seconds`` to control capture duration
- ``--use-startup-gate`` or ``--no-startup-gate`` to control startup validation

Programmatic API:

- ``NexusN3Dot.NexusN3DotClient.discover()``
- ``NexusN3Dot.NexusN3DotClient.connect()``
- ``NexusN3Dot.NexusN3DotClient.configure()``
- ``NexusN3Dot.NexusN3DotClient.start_streams()``
- ``NexusN3Dot.NexusN3DotClient.stop_streams()``
- ``NexusN3Dot.NexusN3DotClient.read_device_status_all()``
- ``NexusN3Dot.NexusN3DotClient.disconnect_all()``

Movesense Client
-----------------   

``Movesense/stream_client.py`` provides a Movesense workflow with the shared gateway 
transport and Movesense-specific command UUIDs, discovery filters, and configuration sequences.  

Public source links:    
- `Movesense/stream_client.py <https://github.com/Nexus-N3/rs-nexus-ble-tooling/blob/main/Movesense/stream_client.py>`_         
- `Movesense/client.py <https://github.com/Nexus-N3/rs-nexus-ble-tooling/blob/main/Movesense/client.py>`_           

Primary options:
- ``--port`` to select the gateway serial device
- ``--sensor-count`` to choose how many sensors to connect but limited to 1 for ECG capture
- ``--scan-timeout-ms`` to control discovery duration
- ``--sampling-rate-hz`` to select a supported Movesense output data rate
- ``--stream-seconds`` to control capture duration
- ``--use-startup-gate`` or ``--no-startup-gate`` to control startup validation     

Programmatic API:
- ``Movesense.MovesenseClient.discover()``
- ``Movesense.MovesenseClient.connect()``           
- ``Movesense.MovesenseClient.configure()``
- ``Movesense.MovesenseClient.start_streams()``
- ``Movesense.MovesenseClient.stop_streams()``
- ``Movesense.MovesenseClient.read_device_status_all()``
- ``Movesense.MovesenseClient.disconnect_all()``


MetaWear Client
---------------

``MetaWear/stream_client.py`` provides the first-pass MetaWear acceleration
workflow. It uses the shared gateway transport, MetaWear-specific command UUIDs,
and gateway arrival timestamps for stream timing because live MetaWear accel
notifications do not embed a sensor-side timestamp.

Public source links:

- `MetaWear/stream_client.py <https://github.com/Nexus-N3/rs-nexus-ble-tooling/blob/main/MetaWear/stream_client.py>`_
- `MetaWear/client.py <https://github.com/Nexus-N3/rs-nexus-ble-tooling/blob/main/MetaWear/client.py>`_

Primary options:

- ``--port`` to select the gateway serial device
- ``--sensor-count`` to choose how many sensors to connect
- ``--scan-timeout-ms`` to control discovery duration
- ``--sampling-rate-hz`` for the current supported MetaWear acceleration rate
- ``--stream-seconds`` to control capture duration
- ``--use-startup-gate`` or ``--no-startup-gate`` to control startup validation
- ``--dump-raw-file`` to capture raw forwarded MetaWear frames for inspection

Programmatic API:

- ``MetaWear.MetaWearClient.discover()``
- ``MetaWear.MetaWearClient.connect()``
- ``MetaWear.MetaWearClient.configure()``
- ``MetaWear.MetaWearClient.start_streams()``
- ``MetaWear.MetaWearClient.stop_streams()``
- ``MetaWear.MetaWearClient.disconnect_all()``
