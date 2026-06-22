from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from NexusBLESdk import DEFAULT_PORT, GatewayClient, StartupGateConfig, open_gateway_serial

from .adapters import CaptureAdapter, SensorSpec, create_adapter, get_sensor_spec
from .session import SessionPaths, create_session_paths, to_utc_iso8601, write_manifest


class CaptureCancelled(RuntimeError):
    pass


@dataclass
class CaptureConfig:
    sensor_type: str
    sensor_count: int
    tag: str
    locations: list[str] = field(default_factory=list)
    identify: bool = False
    port: str = DEFAULT_PORT
    scan_timeout_ms: int = 5000
    connect_timeout_s: float = 30.0
    subscribe_timeout_s: float = 10.0
    write_timeout_s: float = 10.0
    disconnect_timeout_s: float = 5.0
    post_connect_settle_seconds: float = 2.0
    read_timeout_s: float = 10.0
    startup_stability_window_seconds: float | None = None
    startup_packets_required: int | None = None
    startup_min_rate_hz: float | None = None
    startup_min_observation_seconds: float | None = None
    startup_max_gap_events: int | None = None
    startup_gap_grace_seconds: float | None = None
    use_startup_gate: bool | None = None
    without_response: bool = False
    sampling_rate_hz: int | None = None
    duration_seconds: float | None = None
    output_root: Path | None = None


def validate_sensor_count(sensor_count: int) -> int:
    if sensor_count < 1:
        raise ValueError("sensor_count must be at least 1")
    return sensor_count


def validate_location_count(locations: list[str], expected_count: int) -> list[str]:
    cleaned = [location.strip() for location in locations if location.strip()]
    if len(cleaned) != expected_count:
        raise ValueError(f"Expected {expected_count} locations, got {len(cleaned)}")
    return cleaned


def build_startup_gate_config(config: CaptureConfig, spec: SensorSpec) -> StartupGateConfig:
    defaults = spec.startup_gate_defaults
    return StartupGateConfig(
        enabled=defaults["enabled"] if config.use_startup_gate is None else config.use_startup_gate,
        stability_window_seconds=defaults["stability_window_seconds"]
        if config.startup_stability_window_seconds is None
        else config.startup_stability_window_seconds,
        packets_required=defaults["packets_required"]
        if config.startup_packets_required is None
        else config.startup_packets_required,
        min_rate_hz=defaults["min_rate_hz"] if config.startup_min_rate_hz is None else config.startup_min_rate_hz,
        min_observation_seconds=defaults["min_observation_seconds"]
        if config.startup_min_observation_seconds is None
        else config.startup_min_observation_seconds,
        max_gap_events=defaults["max_gap_events"]
        if config.startup_max_gap_events is None
        else config.startup_max_gap_events,
        gap_grace_seconds=defaults["gap_grace_seconds"]
        if config.startup_gap_grace_seconds is None
        else config.startup_gap_grace_seconds,
    )


def build_manifest(
    *,
    session_paths: SessionPaths,
    config: CaptureConfig,
    spec: SensorSpec,
    connections,
    location_by_address: dict[str, str],
    status: str,
    started_at: datetime | None,
    stopped_at: datetime | None,
    output_files: list[str],
    startup_gate_config: StartupGateConfig,
    error: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "capture_tag": config.tag,
        "error": error,
        "output_files": output_files,
        "port": config.port,
        "requested_sensor_count": config.sensor_count,
        "sampling_rate_hz": config.sampling_rate_hz,
        "sensor_family": spec.display_name,
        "sensor_type": spec.sensor_type,
        "session_id": session_paths.session_id,
        "session_dir": str(session_paths.session_dir),
        "started_at": to_utc_iso8601(started_at),
        "startup_gate": {
            "enabled": startup_gate_config.enabled,
            "gap_grace_seconds": startup_gate_config.gap_grace_seconds,
            "max_gap_events": startup_gate_config.max_gap_events,
            "min_observation_seconds": startup_gate_config.min_observation_seconds,
            "min_rate_hz": startup_gate_config.min_rate_hz,
            "packets_required": startup_gate_config.packets_required,
            "stability_window_seconds": startup_gate_config.stability_window_seconds,
        },
        "status": status,
        "stopped_at": to_utc_iso8601(stopped_at),
        "sensors": [
            {
                "address": connection.address,
                "location": location_by_address.get(connection.address),
                "sensor_id": connection.sensor_id,
            }
            for connection in connections
        ],
        **(extra or {}),
    }


def choose_location(
    *,
    prompt_func: Callable[[str], str],
    output_func: Callable[[str], None],
    address: str,
    index: int,
    default_locations: list[str],
) -> str:
    output_func(f"Assign location for sensor {index + 1} ({address}).")
    for option_index, location in enumerate(default_locations, start=1):
        output_func(f"  {option_index}. {location}")
    output_func(f"  {len(default_locations) + 1}. CUSTOM")
    while True:
        raw_choice = prompt_func("Choose location number: ").strip()
        _raise_if_cancelled(raw_choice)
        try:
            choice = int(raw_choice)
        except ValueError:
            output_func("Enter a valid number.")
            continue
        if 1 <= choice <= len(default_locations):
            return default_locations[choice - 1]
        if choice == len(default_locations) + 1:
            custom = prompt_func("Enter custom location: ").strip()
            _raise_if_cancelled(custom)
            if custom:
                return custom
            output_func("Custom location cannot be empty.")
            continue
        output_func("Choice out of range.")


