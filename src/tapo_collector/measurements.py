"""Measurement model, unit handling and validation.

Units are fixed for the whole pipeline (collector SQLite, HTTP API, website database):

==================  =====  ====================================================================
field               unit   source in python-kasa 0.12 (``Module.Energy`` of a Tapo P110)
==================  =====  ====================================================================
power_w             W      ``current_consumption`` (device reports mW; python-kasa divides by 1000)
energy_today_kwh    kWh    ``consumption_today`` (device ``today_energy`` in Wh / 1000)
energy_month_kwh    kWh    ``consumption_this_month`` (device ``month_energy`` in Wh / 1000)
voltage_v           V      ``voltage`` (``get_emeter_data.voltage_mv`` / 1000) - newer firmware only
current_a           A      ``current`` (``get_emeter_data.current_ma`` / 1000) - newer firmware only
==================  =====  ====================================================================

``energy_today_kwh`` resets at the plug's *local* midnight (the time zone set in the Tapo app,
normally Europe/Brussels) and ``energy_month_kwh`` at the start of the local month. Neither is a
lifetime total; the website computes energy from counter *differences* and treats a decrease as a
reset. Values python-kasa cannot provide stay ``None`` - nothing is estimated here.
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

# Version 1.0 derived IDs from uuid5(MEASUREMENT_NAMESPACE, "collector|device|timestamp"). Readings stored by 1.0
# keep those IDs; new readings get random UUIDv4 IDs (see new_measurement_id). Kept for reference only.
MEASUREMENT_NAMESPACE = uuid.UUID("6f0c8f53-1f5e-4f4a-9a43-2f3b7f0d5a11")

# Plausibility limits. The P110 is rated 16 A / 3680 W at 230 V; the margins allow for measurement
# noise and mains voltage tolerance. The website enforces the same limits.
LIMITS: dict[str, tuple[float, float]] = {
    "power_w": (0.0, 4500.0),
    "energy_today_kwh": (0.0, 110.0),  # 4.5 kW for 24 h = 108 kWh
    "energy_month_kwh": (0.0, 3400.0),  # 4.5 kW for 31 days = 3348 kWh
    "voltage_v": (0.0, 300.0),
    "current_a": (0.0, 20.0),
}
METRICS = tuple(LIMITS)


@dataclass(frozen=True)
class Measurement:
    measurement_id: str
    device_id: str
    timestamp: datetime  # timezone-aware, UTC
    online: bool
    power_w: float | None = None
    energy_today_kwh: float | None = None
    energy_month_kwh: float | None = None
    voltage_v: float | None = None
    current_a: float | None = None

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("Measurement timestamps must be timezone-aware")
        if self.timestamp.utcoffset() != UTC.utcoffset(None):
            object.__setattr__(self, "timestamp", self.timestamp.astimezone(UTC))
        if not self.online and any(getattr(self, m) is not None for m in METRICS):
            raise ValueError("An offline measurement cannot carry metric values")

    def to_api(self) -> dict[str, Any]:
        """JSON body item for ``POST /api/energy/measurements``."""
        data = asdict(self)
        data["timestamp"] = format_timestamp(self.timestamp)
        return data


def format_timestamp(value: datetime) -> str:
    """ISO 8601 UTC with millisecond precision and a ``Z`` suffix, e.g. ``2026-11-03T12:00:00.123Z``."""
    value = value.astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp without time zone: {value}")
    return parsed.astimezone(UTC)


def new_measurement_id() -> str:
    """A new, globally unique ID for one reading (random UUIDv4).

    It is generated once, committed to SQLite together with the reading, and reused unchanged for every upload
    retry, so the website still recognises a repeated upload as a duplicate. It is deliberately *not* derived from
    the timestamp: two distinct readings with the same millisecond (system clock stepped back, a second collector
    process with the same collector_id) must never share an ID, or one of them would be silently dropped.
    Readings stored by version 1.0 keep their original (UUIDv5) IDs.
    """
    return str(uuid.uuid4())


def clean_metric(name: str, value: Any, *, device_id: str = "?") -> float | None:
    """Return a finite float within the plausible range, or ``None`` (logged) when invalid or absent."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        log.warning("Device %s: ignoring non-numeric %s=%r", device_id, name, value)
        return None
    number = float(value)
    low, high = LIMITS[name]
    if not math.isfinite(number) or not low <= number <= high:
        log.warning("Device %s: ignoring implausible %s=%r (allowed %s..%s)", device_id, name, value, low, high)
        return None
    return round(number, 6)


def _read_property(module: Any, attribute: str, device_id: str) -> Any:
    """Read one optional property from the python-kasa Energy module without failing the whole reading."""
    try:
        return getattr(module, attribute)
    except Exception as error:  # python-kasa raises when an optional query failed
        log.debug("Device %s: %s unavailable (%s)", device_id, attribute, error)
        return None


def metrics_from_energy_module(module: Any, device_id: str) -> dict[str, float | None]:
    """Map a python-kasa ``Module.Energy`` instance to pipeline field names (units already W/kWh/V/A)."""
    raw = {
        "power_w": _read_property(module, "current_consumption", device_id),
        "energy_today_kwh": _read_property(module, "consumption_today", device_id),
        "energy_month_kwh": _read_property(module, "consumption_this_month", device_id),
        "voltage_v": _read_property(module, "voltage", device_id),
        "current_a": _read_property(module, "current", device_id),
    }
    return {name: clean_metric(name, value, device_id=device_id) for name, value in raw.items()}


def online_measurement(collector_id: str, device_id: str, timestamp: datetime, metrics: dict[str, float | None]) -> Measurement:
    return Measurement(
        measurement_id=new_measurement_id(),
        device_id=device_id,
        timestamp=timestamp,
        online=True,
        **{name: metrics.get(name) for name in METRICS},
    )


def offline_measurement(collector_id: str, device_id: str, timestamp: datetime) -> Measurement:
    """Records that a poll failed. All metrics are None: nothing is invented for missing readings."""
    return Measurement(
        measurement_id=new_measurement_id(),
        device_id=device_id,
        timestamp=timestamp,
        online=False,
    )
