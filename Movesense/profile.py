'''
    Movesense BLE profile parsing and utilities.
'''

from __future__ import annotations

import struct

# Movsense constants 
MOVESENSE_NAME_PREFIX = "Movesense"
MOVESENSE_WRITE_UUID = "34800001-7185-4d5d-b431-630e7050e8f0"
MOVESENSE_NOTIFY_UUID = "34800002-7185-4d5d-b431-630e7050e8f0"
MOVESENSE_SAMPLING_RATES_HZ = (200,)
MOVESENSE_ECG_STREAM_ID = 100
MOVESENSE_HR_STREAM_ID = 1
MOVESENSE_TEMP_STREAM_ID = 2
MOVESENSE_ECG_SAMPLES_PER_PACKET = 16 # packed samples in a ble packet
# movesense packet types 1 = response ack and 2 is a packet
MOVESENSE_PACKET_TYPE_GET_RESPONSE = 1
MOVESENSE_PACKET_TYPE_DATA = 2
MOVESENSE_MIN_PACKET_LEN = 6
ECG_SAMPLE_SCALE_MV = 0.38147 * 0.001
DEFAULT_LOCATIONS = [
    "CHEST",
]

# Across sensor startup gate config (this should be moved to the sdk)
DEFAULT_STARTUP_GATE = {
    "enabled": True,
    "stability_window_seconds": 5.0,
    "packets_required": 20,
    "min_rate_hz": 10.0,
    "min_observation_seconds": 1.5,
    "max_gap_events": 0,
    "gap_grace_seconds": 0.5,
}



def parse_ecg_packet_timestamp_us(payload: bytes) -> int:
    '''
    Parses the ECG packet timestamp in microseconds from the given payload.
     - Movesense ECG packets have a 4-byte timestamp in milliseconds at offset 2.
     - This function converts it to microseconds by multiplying by 1000.
     - If the payload is too short or not a data packet, it raises a ValueError.
     - Returns the timestamp in microseconds as an integer.
    '''
    if len(payload) < MOVESENSE_MIN_PACKET_LEN:
        raise ValueError(f"Movesense payload too short: {len(payload)} bytes")
    if payload[0] != MOVESENSE_PACKET_TYPE_DATA:
        raise ValueError(f"Movesense payload is not a data packet: type={payload[0]}")
    # Movesense ECG packets carry a millisecond timestamp at offset 2.
    return int(struct.unpack_from("<I", payload, 2)[0]) * 1000


def parse_ecg_packet_sample_count(payload: bytes) -> int:
    '''
    Parses the number of ECG samples in the given payload.
     - Movesense ECG packets have a 4-byte timestamp in milliseconds at offset 2, followed by ECG samples starting at offset 6.
     - The number of samples can be determined by the total payload length minus the 6 bytes of header, divided by the sample size (4 bytes for 32-bit samples, or 2 bytes for 16-bit samples).
     - If the payload is too short or not a data packet, it returns 0.
     - If the payload length indicates a full packet of 16 samples (64 bytes of data) or 32 samples (32 bytes of data), it returns the fixed sample count of 16.
     - Otherwise, it calculates the sample count based on the remaining payload length after the header. If the remaining length is not a multiple of the sample size, it returns 0 to indicate an invalid packet.
     - Returns the number of ECG samples as an integer.
    '''
    if len(payload) < MOVESENSE_MIN_PACKET_LEN or payload[0] != MOVESENSE_PACKET_TYPE_DATA:
        return 0

    payload_len = len(payload) - 6
    if payload_len == 64 or payload_len == 32:
        return MOVESENSE_ECG_SAMPLES_PER_PACKET
    if payload_len <= 0:
        return 0
    return payload_len // 4


def parse_ecg_sample_values_mv(payload: bytes) -> list[float]:
    '''
    Parses the ECG sample values in millivolts from the given payload.
     - Movesense ECG packets have a 4-byte timestamp in milliseconds at offset 2, followed by ECG samples starting at offset 6.
     - Each ECG sample is a 32-bit signed integer representing the raw ADC value, which can be converted to millivolts by multiplying by the scale factor (0.38147 mV per LSB).
     - If the payload is too short or not a data packet, it returns an empty list.
     - If the payload length indicates a full packet of 16 samples (64 bytes of data) or 32 samples (32 bytes of data), it parses the fixed number of samples accordingly.
     - Otherwise, it parses as many samples as indicated by the remaining payload length after the header, ensuring that it does not read beyond the payload length. If the remaining length is not a multiple of the sample size, it returns an empty list to indicate an invalid packet.
     - Returns a list of ECG sample values in millivolts as floats. 
    '''
    if len(payload) < MOVESENSE_MIN_PACKET_LEN or payload[0] != MOVESENSE_PACKET_TYPE_DATA:
        return []

    payload_len = len(payload) - 6 # starts after the 6-byte header (type, stream id, timestamp)
    if payload_len <= 0:
        return []

    if payload_len == 64:
        return [
            struct.unpack("<i", payload[6 + index * 4:10 + index * 4])[0] * ECG_SAMPLE_SCALE_MV
            for index in range(MOVESENSE_ECG_SAMPLES_PER_PACKET)
        ]
    if payload_len == 32:
        return [
            struct.unpack("<h", payload[6 + index * 2:8 + index * 2])[0] * ECG_SAMPLE_SCALE_MV
            for index in range(MOVESENSE_ECG_SAMPLES_PER_PACKET)
        ]

    values: list[float] = []
    sample_count = payload_len // 4
    for index in range(sample_count):
        offset = 6 + index * 4
        if offset + 4 > len(payload):
            break
        values.append(struct.unpack("<i", payload[offset:offset + 4])[0] * ECG_SAMPLE_SCALE_MV)
    return values


