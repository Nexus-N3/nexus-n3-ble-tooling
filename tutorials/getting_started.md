# Movella DOT Single-Sensor Tutorial

This tutorial walks through setting up Nexus N3 BLE Tooling on a desktop computer and running a short test with one Movella DOT sensor.

By the end of the tutorial, you will have:

* downloaded the repository or installed the package
* created a Python virtual environment
* installed the tooling
* identified the Nexus N3 BLE Gateway serial port
* connected one Movella DOT sensor
* verified that sensor data is streaming

## Requirements

You will need:

* a desktop computer running Linux, Windows, or macOS
* Python 3.10 or newer
* a programmed Nexus N3 BLE Gateway connected over USB
* one powered Movella DOT sensor

Make sure the Movella DOT is awake and not already connected to another computer, phone, or tablet.

## Get the Tooling

Choose one of these approaches:

### Option 1: Download the source repository

You can either clone the repository with Git or download the ZIP archive from GitHub.

Clone with Git:

.. code-block:: bash

git clone https://github.com/Nexus-N3/nexus-n3-ble-tooling.git
cd nexus-n3-ble-tooling

Or download the ZIP from GitHub, extract it, and change into the extracted directory.

### Option 2: Install from pip

If you do not need a source checkout, install the package directly:

.. code-block:: bash

The current test release:

.. code-block:: bash

pip install -i https://test.pypi.org/simple/ nexus-n3-ble-tooling

The README already documents both install paths.

## Create a Python Environment

This step is mainly for source-checkout use. If you installed directly from pip
into an existing environment, you can skip to gateway setup.

Check that Python 3.10 or newer is available:

.. code-block:: bash

python --version

Create and activate a virtual environment.

Linux or macOS:

.. code-block:: bash

python3 -m venv .venv
source .venv/bin/activate

Windows PowerShell:

.. code-block:: powershell

py -3 -m venv .venv
.venv\Scripts\Activate.ps1

Windows Command Prompt:

.. code-block:: bat

py -3 -m venv .venv
.venv\Scripts\activate.bat

Upgrade `pip` and install the repository:

.. code-block:: bash

python -m pip install --upgrade pip
python -m pip install -e .

The `-e` option installs the package in editable mode. Changes made inside the cloned repository are therefore available without reinstalling the package.

## Connect the Gateway

Connect the Nexus N3 BLE Gateway to the computer using USB.

The tooling works with an explicit serial port on any supported desktop OS.

Linux currently has the nicest stable-port discovery path because
`/dev/serial/by-id/` usually exposes persistent USB serial names. On Windows
and macOS, the tooling still works, but you usually identify the port from the
OS device list and pass it explicitly.

Examples:

.. code-block:: text

Linux:   /dev/serial/by-id/usb-ZEPHYR_IFMCU_CMSIS-DAP_...-if01
Windows: COM3
macOS:   /dev/cu.usbmodem...

On Linux, stable serial-device names are normally available under:

.. code-block:: text

/dev/serial/by-id/

List the available devices:

.. code-block:: bash

ls -l /dev/serial/by-id/

Look for an entry containing:

.. code-block:: text

ZEPHYR_IFMCU_CMSIS-DAP

The gateway may expose more than one serial interface. For Nexus N3 BLE communication, use the entry containing `if01`.

For example:

.. code-block:: text

/dev/serial/by-id/usb-ZEPHYR_IFMCU_CMSIS-DAP_...-if01

Store the detected path in a shell variable:

.. code-block:: bash

GATEWAY_PORT="$(find /dev/serial/by-id 
-maxdepth 1 
-type l 
-name '*ZEPHYR_IFMCU_CMSIS-DAP*if01*' 
-print 
-quit)"

Display the result:

.. code-block:: bash

echo "$GATEWAY_PORT"

The command should print a path under `/dev/serial/by-id/`.

If nothing is printed, disconnect and reconnect the gateway, wait a few seconds, and run the command again.

On Windows, identify the COM port in Device Manager and pass it directly, for example:

.. code-block:: bash

nexus-n3-movella-dot --port COM3 --sensor-count 1 --stream-seconds 10 --use-startup-gate

On macOS, identify the gateway under `/dev/cu.*` and pass that path directly, for example:

.. code-block:: bash

nexus-n3-movella-dot --port /dev/cu.usbmodem12345 --sensor-count 1 --stream-seconds 10 --use-startup-gate

## Prepare the Movella DOT

Before starting the test:

#. Place one Movella DOT close to the gateway.
#. Turn the sensor on or wake it using the Movella DOT charger.
#. Confirm that it is not connected to another application or Bluetooth host.

## Run the Single-Sensor Test

If you installed from a source checkout, run from the repository root:

.. code-block:: bash

