from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from conftest import FakeEnergy
from tapo_collector.measurements import (
    Measurement,
    clean_metric,
    format_timestamp,
    metrics_from_energy_module,
    new_measurement_id,
    offline_measurement,
    online_measurement,
    parse_timestamp,
)

BRUSSELS = ZoneInfo("Europe/Brussels")


def test_energy_module_fields_map_to_pipeline_units() -> None:
    # python-kasa already converts: mW -> W, Wh -> kWh, mV -> V, mA -> A.
    metrics = metrics_from_energy_module(FakeEnergy(power=145.7, today=0.82, month=12.345, voltage=231.2, current=0.66), "p")
    assert metrics == {"power_w": 145.7, "energy_today_kwh": 0.82, "energy_month_kwh": 12.345, "voltage_v": 231.2, "current_a": 0.66}


async def test_python_kasa_unit_conversion_from_raw_p110_payload() -> None:
    """Feed a raw P110 response through python-kasa's own Energy module code and check the units we store."""
    from kasa.smart.modules.energy import Energy

    raw = {
        "get_energy_usage": {"today_energy": 820, "month_energy": 12345, "current_power": 145700},  # Wh, Wh, mW
        "get_emeter_data": {"power_mw": 145700, "voltage_mv": 231200, "current_ma": 660},  # mW, mV, mA
    }

    class RawEnergy(Energy):
        __module__ = "tests.fakes.raw_energy"  # keeps python-kasa from registering it as a real module
        data = property(lambda self: raw)  # type: ignore[assignment]

    module = RawEnergy.__new__(RawEnergy)
    module._last_update_error = None
    await module._post_update_hook()  # python-kasa's own parsing / unit conversion
    metrics = metrics_from_energy_module(module, "p110")
    assert metrics["power_w"] == pytest.approx(145.7)
    assert metrics["energy_today_kwh"] == pytest.approx(0.820)
    assert metrics["energy_month_kwh"] == pytest.approx(12.345)
    assert metrics["voltage_v"] == pytest.approx(231.2)
    assert metrics["current_a"] == pytest.approx(0.660)


async def test_older_p110_firmware_without_emeter_data_has_no_voltage_or_current() -> None:
    from kasa.smart.modules.energy import Energy

    raw = {"get_energy_usage": {"today_energy": 5, "month_energy": 40, "current_power": 2500}}

    class RawEnergy(Energy):
        __module__ = "tests.fakes.raw_energy"  # keeps python-kasa from registering it as a real module
        data = property(lambda self: raw)  # type: ignore[assignment]

    module = RawEnergy.__new__(RawEnergy)
    module._last_update_error = None
    await module._post_update_hook()
    metrics = metrics_from_energy_module(module, "p110")
    assert metrics == {"power_w": 2.5, "energy_today_kwh": 0.005, "energy_month_kwh": 0.04, "voltage_v": None, "current_a": None}


def test_absent_optional_metrics_stay_none() -> None:
    metrics = metrics_from_energy_module(FakeEnergy(voltage=None, current=None, month=None), "p")
    assert metrics["voltage_v"] is None and metrics["current_a"] is None and metrics["energy_month_kwh"] is None
    assert metrics["power_w"] == 145.7


def test_failing_optional_property_does_not_fail_reading() -> None:
    metrics = metrics_from_energy_module(FakeEnergy(raise_on=("voltage", "current")), "p")
    assert metrics["voltage_v"] is None and metrics["current_a"] is None
    assert metrics["energy_today_kwh"] == 0.82


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("power_w", 0, 0.0),
        ("power_w", 3680.0, 3680.0),
        ("power_w", -1.0, None),
        ("power_w", 10_000, None),
        ("power_w", float("nan"), None),
        ("power_w", float("inf"), None),
        ("power_w", "12", None),
        ("power_w", True, None),
        ("voltage_v", 400, None),
        ("current_a", 25, None),
        ("energy_today_kwh", 200, None),
        ("energy_month_kwh", 3000, 3000.0),
    ],
)
def test_clean_metric_validation(name: str, value: object, expected: float | None) -> None:
    assert clean_metric(name, value) == expected


def test_timestamp_format_is_utc_with_milliseconds() -> None:
    ts = datetime(2026, 11, 3, 12, 0, 0, 123456, tzinfo=UTC)
    assert format_timestamp(ts) == "2026-11-03T12:00:00.123Z"
    assert parse_timestamp("2026-11-03T12:00:00.123Z") == ts.replace(microsecond=123000)


@pytest.mark.parametrize(
    ("local", "utc"),
    [
        # Winter time (CET, UTC+1): Belgian midnight is 23:00 UTC the day before.
        (datetime(2026, 11, 4, 0, 0, tzinfo=BRUSSELS), "2026-11-03T23:00:00.000Z"),
        # Summer time (CEST, UTC+2): Belgian midnight is 22:00 UTC.
        (datetime(2026, 7, 1, 0, 0, tzinfo=BRUSSELS), "2026-06-30T22:00:00.000Z"),
    ],
)
def test_local_belgian_times_are_converted_to_utc(local: datetime, utc: str) -> None:
    m = online_measurement("c", "printer_01", local, {"power_w": 1.0})
    assert m.timestamp.tzinfo == UTC
    assert format_timestamp(m.timestamp) == utc
    assert m.to_api()["timestamp"] == utc


def test_naive_timestamps_are_refused() -> None:
    with pytest.raises(ValueError):
        offline_measurement("c", "d", datetime(2026, 11, 3, 12, 0))


def test_measurement_ids_are_unique_even_within_the_same_millisecond() -> None:
    ts = datetime(2026, 11, 3, 12, 0, 0, 123000, tzinfo=UTC)
    readings = [online_measurement("c", "d", ts, {"power_w": 1.0}) for _ in range(50)] + [offline_measurement("c", "d", ts) for _ in range(50)]
    ids = [m.measurement_id for m in readings]
    assert len(set(ids)) == 100
    api_pattern = re.compile(r"^[A-Za-z0-9_-]{8,100}$")  # docs/API.md
    assert all(api_pattern.match(i) and uuid.UUID(i).version == 4 for i in ids)
    assert new_measurement_id() != new_measurement_id()


def test_offline_measurement_has_no_values_and_cannot_carry_any() -> None:
    m = offline_measurement("c", "d", datetime(2026, 11, 3, tzinfo=UTC))
    api = m.to_api()
    assert api["online"] is False
    assert all(api[k] is None for k in ("power_w", "energy_today_kwh", "energy_month_kwh", "voltage_v", "current_a"))
    with pytest.raises(ValueError):
        Measurement("id", "d", datetime(2026, 11, 3, tzinfo=UTC), online=False, power_w=5.0)


def test_api_payload_shape() -> None:
    m = online_measurement("c", "printer_01", datetime(2026, 11, 3, 12, tzinfo=UTC), {"power_w": 145.7, "energy_today_kwh": 0.82})
    assert m.to_api() == {
        "measurement_id": m.measurement_id,
        "device_id": "printer_01",
        "timestamp": "2026-11-03T12:00:00.000Z",
        "online": True,
        "power_w": 145.7,
        "energy_today_kwh": 0.82,
        "energy_month_kwh": None,
        "voltage_v": None,
        "current_a": None,
    }