def _raise_if_cancelled(value: str) -> None:
    if value.strip().lower() in {"q", "quit", "exit", "cancel"}:
        raise CaptureCancelled("Capture cancelled by operator.")


def resolve_locations(
    *,
    adapter: CaptureAdapter,
    connections,
    config: CaptureConfig,
    prompt_func: Callable[[str], str],
    output_func: Callable[[str], None],
) -> dict[str, str]:
    if config.locations:
        validated = validate_location_count(config.locations, len(connections))
        return {connection.address: validated[index] for index, connection in enumerate(connections)}

    if config.identify and not adapter.spec.supports_identify:
        output_func(f"Identify is not supported for {adapter.spec.display_name}; assigning by connection order.")

    assignments: dict[str, str] = {}
    defaults = list(adapter.spec.default_locations)
    for index, connection in enumerate(connections):
        if config.identify and adapter.spec.supports_identify:
            output_func(f"Identifying sensor {index + 1}/{len(connections)}: {connection.address}")
            adapter.identify_sensor(
                connection.address,
                read_timeout_s=config.read_timeout_s,
                write_timeout_s=config.write_timeout_s,
                without_response=config.without_response,
            )
            confirm = prompt_func(
                "Confirm the highlighted sensor is ready, then press Enter to assign its location "
                "(or type quit to cancel): "
            )
            _raise_if_cancelled(confirm)
        assignments[connection.address] = choose_location(
            prompt_func=prompt_func,
            output_func=output_func,
            address=connection.address,
            index=index,
            default_locations=defaults,
        )
    return assignments


def _manual_stop_callback(prompt_func: Callable[[str], str], stop_event: threading.Event) -> None:
    try:
        prompt_func("Press Enter to stop capture. ")
    finally:
        stop_event.set()