python MovellaDot/stream_client.py 
--port "$GATEWAY_PORT" 
--sensor-count 1 
--stream-seconds 10 
--use-startup-gate

or using the port path directly:

.. code-block:: bash

python MovellaDot/stream_client.py \
  --port /dev/serial/by-id/usb-ZEPHYR_IFMCU_CMSIS-DAP_820D9A5F0D64CFA48AEBA-if01 \
  --sensor-count 1 \
  --stream-seconds 10 \
  --use-startup-gate

If you installed from pip, run the installed command instead:

.. code-block:: bash

nexus-n3-movella-dot --port "$GATEWAY_PORT" --sensor-count 1 --stream-seconds 10 --use-startup-gate

This starts a ten-second test using one Movella DOT.

The client will:

#. open the gateway serial port
#. reset the gateway session
#. scan for a Movella DOT
#. connect to the detected sensor
#. configure the sensor for streaming
#. start the data stream
#. verify that the stream is stable
#. collect data for the requested duration
#. stop the stream and disconnect cleanly
#. print a final stream summary

## What Success Looks Like

At the beginning of the test, the client should print the serial port it opened.
The exact value depends on the operating system:

.. code-block:: text

Opening serial port '/dev/serial/by-id/...' at baudrate 1000000...
Opening serial port 'COM3' at baudrate 1000000...
Opening serial port '/dev/cu.usbmodem12345' at baudrate 1000000...

During the test, expect messages similar to:

.. code-block:: text

Scanning for up to 1 sensor...
SCAN MATCH: ...
SCAN COMPLETE: ...
CONNECTED: ...
Selected addresses: [...]
Starting stream. Total stream budget: 10.0s.
Waiting for startup stability gate: ...
Startup stability gate passed. Official measurement is now active.
STOP STREAM ...

The final summary should show:

* a non-zero packet count
* no unexpected disconnect
* no configuration or streaming timeout
* an observed sample rate close to the configured rate

## Automatic Port Detection

The tooling can normally find the Nexus N3 BLE Gateway automatically. This is
most reliable on Linux, where the gateway is often visible under
`/dev/serial/by-id/`. After confirming that the explicit port works, the
shorter command can be used:

.. code-block:: bash

python MovellaDot/stream_client.py 
--sensor-count 1 
--stream-seconds 10 
--use-startup-gate

The equivalent installed command is:

.. code-block:: bash

nexus-n3-movella-dot 
--sensor-count 1 
--stream-seconds 10 
--use-startup-gate

## Troubleshooting

No serial device is listed

```

Check that the gateway is connected with a USB cable that supports data rather than charging only.

Reconnect the gateway and run:

.. code-block:: bash

   ls -l /dev/serial/by-id/

Permission denied
~~~~~~~~~~~~~~~~~

On Ubuntu and similar Linux distributions, serial-port access is commonly provided through the ``dialout`` group.

Add the current user to that group:

.. code-block:: bash

   sudo usermod -aG dialout "$USER"

Log out and back in before retrying the test.

Serial device is busy
~~~~~~~~~~~~~~~~~~~~~

Check whether another process has opened the gateway:

.. code-block:: bash

   sudo lsof "$GATEWAY_PORT"

Close the process using the port before running the sample again.

Wrong or unknown serial port on Windows or macOS
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Pass the gateway port explicitly instead of relying on Linux-style `/dev/serial/by-id/` discovery.

Examples:

.. code-block:: bash

   nexus-n3-movella-dot --port COM3 --sensor-count 1 --stream-seconds 10 --use-startup-gate

.. code-block:: bash

   nexus-n3-movella-dot --port /dev/cu.usbmodem12345 --sensor-count 1 --stream-seconds 10 --use-startup-gate

No Movella DOT is found
~~~~~~~~~~~~~~~~~~~~~~~

Confirm that:

* the sensor is powered and awake
* the sensor is close to the gateway
* the sensor is not connected to another Bluetooth host
* only one DOT is being used for the first test

Startup stability gate fails
```

Move the sensor closer to the gateway and retry the test.

You can also run a longer test:

.. code-block:: bash

python MovellaDot/stream_client.py 
--port "$GATEWAY_PORT" 
--sensor-count 1 
--stream-seconds 20 
--use-startup-gate

## Next Steps

After the single-sensor test succeeds, you can:

* increase `--sensor-count` to test multiple Movella DOT sensors
* change the output rate using `--sampling-rate-hz`
* write parsed sensor data to a CSV file using `--write-to-file`
* use the interactive capture workflow for recorded sessions

For example, to save a twenty-second single-sensor stream:

.. code-block:: bash

python MovellaDot/stream_client.py 
--port "$GATEWAY_PORT" 
--sensor-count 1 
--stream-seconds 20 
--use-startup-gate 
--write-to-file