def parse_hr_value(payload: bytes) -> float | None:
    '''
    Parses the heart rate value in beats per minute from the given payload.
     - Movesense heart rate packets have a 4-byte timestamp in milliseconds at offset 2, followed by a 4-byte float value representing the heart rate in bpm.
     - If the payload is too short or not a data packet, it returns None.
     - If the payload length is sufficient to contain the heart rate value, it parses the float value at the expected offset. If the payload length is not sufficient, it returns None to indicate an invalid packet.
     - Returns the heart rate value in beats per minute as a float, or None if it cannot be parsed.
    '''
    if len(payload) < 8:
        return None
    return float(struct.unpack("<f", payload[2:6])[0])


def parse_temp_value(payload: bytes) -> float | None:
    '''
    Parses the temperature value in degrees Celsius from the given payload.
     - Movesense temperature packets have a 4-byte timestamp in milliseconds at offset 2, followed by a 4-byte float value representing the temperature in Celsius.
     - If the payload is too short or not a data packet, it returns None.
     - If the payload length is sufficient to contain the temperature value, it parses the float value at the expected offset. If the payload length is not sufficient, it returns None to indicate an invalid packet.
     - Returns the temperature value in degrees Celsius as a float, or None if it cannot be parsed.
    '''
    if len(payload) < 6:
        return None
    if len(payload) >= 10:
        temp = struct.unpack("<f", payload[2:6])[0]
    else:
        values = struct.unpack("<" + ((len(payload) - 6) // 4) * "f", payload[6:])
        if not values:
            return None
        temp = float(values[0])
    if temp > 200:
        temp -= 273.15
    return float(temp)


def parse_ecg_sample_timestamps_ms(payload: bytes, sampling_rate_hz: int) -> list[int]:
    '''
        Parses the ECG sample timestamps in milliseconds from the given payload and sampling rate.
         - Movesense ECG packets have a 4-byte timestamp in milliseconds at offset 2, followed by ECG samples starting at offset 6.
         - The sample timestamps can be calculated based on the packet timestamp and the sampling rate. The first sample timestamp corresponds to the packet timestamp, and subsequent samples are spaced by the inverse of the sampling rate (e.g., 5 ms for 200 Hz).
         - If the payload is too short or not a data packet, it returns an empty list.
         - If the payload length indicates a full packet of 16 samples (64 bytes of data) or 32 samples (32 bytes of data), it calculates the timestamps for the fixed number of samples accordingly.
         - Otherwise, it calculates the timestamps for as many samples as indicated by the remaining payload length after the header, ensuring that it does not read beyond the payload length. If the remaining length is not a multiple of the sample size, it returns an empty list to indicate an invalid packet.
         - Returns a list of ECG sample timestamps in milliseconds as integers. 
    '''
    sample_count = parse_ecg_packet_sample_count(payload)
    if sample_count <= 0:
        return []
    packet_timestamp_ms = int(struct.unpack_from("<I", payload, 2)[0])
    return [
        packet_timestamp_ms + int(index * 1000 / sampling_rate_hz)
        for index in range(sample_count)
    ]


def summarize_payload(payload: bytes, sampling_rate_hz: int) -> dict:
    '''
    Summarizes the given payload by extracting key information based on the packet type and stream ID.
     - For data packets, it extracts the packet timestamp, sample count, and sample values for ECG streams, and the value for heart rate and temperature streams.
     - It returns a dictionary containing the packet type, stream ID, payload length, and any parsed values or timestamps. This summary can be used for logging or debugging purposes to understand the contents of the payload without needing to parse all details.
     - If the payload is not a data packet or is too short, it returns a summary with basic information and empty values.
     - The summary includes:
        - "packet_type": The type of the packet (e.g., data or response).
        - "stream_id": The stream ID indicating the type of data (e.g., ECG, heart rate, temperature).
        - "payload_hex": The hexadecimal representation of the raw payload for reference.
        - "payload_len": The length of the payload in bytes.
        - For ECG packets: "packet_timestamp_ms", "packet_timestamp_us", "ecg_sample_count", "ecg_values_mv", "sample_timestamps_ms", "first_sample_timestamp_ms", "last_sample_timestamp_ms".
        - For heart rate packets: "hr_value".
        - For temperature packets: "temp_c".
     - Returns a dictionary summarizing the payload contents.   
    '''
    packet_type = payload[0] if payload else None
    stream_id = payload[1] if len(payload) >= 2 else None
    summary = {
        "packet_type": packet_type,
        "stream_id": stream_id,
        "payload_hex": payload.hex(),
        "payload_len": len(payload),
    }
    if packet_type == MOVESENSE_PACKET_TYPE_DATA:
        if stream_id == MOVESENSE_ECG_STREAM_ID and len(payload) >= MOVESENSE_MIN_PACKET_LEN:
            sample_timestamps_ms = parse_ecg_sample_timestamps_ms(payload, sampling_rate_hz)
            summary.update(
                {
                    "packet_timestamp_ms": int(struct.unpack_from("<I", payload, 2)[0]),
                    "packet_timestamp_us": parse_ecg_packet_timestamp_us(payload),
                    "ecg_sample_count": parse_ecg_packet_sample_count(payload),
                    "ecg_values_mv": parse_ecg_sample_values_mv(payload),
                    "sample_timestamps_ms": sample_timestamps_ms,
                    "first_sample_timestamp_ms": sample_timestamps_ms[0] if sample_timestamps_ms else None,
                    "last_sample_timestamp_ms": sample_timestamps_ms[-1] if sample_timestamps_ms else None,
                }
            )
        elif stream_id == MOVESENSE_HR_STREAM_ID:
            summary["hr_value"] = parse_hr_value(payload)
        elif stream_id == MOVESENSE_TEMP_STREAM_ID:
            summary["temp_c"] = parse_temp_value(payload)
    return summary


def iter_parsed_rows(
    payload: bytes,
    *,
    sampling_rate_hz: int,
    address: str | None,
    sensor_id: int,
    gateway_timestamp_us: int,
) -> list[dict]:
    '''
    Parses the given payload and yields rows of data based on the packet type and stream ID.
     - For ECG data packets, it extracts the sample timestamps and values, and yields a row for each sample with detailed information including the sensor ID, stream type, timestamps, sample index, sampling rate, value in millivolts, and unit.
     - For heart rate and temperature data packets, it extracts the value and yields a single row with the corresponding information.
     - If the payload is not a data packet or is too short, it returns an empty list.
     - The rows are returned as a list of dictionaries, where each dictionary represents a single data point with standardized keys for address, sensor_id, stream type, timestamps, sample index, sampling rate, value, and unit. This format can be easily converted to a DataFrame or other structured format for analysis.
     - Returns a list of parsed rows extracted from the payload.
    '''
    if len(payload) < 2 or payload[0] != MOVESENSE_PACKET_TYPE_DATA:
        return []

    stream_id = payload[1]
    if stream_id == MOVESENSE_ECG_STREAM_ID:
        timestamps_ms = parse_ecg_sample_timestamps_ms(payload, sampling_rate_hz)
        values_mv = parse_ecg_sample_values_mv(payload)
        packet_timestamp_ms = int(struct.unpack_from("<I", payload, 2)[0])
        rows = []
        for index, (timestamp_ms, value_mv) in enumerate(zip(timestamps_ms, values_mv)):
            rows.append(
                {
                    "address": address or "",
                    "sensor_id": sensor_id,
                    "stream": "ecg",
                    "timestamp_ms": timestamp_ms,
                    "gateway_timestamp_us": gateway_timestamp_us,
                    "packet_timestamp_ms": packet_timestamp_ms,
                    "sample_index": index,
                    "sampling_rate_hz": sampling_rate_hz,
                    "value": value_mv,
                    "unit": "mV",
                }
            )
        return rows

    if stream_id == MOVESENSE_HR_STREAM_ID:
        hr_value = parse_hr_value(payload)
        if hr_value is None:
            return []
        return [
            {
                "address": address or "",
                "sensor_id": sensor_id,
                "stream": "hr",
                "timestamp_ms": int(gateway_timestamp_us / 1000),
                "gateway_timestamp_us": gateway_timestamp_us,
                "packet_timestamp_ms": "",
                "sample_index": 0,
                "sampling_rate_hz": "",
                "value": hr_value,
                "unit": "bpm",
            }
        ]

    if stream_id == MOVESENSE_TEMP_STREAM_ID:
        temp_value = parse_temp_value(payload)
        if temp_value is None:
            return []
        return [
            {
                "address": address or "",
                "sensor_id": sensor_id,
                "stream": "temp",
                "timestamp_ms": int(gateway_timestamp_us / 1000),
                "gateway_timestamp_us": gateway_timestamp_us,
                "packet_timestamp_ms": "",
                "sample_index": 0,
                "sampling_rate_hz": "",
                "value": temp_value,
                "unit": "C",
            }
        ]

    return []


def build_subscribe_command(stream_id: int, path: str) -> str:
    return (bytes([1, stream_id]) + path.encode("utf-8")).hex()


def build_stop_command(stream_id: int) -> str:
    return bytes([2, stream_id]).hex()


def is_movesense_match(name: str) -> bool:
    return name.startswith(MOVESENSE_NAME_PREFIX)


def select_addresses(matches, count: int) -> list[str]:
    filtered = [entry.address for entry in matches if is_movesense_match(entry.name)]
    if len(filtered) < count:
        raise RuntimeError(f"Requested {count} Movesense sensors, found {len(filtered)}")
    return filtered[:count]
