"""Finding plugs on the network and testing a single plug.

Broadcast discovery (``Discover.discover``) only works when the workshop computer and the plugs
are on the same subnet and UDP broadcast is allowed - often *not* the case on a university network.
Then find the plug's IP address in the Tapo app (Device > Settings > Device info) or the router's
DHCP table instead, reserve it, and use ``test-device --host``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

from tapo_collector.config import DeviceSettings, normalize_mac
from tapo_collector.devices import DeviceReadError, Reading, TapoPlug

log = logging.getLogger(__name__)


@dataclass
class FoundDevice:
    host: str
    model: str = "?"
    alias: str = ""
    mac: str = ""
    energy: bool | None = None
    error: str = ""

    def suggested_id(self, index: int) -> str:
        base = re.sub(r"[^a-z0-9]+", "_", (self.alias or self.model or "plug").lower()).strip("_")[:50]
        return base or f"plug_{index:02d}"


async def discover_network(target: str, credentials: Any, timeout: int = 5) -> list[FoundDevice]:
    from kasa import Discover, Module

    found: dict[str, FoundDevice] = {}

    async def unsupported(error: Exception) -> None:
        host = getattr(error, "host", None) or "?"
        found[str(host)] = FoundDevice(host=str(host), error=f"unsupported: {error}")

    devices = await Discover.discover(target=target, credentials=credentials, discovery_timeout=timeout, on_unsupported=unsupported)
    for host, device in devices.items():
        item = FoundDevice(host=host, model=getattr(device, "model", "?"))
        try:
            await asyncio.wait_for(device.update(), 10)
            item.alias = device.alias or ""
            item.mac = normalize_mac(device.mac) or (device.mac or "")
            item.energy = Module.Energy in device.modules
        except Exception as error:
            item.error = f"{type(error).__name__}: {error}"
        finally:
            try:
                await device.disconnect()
            except Exception as error:
                log.debug("Ignoring disconnect error for %s: %s", host, error)
        found[host] = item
    return sorted(found.values(), key=lambda d: tuple(int(p) if p.isdigit() else 0 for p in d.host.split(".")))


def yaml_snippet(devices: list[FoundDevice]) -> str:
    lines = ["devices:"]
    for index, device in enumerate(devices, start=1):
        if device.energy is False or device.error:
            continue
        name = (device.alias or f"{device.model} {index}").replace('"', "'")
        lines += [
            f"  - id: {device.suggested_id(index)}",
            f'    name: "{name}"',
            f'    host: "{device.host}"   # initial address; the collector follows the plug if it changes',
            *([f'    mac: "{device.mac}"   # permanent identity of this physical plug'] if device.mac else []),
            '    machine: ""',
            '    category: ""',
        ]
    return "\n".join(lines)


class _MemoryCache:
    """In-memory stand-in for :class:`tapo_collector.storage.Storage` (one-off tests must not touch the service database)."""

    def __init__(self) -> None:
        self.data: dict[str, tuple[str, dict[str, Any]]] = {}
        self.identities: dict[str, tuple[str, str]] = {}

    def get_connection(self, device_id: str, host: str) -> dict[str, Any] | None:
        item = self.data.get(device_id)
        return item[1] if item else None

    def set_connection(self, device_id: str, host: str, config: dict[str, Any]) -> None:
        self.data[device_id] = (host, config)

    def forget_connection(self, device_id: str) -> None:
        self.data.pop(device_id, None)

    def get_last_host(self, device_id: str) -> str | None:
        item = self.data.get(device_id)
        return item[0] if item else None

    def get_identity(self, device_id: str) -> tuple[str, str] | None:
        return self.identities.get(device_id)

    def set_identity(self, device_id: str, mac: str, source: str) -> None:
        self.identities[device_id] = (mac, source)

    def identity_owner(self, mac: str) -> str | None:
        return next((d for d, (m, _) in self.identities.items() if m == mac), None)


async def test_device(
    settings: DeviceSettings, credentials: Any, *, count: int = 3, interval: float = 2.0, timeout: float = 10.0, cache: Any = None
) -> tuple[TapoPlug, list[Reading | DeviceReadError]]:
    """Connect to one plug and read it ``count`` times. Read-only: the plug is never switched."""
    plug = TapoPlug(settings, credentials, cache or _MemoryCache(), timeout=timeout)
    results: list[Reading | DeviceReadError] = []
    try:
        for index in range(count):
            try:
                results.append(await plug.read())
            except DeviceReadError as error:
                results.append(error)
                if error.kind in ("auth", "unsupported"):
                    break
            if index + 1 < count:
                await asyncio.sleep(interval)
    finally:
        await plug.close()
    return plug, results
