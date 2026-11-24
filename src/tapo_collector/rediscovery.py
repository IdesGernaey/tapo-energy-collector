"""Finding a configured plug again after its DHCP address changed.

The collector never trusts an address on its own. Discovery only produces *candidates*: addresses whose discovery
reply carried the plug's MAC address (an unauthenticated hint). :class:`tapo_collector.devices.TapoPlug` then logs in
to each candidate and accepts it only when the MAC reported over the authenticated connection matches.

Sources of candidates, cheapest first:

* **Broadcast** (``rediscovery.broadcast_targets``): one UDP broadcast that every Tapo plug on the subnet answers.
  Managed networks such as UGentSurfing often drop broadcasts. That is detected and reported (``blocked`` when a plug
  known to be online did not answer, ``no_response`` when nothing answered at all).
* **Unicast scan** (``rediscovery.scan_networks``): the same UDP discovery request sent to every address of an
  approved range, for networks that block broadcast but route unicast. Off by default: ask the network
  administrator before enabling it.
* **Candidate hosts** (``rediscovery.candidate_hosts``): addresses or hostnames that are tried directly over TCP with
  a login, for networks where all UDP discovery is blocked (e.g. a small DHCP pool reserved for the plugs).

Several plugs that lose their connection at the same time share one scan (results are reused for a few seconds).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from tapo_collector.config import RediscoverySettings, normalize_mac
from tapo_collector.measurements import format_timestamp

log = logging.getLogger(__name__)

# probe(target, timeout_seconds) -> {ip: mac or ""} of every Tapo/Kasa device that answered.
Probe = Callable[[str, int], Awaitable[dict[str, str]]]


def mac_from_discovery(info: Any) -> str:
    """MAC address from a raw discovery reply (port 20002 ``result.mac`` or legacy port 9999 ``get_sysinfo``)."""
    if not isinstance(info, dict):
        return ""
    result = info.get("result")
    if isinstance(result, dict) and result.get("mac"):
        return normalize_mac(result.get("mac"))
    system = info.get("system")
    sysinfo = system.get("get_sysinfo") if isinstance(system, dict) else None
    if isinstance(sysinfo, dict):
        return normalize_mac(sysinfo.get("mac") or sysinfo.get("mic_mac") or "")
    return ""


async def kasa_probe(target: str, timeout: int) -> dict[str, str]:
    """Send python-kasa's UDP discovery request to ``target`` (broadcast or unicast) without logging in anywhere."""
    from kasa import Discover

    seen: dict[str, str] = {}

    def on_raw(data: Any) -> None:  # kasa.discover.DiscoveredRaw (a TypedDict)
        meta = data.get("meta") or {}
        ip = meta.get("ip")
        if ip:
            seen[str(ip)] = mac_from_discovery(data.get("discovery_response"))

    async def on_unsupported(_error: Exception) -> None:  # e.g. TPAP firmware: still seen through on_raw
        return None

    devices = await Discover.discover(target=target, discovery_timeout=timeout, on_discovered_raw=on_raw, on_unsupported=on_unsupported)
    for ip, device in devices.items():
        if not seen.get(ip):
            seen[ip] = normalize_mac(getattr(device, "mac", ""))
        with suppress(Exception):
            await asyncio.wait_for(device.disconnect(), 5)
    return seen


@dataclass(frozen=True)
class Sighting:
    host: str
    mac: str  # from the unauthenticated discovery reply: a hint, verified after login
    source: str  # broadcast | scan


@dataclass
class ScanResult:
    finished_at: float  # monotonic
    sightings: dict[str, Sighting] = field(default_factory=dict)
    broadcast: str = "disabled"  # disabled | ok | no_response | blocked | error
    detail: str = ""
    scanned_addresses: int = 0

    def hosts_for(self, mac: str) -> list[str]:
        return [s.host for s in self.sightings.values() if mac and s.mac == mac]

    def summary(self) -> str:
        parts = [f"broadcast {self.broadcast}" + (f" ({self.detail})" if self.detail else "")]
        if self.scanned_addresses:
            parts.append(f"unicast scan of {self.scanned_addresses} address(es)")
        parts.append(f"{len(self.sightings)} Tapo device(s) answered")
        return ", ".join(parts)


BLOCKED_HINT = (
    "Broadcast discovery does not work on this network (managed networks such as UGentSurfing usually drop it). "
    "Automatic recovery after an IP change then needs rediscovery.scan_networks or rediscovery.candidate_hosts in "
    "config.yaml - ask the network administrator which is allowed - or a DHCP reservation for each plug."
)


