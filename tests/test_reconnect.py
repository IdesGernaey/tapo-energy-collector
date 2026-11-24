"""Automatic recovery when a plug disappears, comes back at another DHCP address, or the network fails.

A simulated workshop network replaces python-kasa's network entry points: plugs can be unplugged, moved to another
address, swapped, replaced by an unknown device, and Wi-Fi / broadcast / the website can be switched off.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from typing import Any

import httpx
import kasa
import pytest
import respx

from conftest import ENDPOINT, TOKEN, FakeConfig, FakeEnergy
from tapo_collector.config import ConfigError, DeviceSettings, RediscoverySettings, normalize_mac
from tapo_collector.devices import DeviceReadError, TapoPlug
from tapo_collector.rediscovery import Rediscovery, kasa_probe, mac_from_discovery
from tapo_collector.service import Collector, DevicePoller
from tapo_collector.storage import Storage
from tapo_collector.uploader import ApiClient
from test_service import FakeWebsite

CREDS = kasa.Credentials("tapo@example.test", "tapo-password-123")
MAC_HOT = "AA:AA:AA:00:00:01"
MAC_SOLDER = "AA:AA:AA:00:00:02"
MAC_STRANGER = "BB:BB:BB:00:00:99"
HOT = DeviceSettings(id="hotplate_01", name="Hotplate", host="192.0.2.10")
SOLDER = DeviceSettings(id="soldering_01", name="Soldering station", host="192.0.2.11")


class PhysicalPlug:
    """A real P110 in the workshop: identified by its MAC, with its own power reading."""

    def __init__(self, mac: str, power: float, *, account_ok: bool = True) -> None:
        self.mac = mac
        self.power = power
        self.account_ok = account_ok


class Session:
    """What python-kasa returns after connecting to an address: talks to whatever device currently has that IP."""

    def __init__(self, net: Network, host: str) -> None:
        self.net, self.host = net, host
        self.config = FakeConfig(host)
        self.model, self.alias = "P110", "plug"
        self.disconnected = False
        self.switch_calls = 0
        self._sync()

    def _sync(self) -> None:
        plug = self.net.at(self.host)
        self.mac = plug.mac
        self.modules = {"Energy": FakeEnergy(power=plug.power)}

    async def update(self) -> None:
        self._sync()

    async def disconnect(self) -> None:
        self.disconnected = True


class Network:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.devices: dict[str, PhysicalPlug] = {}
        self.wifi_up = True
        self.broadcast_blocked = False
        self.udp_blocked = False  # unicast discovery blocked too
        self.logins: Counter[str] = Counter()
        self.probes: list[str] = []

        def connect_to(host: str) -> Session:
            self.logins[host] += 1
            plug = self.at(host)
            if not plug.account_ok:
                raise kasa.AuthenticationError("login refused")
            return Session(self, host)

        async def device_connect(*, host: str | None = None, config: Any = None) -> Any:
            return connect_to(config.host)

        async def discover_single(host: str, **kwargs: Any) -> Any:
            if self.udp_blocked or not self.wifi_up:
                raise kasa.exceptions.TimeoutError("Timed out getting discovery response")
            return connect_to(host)

        async def try_connect_all(host: str, **kwargs: Any) -> Any:
            try:
                return connect_to(host)
            except kasa.exceptions._ConnectionError:
                return None

        monkeypatch.setattr(kasa.Device, "connect", staticmethod(device_connect))
        monkeypatch.setattr(kasa.Discover, "discover_single", staticmethod(discover_single))
        monkeypatch.setattr(kasa.Discover, "try_connect_all", staticmethod(try_connect_all))

    def at(self, host: str) -> PhysicalPlug:
        if not self.wifi_up or host not in self.devices:
            raise kasa.exceptions._ConnectionError(f"Unable to connect to the device: {host}")
        return self.devices[host]

    async def probe(self, target: str, timeout: int) -> dict[str, str]:
        """Stands in for kasa_probe (UDP discovery request, no login)."""
        self.probes.append(target)
        if not self.wifi_up:
            raise OSError(101, "Network is unreachable")
        if target.endswith(".255"):
            return {} if self.broadcast_blocked else {h: p.mac for h, p in self.devices.items()}
        if self.udp_blocked or target not in self.devices:
            return {}
        return {target: self.devices[target].mac}


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> Network:
    return Network(monkeypatch)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_locator(net: Network, clock: Clock, **overrides: Any) -> Rediscovery:
    settings = RediscoverySettings(
        **{"broadcast_targets": ("192.0.2.255",), "min_interval_seconds": 30, "max_interval_seconds": 120, "result_cache_seconds": 0, **overrides}
    )
    return Rediscovery(settings, probe=net.probe, clock=clock)


def make_plug(settings: DeviceSettings, storage: Storage, locator: Rediscovery, clock: Clock) -> TapoPlug:
    return TapoPlug(settings, CREDS, storage, timeout=1, locator=locator, clock=clock)


async def failing_reads(plug: TapoPlug, count: int) -> list[DeviceReadError]:
    errors = []
    for _ in range(count):
        with pytest.raises(DeviceReadError) as info:
            await plug.read()
        errors.append(info.value)
    return errors


# ---------------------------------------------------------------- scenarios


async def test_plug_unplugged_and_reconnected_with_same_ip(net: Network, storage: Storage) -> None:
    clock = Clock()
    net.devices["192.0.2.10"] = PhysicalPlug(MAC_HOT, 100)
    locator = make_locator(net, clock)
    plug = make_plug(HOT, storage, locator, clock)
    assert (await plug.read()).metrics["power_w"] == 100
    assert plug.expected_mac == MAC_HOT and plug.identity_source == "learned"

    unplugged = net.devices.pop("192.0.2.10")
    errors = await failing_reads(plug, 2)
    assert all(e.kind == "unreachable" for e in errors)
    assert net.probes == []  # fewer failures than the rediscovery threshold: only the last address is retried

    net.devices["192.0.2.10"] = unplugged
    assert (await plug.read()).metrics["power_w"] == 100
    assert plug.host == "192.0.2.10" and plug.connect_failures == 0
    assert net.probes == []


async def test_plug_reconnected_with_different_ip(net: Network, storage: Storage, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    clock = Clock()
    net.devices["192.0.2.10"] = PhysicalPlug(MAC_HOT, 100)
    locator = make_locator(net, clock)
    plug = make_plug(HOT, storage, locator, clock)
    await plug.read()

    net.devices["192.0.2.57"] = net.devices.pop("192.0.2.10")  # moved to another outlet, new DHCP lease
    await failing_reads(plug, 2)
    reading = await plug.read()  # third failure triggers rediscovery, which finds and verifies it
    assert reading.metrics["power_w"] == 100
    assert plug.host == "192.0.2.57"
    assert storage.get_last_host("hotplate_01") == "192.0.2.57"  # persisted for the next start
    assert plug.connection_status()["last_host_change"]["from"] == "192.0.2.10"
    for event in ("rediscovery started", "IP address changed from 192.0.2.10 to 192.0.2.57", "identity verified", "reconnected after rediscovery"):
        assert event in caplog.text
    assert "tapo-password-123" not in caplog.text  # credentials never logged

    # Polling continues at the new address without another scan.
    probes = len(net.probes)
    await plug.read()
    assert len(net.probes) == probes


async def test_wifi_temporarily_unavailable(net: Network, storage: Storage) -> None:
    clock = Clock()
    net.devices["192.0.2.10"] = PhysicalPlug(MAC_HOT, 100)
    locator = make_locator(net, clock, failures_before_rediscovery=2)
    plug = make_plug(HOT, storage, locator, clock)
    wall = iter(range(1_800_000_000, 1_800_100_000, 10))  # one poll every 10 s
    poller = DevicePoller(plug, storage, "c", clock=lambda: float(next(wall)))
    assert await poller.poll_once()

    net.wifi_up = False
    assert not await poller.poll_once()
    assert not await poller.poll_once()  # rediscovery runs, fails: the network itself is down
    assert locator.status()["broadcast"] == "error"
    assert plug.rediscovery_state == "waiting"
    first_wait = plug.next_rediscovery_at - clock.now
    assert first_wait == 30

    scans = locator.scans
    assert not await poller.poll_once()  # inside the backoff window: only the last address is retried
    assert locator.scans == scans

    clock.now += 31
    assert not await poller.poll_once()  # second rediscovery fails: backoff doubles
    assert plug.next_rediscovery_at - clock.now == 60

    net.wifi_up = True
    assert await poller.poll_once()  # back at the same address, no manual action
    assert plug.host == "192.0.2.10"
    rows = storage.next_batch(100)
    offline = [m for m in rows if not m.online]
    assert len(offline) == 4 and all(m.power_w is None and m.energy_today_kwh is None for m in offline)  # nothing invented
    assert sum(m.online for m in rows) == 2


@respx.mock
async def test_website_unavailable_while_local_polling_continues(net: Network, make_settings, secrets) -> None:
    settings = make_settings(
        devices=[{"id": "hotplate_01", "name": "Hotplate", "host": "192.0.2.10", "mac": MAC_HOT}],
        polling_interval_seconds=2,
        upload={"min_retry_seconds": 0.2, "max_retry_seconds": 1, "idle_seconds": 0.1},
        rediscovery={"broadcast_targets": ["192.0.2.255"]},
    )
    net.devices["192.0.2.10"] = PhysicalPlug(MAC_HOT, 100)
    website = FakeWebsite()
    website.up = False
    respx.post(ENDPOINT).mock(side_effect=website)
    storage = Storage(settings.storage.database_path)
    collector = Collector(
        settings, secrets, storage=storage, client=ApiClient(settings.upload, TOKEN), rediscovery=Rediscovery(settings.rediscovery, probe=net.probe)
    )
    stop = asyncio.Event()
    task = asyncio.create_task(collector.run(stop))
    await asyncio.sleep(2.3)  # two polls while the website is down
    assert website.requests > 0 and website.stored == {}
    assert collector.storage.stats().pending >= 2
    website.up = True
    await asyncio.sleep(1.0)
    stop.set()
    await asyncio.wait_for(task, 20)
    check = Storage(settings.storage.database_path)
    stats = check.stats()
    check.close()
    assert stats.pending == 0 and stats.delivered == len(website.stored) >= 2
    assert all(m["online"] and m["power_w"] == 100 for m in website.stored.values())


async def test_two_devices_swap_ip_addresses(net: Network, storage: Storage) -> None:
    clock = Clock()
    net.devices["192.0.2.10"] = PhysicalPlug(MAC_HOT, 100)
    net.devices["192.0.2.11"] = PhysicalPlug(MAC_SOLDER, 25)
    locator = make_locator(net, clock)
    hot = make_plug(HOT, storage, locator, clock)
    solder = make_plug(SOLDER, storage, locator, clock)
    assert (await hot.read()).metrics["power_w"] == 100
    assert (await solder.read()).metrics["power_w"] == 25

    net.devices["192.0.2.10"], net.devices["192.0.2.11"] = net.devices["192.0.2.11"], net.devices["192.0.2.10"]
    # The open sessions now talk to the other plug: refused, never stored under the wrong ID.
    assert (await failing_reads(hot, 1))[0].kind == "identity"
    assert (await failing_reads(solder, 1))[0].kind == "identity"
    # Identity mismatch -> immediate rediscovery (no waiting for repeated failures).
    assert (await hot.read()).metrics["power_w"] == 100
    assert (await solder.read()).metrics["power_w"] == 25
    assert (hot.host, solder.host) == ("192.0.2.11", "192.0.2.10")
    assert (storage.get_last_host("hotplate_01"), storage.get_last_host("soldering_01")) == ("192.0.2.11", "192.0.2.10")


async def test_unknown_device_at_old_address_is_never_used(net: Network, storage: Storage, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    clock = Clock()
    net.devices["192.0.2.10"] = PhysicalPlug(MAC_HOT, 100)
    locator = make_locator(net, clock)
    locator.known_macs = lambda: {MAC_HOT}
    plug = make_plug(HOT, storage, locator, clock)
    await plug.read()

    del net.devices["192.0.2.10"]  # hotplate unplugged ...
    net.devices["192.0.2.10"] = PhysicalPlug(MAC_STRANGER, 3000)  # ... and someone else's P110 gets its address
    for _ in range(3):
        with pytest.raises(DeviceReadError) as info:
            await plug.read()
        assert info.value.kind == "identity"
        clock.now += 1000  # past any rediscovery backoff
    assert plug.host == "192.0.2.10" and not plug.connected
    assert locator.status()["unknown_devices"] == [{"mac": MAC_STRANGER, "host": "192.0.2.10"}]
    assert "ignoring unknown Tapo device" in caplog.text
    assert storage.get_identity("hotplate_01") == (MAC_HOT, "learned")  # identity not overwritten


async def test_learning_refuses_a_mac_owned_by_another_plug(net: Network, storage: Storage) -> None:
    storage.set_identity("soldering_01", MAC_SOLDER, "config")
    net.devices["192.0.2.10"] = PhysicalPlug(MAC_SOLDER, 25)  # config says hotplate is at .10, but it is the soldering plug
    plug = TapoPlug(HOT, CREDS, storage, timeout=1)
    with pytest.raises(DeviceReadError) as info:
        await plug.read()
    assert info.value.kind == "identity" and "soldering_01" in str(info.value)
    assert plug.expected_mac == "" and storage.get_identity("hotplate_01") is None


async def test_configured_mac_is_enforced_from_the_first_connection(net: Network, storage: Storage) -> None:
    net.devices["192.0.2.10"] = PhysicalPlug(MAC_STRANGER, 3000)
    plug = TapoPlug(DeviceSettings(id="hotplate_01", name="Hotplate", host="192.0.2.10", mac=MAC_HOT), CREDS, storage, timeout=1)
    with pytest.raises(DeviceReadError) as info:
        await plug.read()
    assert info.value.kind == "identity"


async def test_blocked_broadcast_is_detected_and_reported(net: Network, storage: Storage, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    clock = Clock()
    net.devices["192.0.2.10"] = PhysicalPlug(MAC_HOT, 100)
    net.devices["192.0.2.11"] = PhysicalPlug(MAC_SOLDER, 25)
    net.broadcast_blocked = True
    locator = make_locator(net, clock, failures_before_rediscovery=1)
    hot = make_plug(HOT, storage, locator, clock)
    solder = make_plug(SOLDER, storage, locator, clock)
    locator.online_plugs = lambda: {p.host: p.expected_mac for p in (hot, solder) if p.connected}
    await hot.read()
    await solder.read()

    net.devices["192.0.2.60"] = net.devices.pop("192.0.2.10")
    errors = await failing_reads(hot, 2)  # open connection breaks, then the reconnect triggers rediscovery
    assert "rediscovery did not find the plug" in str(errors[-1])
    status = locator.status()
    assert status["broadcast"] == "blocked" and "192.0.2.11" in status["detail"]
    assert "UGentSurfing" in caplog.text and "scan_networks" in caplog.text
    assert "not found" in hot.connection_status()["rediscovery"]["last_result"]
    assert (await solder.read()).metrics["power_w"] == 25  # the other plug is unaffected


async def test_discovery_timeout_is_reported_as_error(storage: Storage) -> None:
    async def timing_out(target: str, timeout: int) -> dict[str, str]:
        raise TimeoutError

    locator = Rediscovery(RediscoverySettings(result_cache_seconds=0), probe=timing_out)
    hosts, summary = await locator.candidates(MAC_HOT)
    assert hosts == [] and "broadcast error" in summary
    assert locator.status()["broadcast"] == "error"


async def test_unicast_scan_finds_plug_when_broadcast_is_blocked(net: Network, storage: Storage) -> None:
    clock = Clock()
    net.devices["192.0.2.10"] = PhysicalPlug(MAC_HOT, 100)
    net.broadcast_blocked = True
    locator = make_locator(net, clock, failures_before_rediscovery=1, scan_networks=("192.0.2.0/26",))
    plug = make_plug(HOT, storage, locator, clock)
    await plug.read()
    net.devices["192.0.2.42"] = net.devices.pop("192.0.2.10")
    await failing_reads(plug, 1)
    assert (await plug.read()).metrics["power_w"] == 100
    assert plug.host == "192.0.2.42"
    assert sum(1 for p in net.probes if not p.endswith(".255")) == 62  # every usable address of the /26 probed once
    # Only the matching address was logged in to: no login attempts on unrelated devices.
    assert set(net.logins) == {"192.0.2.10", "192.0.2.42"}


async def test_candidate_hosts_work_when_all_udp_discovery_is_blocked(net: Network, storage: Storage) -> None:
    clock = Clock()
    net.devices["192.0.2.10"] = PhysicalPlug(MAC_HOT, 100)
    net.broadcast_blocked = net.udp_blocked = True
    locator = make_locator(net, clock, failures_before_rediscovery=1, candidate_hosts=("192.0.2.200", "192.0.2.201"))
    plug = make_plug(HOT, storage, locator, clock)
    await plug.read()
    net.devices["192.0.2.200"] = PhysicalPlug(MAC_STRANGER, 3000)  # verified and rejected
    net.devices["192.0.2.201"] = net.devices.pop("192.0.2.10")
    await failing_reads(plug, 1)
    assert (await plug.read()).metrics["power_w"] == 100
    assert plug.host == "192.0.2.201"


async def test_collector_restart_after_outage_uses_last_verified_address(net: Network, make_settings, secrets) -> None:
    settings = make_settings(
        devices=[{"id": "hotplate_01", "name": "Hotplate", "host": "192.0.2.10"}],
        rediscovery={"broadcast_targets": ["192.0.2.255"], "failures_before_rediscovery": 1},
    )
    net.devices["192.0.2.10"] = PhysicalPlug(MAC_HOT, 100)

    first = Collector(settings, secrets, rediscovery=Rediscovery(settings.rediscovery, probe=net.probe))
    poller = first.pollers[0]
    assert await poller.poll_once()
    net.devices["192.0.2.80"] = net.devices.pop("192.0.2.10")
    assert not await poller.poll_once()  # connection lost
    assert await poller.poll_once()  # rediscovered and verified at .80
    net.wifi_up = False
    assert not await poller.poll_once()  # outage begins, then the collector is restarted (e.g. reboot)
    pending = first.storage.stats().pending
    await first.close()

    net.wifi_up = True
    net.probes.clear()
    second = Collector(settings, secrets, rediscovery=Rediscovery(settings.rediscovery, probe=net.probe))
    plug = second.pollers[0].plug
    assert isinstance(plug, TapoPlug)
    assert plug.host == "192.0.2.80" and plug.expected_mac == MAC_HOT  # remembered across the restart
    assert second.storage.stats().pending == pending  # queued readings survived
    assert await second.pollers[0].poll_once()
    assert net.probes == []  # no rediscovery needed after the restart
    status = second.status()
    assert status["devices"][0]["host"] == "192.0.2.80"
    assert status["devices"][0]["connection"]["mac"] == MAC_HOT
    assert status["rediscovery"]["broadcast"] == "not_tried"
    await second.close()


async def test_rediscovery_scans_are_shared_between_plugs(net: Network, storage: Storage) -> None:
    clock = Clock()
    locator = make_locator(net, clock, result_cache_seconds=15)
    net.devices["192.0.2.30"] = PhysicalPlug(MAC_HOT, 1)
    net.devices["192.0.2.31"] = PhysicalPlug(MAC_SOLDER, 2)
    first, second = await asyncio.gather(locator.candidates(MAC_HOT), locator.candidates(MAC_SOLDER))
    assert first[0] == ["192.0.2.30"] and second[0] == ["192.0.2.31"]
    assert locator.scans == 1 and net.probes == ["192.0.2.255"]


async def test_plug_without_known_identity_does_not_rediscover(net: Network, storage: Storage, caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock()
    locator = make_locator(net, clock, failures_before_rediscovery=1)
    plug = make_plug(HOT, storage, locator, clock)  # never connected, no mac configured
    await failing_reads(plug, 2)
    assert net.probes == [] and plug.rediscovery_state == "unavailable"
    assert caplog.text.count("MAC address is unknown") == 1


# ---------------------------------------------------------------- probe and configuration


async def test_kasa_probe_reads_mac_from_discovery_reply_without_login(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def discover(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        kwargs["on_discovered_raw"]({"discovery_response": {"result": {"mac": "aa-aa-aa-00-00-01"}}, "meta": {"ip": "192.0.2.10", "port": 20002}})
        kwargs["on_discovered_raw"](
            {"discovery_response": {"system": {"get_sysinfo": {"mic_mac": "BBBBBB000099"}}}, "meta": {"ip": "192.0.2.12", "port": 9999}}
        )
        return {}

    monkeypatch.setattr(kasa.Discover, "discover", staticmethod(discover))
    assert await kasa_probe("192.0.2.255", 3) == {"192.0.2.10": MAC_HOT, "192.0.2.12": MAC_STRANGER}
    assert "credentials" not in calls[0] and calls[0]["target"] == "192.0.2.255"


def test_mac_helpers() -> None:
    assert normalize_mac("a8-42-a1-01-02-03") == "A8:42:A1:01:02:03"
    assert normalize_mac("A842A1010203") == "A8:42:A1:01:02:03"
    assert normalize_mac("not a mac") == "" and normalize_mac(None) == ""
    assert mac_from_discovery({"result": {}}) == "" and mac_from_discovery("junk") == ""


def test_config_mac_and_rediscovery_validation(make_settings) -> None:
    settings = make_settings(
        devices=[{"id": "hotplate_01", "name": "Hotplate", "host": "192.0.2.10", "mac": "aa-aa-aa-00-00-01"}],
        rediscovery={"scan_networks": ["10.10.4.0/24"], "candidate_hosts": ["tapo-hotplate.local"]},
    )
    assert settings.devices[0].mac == MAC_HOT
    assert settings.rediscovery.scan_networks == ("10.10.4.0/24",) and settings.rediscovery.enabled
    assert make_settings().rediscovery.broadcast_targets == ("255.255.255.255",)  # default when the section is absent
    with pytest.raises(ConfigError, match="already used"):
        make_settings(devices=[{"id": "a", "host": "192.0.2.1", "mac": MAC_HOT}, {"id": "b", "host": "192.0.2.2", "mac": MAC_HOT}])
    with pytest.raises(ConfigError, match="not a MAC"):
        make_settings(devices=[{"id": "a", "host": "192.0.2.1", "mac": "12:34"}])
    with pytest.raises(ConfigError, match="at most"):
        make_settings(rediscovery={"scan_networks": ["10.0.0.0/16"]})
    with pytest.raises(ConfigError, match="IPv4"):
        make_settings(rediscovery={"broadcast_targets": ["everyone"]})


async def test_configured_mac_overrides_a_conflicting_learned_identity(make_settings, secrets) -> None:
    settings = make_settings(devices=[{"id": "hotplate_01", "host": "192.0.2.10", "mac": MAC_HOT}, {"id": "soldering_01", "host": "192.0.2.11"}])
    store = Storage(settings.storage.database_path)
    store.set_identity("soldering_01", MAC_HOT, "learned")  # wrongly learned earlier
    store.set_identity("removed_plug", MAC_STRANGER, "learned")
    store.close()
    collector = Collector(settings, secrets)
    assert collector.storage.get_identity("hotplate_01") == (MAC_HOT, "config")
    assert collector.storage.get_identity("soldering_01") is None
    assert collector.storage.identity_owner(MAC_STRANGER) is None
    await collector.close()


def test_website_fake_is_reused() -> None:  # guard: the shared fake keeps its idempotent behaviour
    site = FakeWebsite()
    request = httpx.Request("POST", ENDPOINT, headers={"Authorization": f"Bearer {TOKEN}"}, json={"measurements": [{"measurement_id": "x"}]})
    assert site(request).json()["accepted"] == ["x"] and site(request).json()["duplicates"] == ["x"]