def run_capture_session(
    config: CaptureConfig,
    *,
    prompt_func: Callable[[str], str] = input,
    output_func: Callable[[str], None] = print,
    adapter_factory: Callable[[str, Any], CaptureAdapter] = create_adapter,
    gateway_factory: Callable[[Any], Any] = GatewayClient,
    serial_opener: Callable[[str], Any] = open_gateway_serial,
    should_stop: Callable[[], bool] | None = None,
    await_start: Callable[[], None] | None = None,
    time_source: Callable[[], float] = time.monotonic,
    sleep_func: Callable[[float], None] = time.sleep,
    now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    validate_sensor_count(config.sensor_count)
    spec = get_sensor_spec(config.sensor_type)
    if spec.max_sensor_count is not None and config.sensor_count > spec.max_sensor_count:
        raise ValueError(f"{spec.display_name} supports at most {spec.max_sensor_count} sensor(s)")
    sampling_rate_hz = config.sampling_rate_hz or spec.default_sampling_rate_hz
    if sampling_rate_hz not in spec.sampling_rates_hz:
        raise ValueError(
            f"Unsupported sampling rate for {spec.display_name}: {sampling_rate_hz} "
            f"(supported: {list(spec.sampling_rates_hz)})"
        )
    config.sampling_rate_hz = sampling_rate_hz

    session_paths = create_session_paths(config.tag, root_dir=config.output_root)
    startup_gate_config = build_startup_gate_config(config, spec)
    started_at_utc: datetime | None = None
    stopped_at_utc: datetime | None = None
    status = "failed"
    error: str | None = None
    output_files: list[str] = []
    connections = []
    location_by_address: dict[str, str] = {}
    extra: dict[str, Any] = {}
    adapter: CaptureAdapter | None = None
    monitor = None
    disconnect_needed = False
    streams_started = False
    streams_stopped = False

    try:
        with serial_opener(config.port) as ser:
            gateway = gateway_factory(ser, client_name="capture_cli")
            adapter = adapter_factory(spec.sensor_type, gateway)
            if hasattr(adapter, "attach_output"):
                adapter.attach_output(session_paths.session_dir)
            output_files = adapter.output_files

            gateway.phase = "reset_session"
            gateway.reset_session()
            gateway.phase = "hello"
            gateway.hello()

            gateway.phase = "scan"
            selected = adapter.discover(config.sensor_count, config.scan_timeout_ms)
            output_func(f"Selected addresses: {selected}")

            gateway.phase = "connect"
            connections = adapter.connect(selected, timeout_s=config.connect_timeout_s)
            disconnect_needed = bool(connections)
            location_by_address = resolve_locations(
                adapter=adapter,
                connections=connections,
                config=config,
                prompt_func=prompt_func,
                output_func=output_func,
            )
            labels_by_address = {
                connection.address: location_by_address.get(connection.address)
                for connection in connections
            }
            monitor = adapter.create_monitor(
                labels_by_address=labels_by_address,
                sampling_rate_hz=sampling_rate_hz,
                startup_gate_config=startup_gate_config,
            )

            if config.post_connect_settle_seconds > 0:
                gateway.phase = "post_connect_settle"
                output_func(
                    "All sensors connected. "
                    f"Waiting {config.post_connect_settle_seconds:.1f}s for BLE links/params to settle."
                )
                sleep_func(config.post_connect_settle_seconds)

            gateway.phase = "configure"
            adapter.configure(
                sampling_rate_hz=sampling_rate_hz,
                subscribe_timeout_s=config.subscribe_timeout_s,
                write_timeout_s=config.write_timeout_s,
                without_response=config.without_response,
            )

            if config.post_connect_settle_seconds > 0:
                gateway.phase = "post_config_settle"
                output_func(
                    "All sensors configured. "
                    f"Waiting {config.post_connect_settle_seconds:.1f}s before stream start."
                )
                sleep_func(config.post_connect_settle_seconds)

            if await_start is not None:
                await_start()

            gateway.phase = "start_streams"
            output_func("Starting capture stream.")
            started = adapter.start_streams(
                write_timeout_s=config.write_timeout_s,
                without_response=config.without_response,
            )
            streams_started = True
            for address, command_time in started.items():
                monitor.mark_stream_started(address, command_time)
            monitor.announce_startup_state()
            started_at_utc = now_func().astimezone(timezone.utc)

            stop_event = threading.Event()
            stop_thread = None
            if should_stop is None and not config.duration_seconds:
                stop_thread = threading.Thread(
                    target=_manual_stop_callback,
                    args=(prompt_func, stop_event),
                    daemon=True,
                )
                stop_thread.start()

            gateway.phase = "monitor"
            startup_deadline = time_source() + startup_gate_config.stability_window_seconds
            timed_deadline = None if not config.duration_seconds else time_source() + config.duration_seconds

            while True:
                if timed_deadline is not None and time_source() >= timed_deadline:
                    break
                if should_stop is not None and should_stop():
                    break
                if stop_event.is_set():
                    break
                if (
                    startup_gate_config.enabled
                    and not monitor.measurement_active
                    and time_source() >= startup_deadline
                ):
                    stable, unstable = monitor.evaluate_startup_stability()
                    if not stable:
                        raise RuntimeError(
                            "Startup stability gate failed: "
                            + (", ".join(unstable) if unstable else "unknown startup instability")
                        )
                    monitor.activate_measurement()

                try:
                    item_type, item = gateway.read_item(timeout_s=0.2)
                except TimeoutError:
                    continue

                if item_type != "stream_frame":
                    if item.get("type") == "sensor_disconnected":
                        raise RuntimeError(
                            f"Unexpected disconnect during stream: {item.get('address')} reason={item.get('reason')}"
                        )
                    continue

                adapter.handle_stream_frame(item, monitor=monitor, wall_time=time_source())
                monitor.handle_stream_frame(item, time_source())

            gateway.phase = "stop_streams"
            adapter.stop_streams(
                write_timeout_s=config.write_timeout_s,
                without_response=config.without_response,
            )
            streams_stopped = True
            stopped_at_utc = now_func().astimezone(timezone.utc)

            gateway.phase = "post_stop_drain"
            monitor.drain_after_stop(gateway)

            extra.update(adapter.collect_post_capture_details(timeout_s=config.read_timeout_s))
            try:
                gateway.phase = "get_status"
                extra["gateway_status_snapshot"] = gateway.get_status_snapshot(timeout_s=10.0)
            except TimeoutError:
                extra["gateway_status_warning"] = "Timed out waiting for gateway status snapshot."

            gateway.phase = "disconnect"
            adapter.disconnect_all(timeout_s=config.disconnect_timeout_s)

            extra["stream_summary"] = monitor.summary_lines(gateway)
            status = "completed"
    except CaptureCancelled as exc:
        error = str(exc)
        status = "cancelled"
    except KeyboardInterrupt as exc:
        error = str(exc) or "Capture interrupted by operator."
        status = "interrupted"
    except Exception as exc:
        error = str(exc)
        status = "failed"
        raise
    finally:
        if adapter is not None:
            if streams_started and not streams_stopped:
                try:
                    adapter.stop_streams(
                        write_timeout_s=config.write_timeout_s,
                        without_response=config.without_response,
                    )
                    streams_stopped = True
                except Exception:
                    pass
            if disconnect_needed:
                try:
                    adapter.disconnect_all(timeout_s=config.disconnect_timeout_s)
                    disconnect_needed = False
                except Exception:
                    pass
        if adapter is not None:
            try:
                adapter.close()
            except Exception:
                pass
            output_files = adapter.output_files
        manifest = build_manifest(
            session_paths=session_paths,
            config=config,
            spec=spec,
            connections=connections,
            location_by_address=location_by_address,
            status=status,
            started_at=started_at_utc,
            stopped_at=stopped_at_utc,
            output_files=output_files,
            startup_gate_config=startup_gate_config,
            error=error,
            extra=extra,
        )
        write_manifest(session_paths.manifest_path, manifest)

    return manifest
