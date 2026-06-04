from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path


def ensure_output_dir() -> Path:
    output_dir = Path.cwd() / "output-files"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


# build a file output for the sensor with a timestamped filename and the given suffix (e.g. "csv")
def build_output_path(sensor_slug: str, suffix: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ensure_output_dir() / f"{sensor_slug}_{timestamp}.{suffix}"


class CsvRowWriter:
    def __init__(self, path: Path, fieldnames: list[str]):
        self.path = path
        self._file = path.open("w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=fieldnames)
        self._writer.writeheader()

    def write_row(self, row: dict):
        self._writer.writerow(row)
        self._file.flush()

    def close(self):
        self._file.close()