class Rediscovery:
    """Shared by all plugs: scans the network for Tapo devices and reports what it found (and whether it can work)."""

    def __init__(
        self,
        settings: RediscoverySettings,
        *,
        probe: Probe = kasa_probe,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.probe = probe
        self.clock = clock
        self.wall_clock = wall_clock
        self._lock = asyncio.Lock()
        self._last: ScanResult | None = None
        self._last_scan_wall: float | None = None
        self._reported_state: str | None = None
        self._reported_unknown: set[str] = set()
        self.scans = 0
        self.unknown_devices: dict[str, str] = {}  # mac -> host of Tapo devices that are not configured plugs
        # Set by the collector: host -> MAC of plugs that are connected right now, and MACs of configured plugs.
        self.online_plugs: Callable[[], dict[str, str]] = dict
        self.known_macs: Callable[[], set[str]] = set

    @property
    def has_alternative(self) -> bool:
        return bool(self.settings.scan_networks or self.settings.candidate_hosts)

    async def candidates(self, mac: str) -> tuple[list[str], str]:
        """Addresses whose discovery reply carried ``mac`` (to be verified by the caller) and a one-line summary."""
        result = await self.scan()
        return result.hosts_for(mac), result.summary()

    async def scan(self) -> ScanResult:
        async with self._lock:
            if self._last is not None and self.clock() - self._last.finished_at <= self.settings.result_cache_seconds:
                return self._last
            result = await self._scan()
            self._last = result
            self._last_scan_wall = self.wall_clock()
            self.scans += 1
            self._report(result)
            return result

    async def _scan(self) -> ScanResult:
        s = self.settings
        result = ScanResult(finished_at=0.0)
        if s.broadcast_targets:
            answered, errors = False, []
            for target in s.broadcast_targets:
                try:
                    found = await asyncio.wait_for(self.probe(target, s.discovery_timeout_seconds), s.discovery_timeout_seconds + 10)
                except Exception as error:  # OSError: no network / broadcast not permitted; TimeoutError
                    errors.append(f"{target}: {str(error) or type(error).__name__}")
                    continue
                answered = answered or bool(found)
                for host, mac in found.items():
                    result.sightings.setdefault(host, Sighting(host, mac, "broadcast"))
            online = self.online_plugs()
            if answered:
                missing = sorted(h for h in online if h not in result.sightings)
                result.broadcast = "ok"
                if missing:
                    result.detail = f"connected plug(s) at {', '.join(missing)} did not answer; broadcast may be partly filtered"
            elif errors and len(errors) == len(s.broadcast_targets):
                result.broadcast = "error"
                result.detail = "; ".join(errors) + " (is the network up?)"
            elif online:
                result.broadcast = "blocked"
                result.detail = f"no reply although plug(s) at {', '.join(sorted(online))} are connected"
            else:
                result.broadcast = "no_response"
                result.detail = "no Tapo device answered (broadcast blocked, network down, or no plug powered)"
        if s.scan_networks:
            semaphore = asyncio.Semaphore(s.scan_concurrency)
            addresses = [str(ip) for net in s.scan_networks for ip in ipaddress.ip_network(net, strict=False).hosts()]
            result.scanned_addresses = len(addresses)

            async def probe_one(ip: str) -> None:
                async with semaphore:
                    try:
                        found = await asyncio.wait_for(self.probe(ip, 1), 10)
                    except Exception:  # nothing at that address, or unreachable
                        return
                for host, mac in found.items():
                    result.sightings.setdefault(host, Sighting(host, mac, "scan"))

            await asyncio.gather(*(probe_one(ip) for ip in addresses))
        result.finished_at = self.clock()
        return result

    def _report(self, result: ScanResult) -> None:
        state = result.broadcast
        if state != self._reported_state:
            self._reported_state = state
            if state in ("blocked", "no_response"):
                level = logging.INFO if self.has_alternative else logging.WARNING
                log.log(
                    level, "Rediscovery: broadcast discovery %s: %s. %s", state.replace("_", " "), result.detail, "" if self.has_alternative else BLOCKED_HINT
                )
            elif state == "error":
                log.warning("Rediscovery: broadcast discovery failed: %s", result.detail)
            elif state == "ok":
                log.info("Rediscovery: broadcast discovery works (%d Tapo device(s) answered)", len(result.sightings))
        known = self.known_macs()
        for sighting in result.sightings.values():
            if sighting.mac and sighting.mac not in known:
                self.unknown_devices[sighting.mac] = sighting.host
                if sighting.mac not in self._reported_unknown:
                    self._reported_unknown.add(sighting.mac)
                    log.info("Rediscovery: ignoring unknown Tapo device %s at %s (not a configured plug)", sighting.mac, sighting.host)

    def status(self) -> dict[str, Any]:
        last = self._last
        return {
            "enabled": self.settings.enabled,
            "broadcast": last.broadcast if last else "not_tried",
            "detail": last.detail if last else "",
            "last_scan": format_timestamp(datetime.fromtimestamp(self._last_scan_wall, UTC)) if self._last_scan_wall else None,
            "scans": self.scans,
            "devices_seen": len(last.sightings) if last else 0,
            "unknown_devices": [{"mac": mac, "host": host} for mac, host in sorted(self.unknown_devices.items())],
            "scan_networks": list(self.settings.scan_networks),
            "candidate_hosts": list(self.settings.candidate_hosts),
        }
