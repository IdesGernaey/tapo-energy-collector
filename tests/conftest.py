"""Shared fixtures: fake python-kasa devices and a settings factory. No physical plugs are needed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tapo_collector.config import Secrets, Settings, settings_from_dict
from tapo_collector.storage import Storage

TOKEN = "test-token-0123456789abcdefghijklmnopqrstuvwxyz"
ENDPOINT = "https://ccbom.example.test/api/energy/measurements"


class FakeEnergy:
    """Mimics python-kasa 0.12 ``smart.modules.Energy`` as used by a P110 (values already W / kWh / V / A)."""

    def __init__(
        self,
        power: float | None = 145.7,
        today: float | None = 0.82,
        month: float | None = 12.345,
        voltage: float | None = 231.2,
        current: float | None = 0.66,
        raise_on: tuple[str, ...] = (),
    ) -> None:
        self._values = {
            "current_consumption": power,
            "consumption_today": today,
            "consumption_this_month": month,
            "voltage": voltage,
            "current": current,
        }
        self._raise_on = raise_on

    def __getattr__(self, name: str) -> Any:
        values = self.__dict__.get("_values", {})
        if name in values:
            if name in self.__dict__["_raise_on"]:
                raise RuntimeError(f"{name} query failed")
            return values[name]
        raise AttributeError(name)


class FakeConfig:
    def __init__(self, host: str) -> None:
        self.host = host

    def to_dict_control_credentials(self, *, credentials_hash: str | None = None, exclude_credentials: bool = False) -> dict[str, Any]:
        assert credentials_hash == ""  # the collector must strip credentials
        return {
            "host": self.host,
            "timeout": 5,
            "connection_type": {"device_family": "SMART.TAPOPLUG", "encryption_type": "KLAP", "login_version": 2, "https": False},
            "aes_keys": {"private": "SECRET", "public": "x"},
        }


class FakeDevice:
    def __init__(
        self,
        host: str = "192.0.2.10",
        energy: FakeEnergy | None = None,
        *,
        has_energy: bool = True,
        update_errors: list[BaseException] | None = None,
        model: str = "P110",
        alias: str = "Printer plug",
    ) -> None:
        self.host = host
        self.model = model
        self.alias = alias
        self.mac = "AA:BB:CC:DD:EE:FF"
        self.config = FakeConfig(host)
        self.energy = energy or FakeEnergy()
        self.modules: dict[str, Any] = {"Energy": self.energy} if has_energy else {}
        self.update_errors = list(update_errors or [])
        self.updates = 0
        self.disconnected = False
        self.switch_calls = 0

    async def update(self) -> None:
        self.updates += 1
        if self.update_errors:
            raise self.update_errors.pop(0)

    async def disconnect(self) -> None:
        self.disconnected = True

    # If the collector ever tried to switch a plug, these would record it.
    async def turn_on(self) -> None:
        self.switch_calls += 1

    async def turn_off(self) -> None:
        self.switch_calls += 1


@pytest.fixture
def make_settings(tmp_path: Path):
    def factory(**overrides: Any) -> Settings:
        raw: dict[str, Any] = {
            "collector_id": "test-collector",
            "polling_interval_seconds": 10,
            "devices": [
                {"id": "printer_01", "name": "3D Printer 1", "host": "192.0.2.10", "machine": "Prusa", "category": "3D printer"},
                {"id": "soldering_01", "name": "Soldering Station", "host": "192.0.2.11"},
            ],
            "upload": {"endpoint_url": ENDPOINT, "batch_size": 50, "min_retry_seconds": 1, "max_retry_seconds": 60},
            "storage": {"database_path": "data/collector.sqlite"},
            "logging": {"directory": "logs"},
        }
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(raw.get(key), dict):
                raw[key] = {**raw[key], **value}
            else:
                raw[key] = value
        return settings_from_dict(raw, tmp_path / "config.yaml")

    return factory


@pytest.fixture
def secrets() -> Secrets:
    return Secrets(tapo_username="tapo@example.test", tapo_password="tapo-password-123", api_token=TOKEN)


@pytest.fixture
def storage(tmp_path: Path):
    store = Storage(tmp_path / "queue.sqlite")
    yield store
    try:
        store.close()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ENERGY_API_URL", "TAPO_USERNAME", "TAPO_PASSWORD", "ENERGY_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
