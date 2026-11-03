from __future__ import annotations

import asyncio
from typing import Any

import kasa
import pytest

from conftest import FakeDevice, FakeEnergy
from tapo_collector.config import DeviceSettings
from tapo_collector.devices import DeviceReadError, TapoPlug, safe_connection_dict
from tapo_collector.discovery import discover_network, yaml_snippet
from tapo_collector.discovery import test_device as run_device_test
from tapo_collector.service import DevicePoller, device_retry_delay
from tapo_collector.storage import Storage

SETTINGS = DeviceSettings(id="printer_01", name="3D Printer 1", host="192.0.2.10")
CREDS = kasa.Credentials("tapo@example.test", "tapo-password-123")


class Kasa:
    """Replaces the network-facing python-kasa entry points for one test."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[str] = []
        self.connect_result: Any = None
        self.discover_result: Any = None
        self.try_all_result: Any = None
        self.network: dict[str, Any] = {}
        mp = monkeypatch

        async def connect(*, host: str | None = None, config: Any = None) -> Any:
            self.calls.append("connect")
            self.connect_config = config
            return self._resolve(self.connect_result)

        async def discover_single(host: str, **kwargs: Any) -> Any:
            self.calls.append("discover_single")
            return self._resolve(self.discover_result)

        async def try_connect_all(host: str, **kwargs: Any) -> Any:
            self.calls.append("try_connect_all")
            return self._resolve(self.try_all_result)

        async def discover(**kwargs: Any) -> Any:
            self.calls.append("discover")
            return self.network

        mp.setattr(kasa.Device, "connect", staticmethod(connect))
        mp.setattr(kasa.Discover, "discover_single", staticmethod(discover_single))
        mp.setattr(kasa.Discover, "try_connect_all", staticmethod(try_connect_all))
        mp.setattr(kasa.Discover, "discover", staticmethod(discover))

    @staticmethod
    def _resolve(value: Any) -> Any:
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, list):
            item = value.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return value


@pytest.fixture
def fake_kasa(monkeypatch: pytest.MonkeyPatch) -> Kasa:
    return Kasa(monkeypatch)


CACHED_CONFIG = kasa.DeviceConfig(
    host="192.0.2.10",
    connection_type=kasa.DeviceConnectionParameters(kasa.DeviceFamily.SmartTapoPlug, kasa.DeviceEncryptionType.Klap, login_version=2),
).to_dict()


async def test_first_connection_uses_discovery_and_caches_parameters_without_secrets(fake_kasa: Kasa, storage: Storage) -> None:
    device = FakeDevice()
    fake_kasa.discover_result = device
    plug = TapoPlug(SETTINGS, CREDS, storage)
    reading = await plug.read()
    assert fake_kasa.calls == ["discover_single"]
    assert reading.metrics["power_w"] == 145.7
    assert reading.timestamp.tzinfo is not None
    cached = storage.get_connection("printer_01", "192.0.2.10")
    assert cached is not None and "aes_keys" not in cached and "credentials" not in cached
    assert "tapo-password-123" not in str(cached)
    assert device.switch_calls == 0  # read-only


async def test_cached_parameters_avoid_udp_discovery(fake_kasa: Kasa, storage: Storage) -> None:
    storage.set_connection("printer_01", "192.0.2.10", CACHED_CONFIG)
    fake_kasa.connect_result = FakeDevice()
    plug = TapoPlug(SETTINGS, CREDS, storage)
    await plug.read()
    assert fake_kasa.calls == ["connect"]
    assert fake_kasa.connect_config.credentials == CREDS
    assert fake_kasa.connect_config.connection_type.encryption_type is kasa.DeviceEncryptionType.Klap


async def test_falls_back_to_tcp_when_udp_discovery_is_blocked(fake_kasa: Kasa, storage: Storage) -> None:
    fake_kasa.discover_result = kasa.exceptions.TimeoutError("Timed out getting discovery response")
    fake_kasa.try_all_result = FakeDevice()
    plug = TapoPlug(SETTINGS, CREDS, storage)
    reading = await plug.read()
    assert fake_kasa.calls == ["discover_single", "try_connect_all"]
    assert reading.metrics["energy_today_kwh"] == 0.82


async def test_stale_cache_falls_back_to_discovery(fake_kasa: Kasa, storage: Storage) -> None:
    storage.set_connection("printer_01", "192.0.2.10", CACHED_CONFIG)
    fake_kasa.connect_result = kasa.KasaException("protocol changed after firmware update")
    fake_kasa.discover_result = FakeDevice()
    await TapoPlug(SETTINGS, CREDS, storage).read()
    assert fake_kasa.calls == ["connect", "discover_single"]


async def test_authentication_failure_is_reported_as_auth(fake_kasa: Kasa, storage: Storage) -> None:
    fake_kasa.discover_result = kasa.AuthenticationError("bad credentials")
    with pytest.raises(DeviceReadError) as info:
        await TapoPlug(SETTINGS, CREDS, storage).read()
    assert info.value.kind == "auth"
    assert "TAPO_USERNAME" in str(info.value)
    assert "tapo-password-123" not in str(info.value)


async def test_unreachable_device(fake_kasa: Kasa, storage: Storage) -> None:
    fake_kasa.discover_result = kasa.exceptions.TimeoutError("no answer")
    fake_kasa.try_all_result = None
    with pytest.raises(DeviceReadError) as info:
        await TapoPlug(SETTINGS, CREDS, storage).read()
    assert info.value.kind == "unreachable"


async def test_device_without_energy_monitoring_is_unsupported(fake_kasa: Kasa, storage: Storage) -> None:
    device = FakeDevice(has_energy=False, model="P100")
    fake_kasa.discover_result = device
    with pytest.raises(DeviceReadError) as info:
        await TapoPlug(SETTINGS, CREDS, storage).read()
    assert info.value.kind == "unsupported" and "P110" in str(info.value)
    assert device.disconnected


async def test_reconnects_after_device_becomes_unreachable(fake_kasa: Kasa, storage: Storage) -> None:
    first = FakeDevice()
    second = FakeDevice()
    fake_kasa.discover_result = [first, second]
    plug = TapoPlug(SETTINGS, CREDS, storage)
    await plug.read()  # connects (discovery) and reads
    first.update_errors = [kasa.exceptions._ConnectionError("Wi-Fi dropped")]
    with pytest.raises(DeviceReadError) as info:
        await plug.read()
    assert info.value.kind == "unreachable"
    assert first.disconnected and not plug.connected
    # Next poll reconnects. The cache from the first connection is tried first, then discovery.
    fake_kasa.connect_result = second
    reading = await plug.read()
    assert reading.metrics["power_w"] == 145.7
    assert fake_kasa.calls[-1] == "connect"


async def test_slow_device_times_out(fake_kasa: Kasa, storage: Storage) -> None:
    class Slow(FakeDevice):
        async def update(self) -> None:
            await asyncio.sleep(5)

    plug = TapoPlug(SETTINGS, CREDS, storage, timeout=0.05)
    plug._device = Slow()
    with pytest.raises(DeviceReadError) as info:
        await plug.read()
    assert info.value.kind == "unreachable"


def test_safe_connection_dict_strips_credentials() -> None:
    assert safe_connection_dict(FakeDevice()) == {
        "host": "192.0.2.10",
        "timeout": 5,
        "connection_type": {"device_family": "SMART.TAPOPLUG", "encryption_type": "KLAP", "login_version": 2, "https": False},
    }


async def test_network_discovery_lists_devices_and_suggests_config(fake_kasa: Kasa) -> None:
    fake_kasa.network = {
        "192.0.2.21": FakeDevice("192.0.2.21", alias="Prusa MK4 #2"),
        "192.0.2.20": FakeDevice("192.0.2.20", alias="Soldeerstation"),
        "192.0.2.30": FakeDevice("192.0.2.30", has_energy=False, model="P100", alias="Lamp"),
    }
    found = await discover_network("255.255.255.255", CREDS)
    assert [d.host for d in found] == ["192.0.2.20", "192.0.2.21", "192.0.2.30"]
    assert [d.energy for d in found] == [True, True, False]
    snippet = yaml_snippet(found)
    assert "id: soldeerstation" in snippet and "id: prusa_mk4_2" in snippet
    assert "192.0.2.30" not in snippet  # P100 has no energy meter


async def test_test_device_command_reads_without_switching(fake_kasa: Kasa) -> None:
    device = FakeDevice(energy=FakeEnergy(power=3.5))
    fake_kasa.discover_result = device
    plug, results = await run_device_test(SETTINGS, CREDS, count=2, interval=0)
    assert [r.metrics["power_w"] for r in results] == [3.5, 3.5]  # type: ignore[union-attr]
    assert device.switch_calls == 0 and plug.model == "P110"


# ---------------------------------------------------------------- polling / failure isolation


class ScriptedPlug:
    def __init__(self, settings: DeviceSettings, results: list[Any]) -> None:
        self.settings = settings
        self.results = results

    async def read(self) -> Any:
        from tapo_collector.devices import Reading

        item = self.results.pop(0) if self.results else DeviceReadError("gone", "unreachable")
        if isinstance(item, Exception):
            raise item
        from datetime import UTC, datetime

        return Reading(timestamp=datetime.now(UTC), metrics={"power_w": item}, latency_seconds=0.01)

    async def close(self) -> None:
        pass


async def test_one_offline_plug_does_not_affect_the_others(storage: Storage) -> None:
    good = DevicePoller(ScriptedPlug(DeviceSettings("good", "Good", "192.0.2.1", polling_interval_seconds=2), [10.0, 11.0]), storage, "c")
    bad = DevicePoller(
        ScriptedPlug(DeviceSettings("bad", "Bad", "192.0.2.2", polling_interval_seconds=2), [DeviceReadError("timeout", "unreachable")] * 2),
        storage,
        "c",
    )
    assert await bad.poll_once() is False
    assert await good.poll_once() is True
    assert await bad.poll_once() is False
    assert await good.poll_once() is True
    rows = storage.next_batch(10)
    assert [(m.device_id, m.online, m.power_w) for m in rows if m.device_id == "good"] == [("good", True, 10.0), ("good", True, 11.0)]
    offline = [m for m in rows if m.device_id == "bad"]
    assert len(offline) == 2 and all(not m.online and m.power_w is None for m in offline)
    assert bad.state.consecutive_failures == 2 and bad.state.offline_since is not None
    assert good.state.consecutive_failures == 0


async def test_poller_recovers_and_resets_state(storage: Storage) -> None:
    poller = DevicePoller(ScriptedPlug(SETTINGS, [DeviceReadError("x", "unreachable"), 50.0]), storage, "c")
    await poller.poll_once()
    assert poller.state.offline_since is not None
    await poller.poll_once()
    assert poller.state.offline_since is None and poller.state.last_power_w == 50.0


async def test_unexpected_exception_does_not_kill_poller(storage: Storage) -> None:
    poller = DevicePoller(ScriptedPlug(SETTINGS, [RuntimeError("bug in library")]), storage, "c")
    assert await poller.poll_once() is False
    assert poller.state.last_error_kind == "error"


def test_device_retry_delay() -> None:
    assert device_retry_delay(10, 1) == 10
    assert device_retry_delay(10, 2) == 20
    assert device_retry_delay(10, 5) == 160
    assert device_retry_delay(10, 50) == 300
    assert device_retry_delay(600, 3) == 600  # never faster than the configured interval


async def test_polls_are_skipped_while_the_system_clock_is_unset(storage: Storage) -> None:
    plug = ScriptedPlug(SETTINGS, [42.0])
    poller = DevicePoller(plug, storage, "c", clock=lambda: 86400.0)  # 1970: Raspberry Pi before NTP sync
    assert await poller.poll_once() is False
    assert storage.stats().pending == 0 and plug.results == [42.0]  # plug not even read


async def test_tpap_firmware_is_reported_as_unsupported_not_unreachable(fake_kasa: Kasa, storage: Storage) -> None:
    fake_kasa.discover_result = kasa.UnsupportedDeviceError(
        "Unsupported device 192.0.2.10 of type SMART.TAPOPLUG with encrypt_scheme EncryptionScheme(encrypt_type='TPAP', lv=2)"
    )
    with pytest.raises(DeviceReadError) as info:
        await TapoPlug(SETTINGS, CREDS, storage).read()
    assert info.value.kind == "unsupported"
    assert "TPAP" in str(info.value) and "issues/1590" in str(info.value)
    assert fake_kasa.calls == ["discover_single"]  # no pointless protocol probing
