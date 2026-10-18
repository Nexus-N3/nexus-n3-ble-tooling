"""Timestamp metadata with explicit evidence sources, independent of local Git IDs."""
from __future__ import annotations


def timestamp_provenance(hello: dict | None = None, *, supplied_firmware_id: str | None = None,
                         inspected_tick_rate_hz: int = 31_250) -> dict:
    """Only explicit device fields can replace supplied inspection metadata.

    The inspected protocol currently reports neither field. Retain the raw hello
    separately, and recognize explicit firmware_build_id/sys_clock_ticks_per_sec
    fields if a running firmware supplies them. protocol_version is never a build ID.
    """
    hello = hello or {}
    firmware_id = hello.get("firmware_build_id")
    if isinstance(firmware_id, str) and firmware_id.strip():
        firmware = {"value": firmware_id, "obtained_from": "device_reported: hello_ack.firmware_build_id",
                    "device_reported": True}
    elif supplied_firmware_id:
        firmware = {"value": supplied_firmware_id, "obtained_from": "user_supplied: --gateway-firmware-id",
                    "device_reported": False}
    else:
        firmware = {"value": "unknown", "obtained_from": "not reported by existing protocol; no identifier supplied",
                    "device_reported": False}
    tick_rate = hello.get("sys_clock_ticks_per_sec")
    if type(tick_rate) is int and tick_rate > 0:
        ticks = {"value": tick_rate, "unit": "Hz",
                 "obtained_from": "device_reported: hello_ack.sys_clock_ticks_per_sec",
                 "device_reported": True}
    else:
        ticks = {"value": inspected_tick_rate_hz, "unit": "Hz",
                 "obtained_from": "supplied inspected-build CONFIG_SYS_CLOCK_TICKS_PER_SEC; --inspected-gateway-tick-rate-hz",
                 "device_reported": False}
    return {
        "source": {
            "value": "DOT payload timestamp", "unit": "microseconds", "width_bits": 32,
            "obtained_from": "MovellaDot/profile.py parse_sensor_timestamp: payload bytes 0..3, <I; supplied DOT timestamp semantics",
            "effective_resolution": "unknown; microsecond units do not establish source resolution",
        },
        "reference": {
            "value": "gateway uptime sampled in GATT notification callback processing",
            "unit": "microseconds", "width_bits": 64,
            "obtained_from": "local firmware/protocol source inspection and supplied verified timestamp semantics; not runtime instrumentation",
            "capture_expression": {
                "value": "k_ticks_to_us_floor64(k_uptime_ticks())",
                "obtained_from": "src/ble/ble_interface.c notify_cb(), while calling on_notification, after data-null check and receive-sequence increment",
            },
            "payload_pairing": {
                "value": "timestamp and same notification payload forwarded unchanged through app/gateway.c, scheduler queue, and gateway_interface_send_stream_frame",
                "obtained_from": "local firmware/protocol source inspection and supplied verification",
            },
            "capture_stage": "before application notification queueing, UART pacing, USB delivery, and host parsing",
            "interpretation": "GATT callback processing time; neither radio hardware reception time nor sensor measurement time",
            "tick_rate": ticks,
            "effective_tick_resolution": {
                "value": 1_000_000 / ticks["value"], "unit": "microseconds",
                "obtained_from": "derived as 1000000 / reference.tick_rate.value; inherits that metadata's evidence source",
                "device_reported_tick_rate": ticks["device_reported"],
                "runtime_verified_resolution": False,
            },
        },
        "running_gateway_firmware_build_identifier": firmware,
        "supplied_metadata": {"firmware_build_identifier": supplied_firmware_id,
                              "inspected_tick_rate_hz": inspected_tick_rate_hz},
        "host_elapsed_seconds": "progress/watchdog field; not a host receive timestamp",
        "mapping": "DOT to gateway reference timeline only; reference_time_ns = gateway_timestamp_us * 1000",
    }
