"""Connecting to and reading one Tapo P110 with python-kasa.

Connection strategy (designed for networks where UDP broadcast/discovery may be blocked):

1. Reuse cached connection parameters (``DeviceConfig.to_dict()`` without credentials) with
   ``Device.connect()`` - direct TCP/HTTP, no UDP needed.
2. ``Discover.discover_single(host)`` - unicast UDP discovery to that address (port 20002).
3. ``Discover.try_connect_all(host)`` - tries every known protocol over TCP.

After a successful connection the parameters are cached in SQLite for the next start.
The collector only calls ``update()`` and reads properties; it never switches a plug.

Identity and address changes:

* A plug's permanent identity is its MAC address (``mac:`` in the configuration, or learned from the plug's
  authenticated device info on the first verified connection and stored in SQLite).
* Every connection and every read checks that MAC. A different plug that inherited the old IP address is never
  read under this plug's ID (``DeviceReadError`` kind ``identity``).
* After ``rediscovery.failures_before_rediscovery`` failed connection attempts (immediately on an identity
  mismatch) the plug asks :class:`tapo_collector.rediscovery.Rediscovery` for candidate addresses, verifies each
  one, and continues at the new address. The last verified address is stored and used after a restart.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from tapo_collector.config import DeviceSettings, RediscoverySettings, normalize_mac
from tapo_collector.measurements import format_timestamp, metrics_from_energy_module

log = logging.getLogger(__name__)

# Keys of DeviceConfig.to_dict() that must never be written to disk.
_SECRET_CONFIG_KEYS = {"credentials", "credentials_hash", "aes_keys", "http_client"}


class DeviceReadError(Exception):
    """A plug could not be read. ``kind`` is one of: unreachable, auth, unsupported, identity, error."""

    def __init__(self, message: str, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind


class ConnectionCache(Protocol):
    def get_connection(self, device_id: str, host: str) -> dict[str, Any] | None: ...
    def set_connection(self, device_id: str, host: str, config: dict[str, Any]) -> None: ...
    def forget_connection(self, device_id: str) -> None: ...
    def get_last_host(self, device_id: str) -> str | None: ...
    def get_identity(self, device_id: str) -> tuple[str, str] | None: ...
    def set_identity(self, device_id: str, mac: str, source: str) -> None: ...
    def identity_owner(self, mac: str) -> str | None: ...


class Locator(Protocol):
    settings: RediscoverySettings

    async def candidates(self, mac: str) -> tuple[list[str], str]: ...


@dataclass(frozen=True)
class Reading:
    timestamp: datetime
    metrics: dict[str, float | None]
    latency_seconds: float


def kasa_credentials(username: str, password: str) -> Any:
    from kasa import Credentials

    return Credentials(username, password)


def safe_connection_dict(device: Any) -> dict[str, Any]:
    """Connection parameters of a connected device, with all credential material removed."""
    config = device.config.to_dict_control_credentials(credentials_hash="")
    return {k: v for k, v in config.items() if k not in _SECRET_CONFIG_KEYS}


def classify_error(error: BaseException) -> str:
    from kasa import AuthenticationError, KasaException, UnsupportedDeviceError
    from kasa.exceptions import TimeoutError as KasaTimeoutError
    from kasa.exceptions import _ConnectionError

    if isinstance(error, AuthenticationError):
        return "auth"
    if isinstance(error, UnsupportedDeviceError):
        return "unsupported"
    if isinstance(error, (asyncio.TimeoutError, KasaTimeoutError, _ConnectionError, ConnectionError, OSError)):
        return "unreachable"
    if isinstance(error, KasaException):
        return "error"
    return "error"


DeviceFactory = Callable[[], Awaitable[Any]]


def _iso(value: float | None) -> str | None:
    return format_timestamp(datetime.fromtimestamp(value, UTC)) if value is not None else None


class TapoPlug:
    """One configured plug. Keeps the python-kasa connection open between polls, reconnects on failure and follows
    the plug (verified by MAC address) when its IP address changes."""

    def __init__(
        self,
        settings: DeviceSettings,
        credentials: Any,
        cache: ConnectionCache,
        *,
        timeout: float = 10.0,
        locator: Locator | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.credentials = credentials
        self.cache = cache
        self.timeout = timeout
        self.locator = locator
        self.clock = clock
        self.wall_clock = wall_clock
        self._device: Any | None = None
        self.model: str | None = None
        self.alias: str | None = None
        self.host = self._initial_host()
        self.expected_mac, self.identity_source = self._initial_identity()
        self.connect_failures = 0
        self.rediscovery_state = "idle"  # idle | waiting | unavailable
        self.rediscovery_attempts = 0
        self.rediscovery_failures = 0
        self.next_rediscovery_at = 0.0
        self.last_rediscovery_at: float | None = None
        self.last_rediscovery_result = ""
        self.last_host_change: dict[str, Any] | None = None
        self._reported_no_identity = False

    # ---------------------------------------------------------------- identity and address

    def _initial_host(self) -> str:
        try:
            last = self.cache.get_last_host(self.settings.id)
        except Exception as error:
            log.debug("Device %s: no stored address (%s)", self.settings.id, error)
            last = None
        if last and last != self.settings.host:
            log.info("Device %s: starting at its last verified address %s (configured %s)", self.settings.id, last, self.settings.host)
        return last or self.settings.host

    def _initial_identity(self) -> tuple[str, str]:
        try:
            stored = self.cache.get_identity(self.settings.id)
        except Exception as error:
            log.debug("Device %s: no stored identity (%s)", self.settings.id, error)
            stored = None
        if self.settings.mac:
            if stored and stored[0] != self.settings.mac:
                log.warning(
                    "Device %s: the configured MAC %s differs from the previously stored MAC %s; using the configured one",
                    self.settings.id,
                    self.settings.mac,
                    stored[0],
                )
            return self.settings.mac, "config"
        if stored:
            return stored[0], "learned"
        return "", "unknown"

    @property
    def connected(self) -> bool:
        return self._device is not None

    async def _verify_identity(self, device: Any, host: str) -> None:
        """Accept ``device`` as this plug only if its authenticated MAC matches. Raises DeviceReadError('identity')."""
        mac = normalize_mac(getattr(device, "mac", ""))
        if not mac:
            await asyncio.wait_for(device.update(), self.timeout)
            mac = normalize_mac(getattr(device, "mac", ""))
        device_id = self.settings.id
        if not mac:
            if self.expected_mac:
                await self._safe_disconnect(device)
                raise DeviceReadError(f"{host} did not report a MAC address, so it cannot be verified as {self.expected_mac}", "identity")
            log.warning("Device %s: %s did not report a MAC address; its identity cannot be checked", device_id, host)
            return
        if self.expected_mac:
            if mac != self.expected_mac:
                await self._safe_disconnect(device)
                owner = self._owner(mac)
                who = f"plug '{owner}'" if owner else "a different device"
                log.warning("Device %s: %s is %s (MAC %s, expected %s); not using it", device_id, host, who, mac, self.expected_mac)
                raise DeviceReadError(f"{host} is {who} (MAC {mac}), not {device_id} (MAC {self.expected_mac})", "identity")
            log.info("Device %s: identity verified (MAC %s at %s)", device_id, mac, host)
            self._store_identity(mac, self.identity_source)
            return
        owner = self._owner(mac)
        if owner and owner != device_id:
            await self._safe_disconnect(device)
            log.warning("Device %s: %s is plug '%s' (MAC %s); not learning it as %s", device_id, host, owner, mac, device_id)
            raise DeviceReadError(f"{host} is plug '{owner}' (MAC {mac}), not {device_id}", "identity")
        self.expected_mac, self.identity_source = mac, "learned"
        self._store_identity(mac, "learned")
        log.info('Device %s: identity learned and stored: MAC %s at %s. To pin it, add  mac: "%s"  to this device in config.yaml', device_id, mac, host, mac)

    def _owner(self, mac: str) -> str | None:
        try:
            return self.cache.identity_owner(mac)
        except Exception:
            return None

    def _store_identity(self, mac: str, source: str) -> None:
        try:
            self.cache.set_identity(self.settings.id, mac, source)
        except Exception as error:
            log.debug("Device %s: could not store identity: %s", self.settings.id, error)

    # ---------------------------------------------------------------- connecting

    async def _connect_cached(self, host: str) -> Any | None:
        from kasa import Device, DeviceConfig

        cached = self.cache.get_connection(self.settings.id, host)
        if not cached:
            return None
        try:
            config = DeviceConfig.from_dict({**cached, "host": host})
            config.credentials = self.credentials
            config.timeout = int(self.timeout)
            return await asyncio.wait_for(Device.connect(config=config), self.timeout * 2)
        except Exception as error:
            if classify_error(error) == "auth":
                raise
            log.info("Device %s: cached connection parameters failed at %s (%s); trying discovery", self.settings.id, host, error)
            return None

    async def _connect_discover(self, host: str) -> Any:
        from kasa import Discover

        try:
            device = await Discover.discover_single(host, credentials=self.credentials, discovery_timeout=5, timeout=int(self.timeout))
            if device is not None:
                await asyncio.wait_for(device.update(), self.timeout)
                return device
        except Exception as error:
            kind = classify_error(error)
            if kind == "auth":
                raise
            if kind == "unsupported":
                # The plug answered, but announced a protocol this python-kasa version cannot speak
                # (e.g. encrypt_type 'TPAP' on P110 firmware 1.4.x). Trying other protocols will not help.
                hint = (
                    " This firmware uses TP-Link's newer TPAP login, which python-kasa does not support yet "
                    "(see https://github.com/python-kasa/python-kasa/issues/1590). Update python-kasa once a release supports it."
                    if "TPAP" in str(error)
                    else ""
                )
                raise DeviceReadError(f"{host} answered but is not supported by python-kasa: {error}.{hint}", "unsupported") from error
            log.info("Device %s: UDP discovery of %s failed (%s); trying direct TCP connection", self.settings.id, host, error)
        device = await asyncio.wait_for(
            Discover.try_connect_all(host, credentials=self.credentials, timeout=int(self.timeout)),
            self.timeout * 6,
        )
        if device is None:
            raise DeviceReadError(f"No supported Tapo device answered at {host}", "unreachable")
        return device

    async def connect(self, host: str | None = None) -> Any:
        """Connect to this plug at ``host`` (default: its current address). A new address is only adopted after the
        plug's identity has been verified there."""
        host = host or self.host
        try:
            device = await self._connect_cached(host) or await self._connect_discover(host)
        except DeviceReadError:
            raise
        except Exception as error:
            kind = classify_error(error)
            if kind == "auth":
                if host == self.host:
                    self.cache.forget_connection(self.settings.id)
                raise DeviceReadError(
                    f"TP-Link login rejected by {host}. Check TAPO_USERNAME/TAPO_PASSWORD (the Tapo app account that owns the plug).",
                    "auth",
                ) from error
            raise DeviceReadError(f"Cannot connect to {host}: {str(error) or type(error).__name__}", kind) from error

        from kasa import Module

        if Module.Energy not in device.modules:
            await self._safe_disconnect(device)
            other_mac = normalize_mac(getattr(device, "mac", ""))
            if self.expected_mac and other_mac and other_mac != self.expected_mac:
                raise DeviceReadError(f"{host} is a different device ({getattr(device, 'model', '?')}, MAC {other_mac}), not {self.settings.id}", "identity")
            raise DeviceReadError(
                f"{host} ({getattr(device, 'model', '?')}) has no energy monitoring; a Tapo P110 is required.",
                "unsupported",
            )
        await self._verify_identity(device, host)
        previous = self.host
        self._device = device
        self.host = host
        self.model = getattr(device, "model", None)
        self.alias = getattr(device, "alias", None)
        try:
            self.cache.set_connection(self.settings.id, host, safe_connection_dict(device))
        except Exception as error:  # caching is an optimisation only
            log.debug("Device %s: could not cache connection parameters: %s", self.settings.id, error)
        if previous != host:
            self.last_host_change = {"from": previous, "to": host, "at": _iso(self.wall_clock())}
            log.warning(
                "Device %s: IP address changed from %s to %s (identity %s verified); stored for future starts",
                self.settings.id,
                previous,
                host,
                self.expected_mac,
            )
        log.info("Device %s: connected to %s (%s, '%s')", self.settings.id, host, self.model, self.alias)
        return device

    # ---------------------------------------------------------------- reconnection and rediscovery

    async def _reconnect(self) -> Any:
        try:
            device = await self.connect()
        except DeviceReadError as error:
            self.connect_failures += 1
            if not self._should_rediscover(error):
                raise
            device = await self._rediscover(error)
        self.connect_failures = 0
        return device

    def _should_rediscover(self, error: DeviceReadError) -> bool:
        if self.locator is None or not self.locator.settings.enabled or error.kind == "auth":
            return False
        if not self.expected_mac:
            self.rediscovery_state = "unavailable"
            if not self._reported_no_identity:
                self._reported_no_identity = True
                log.warning(
                    "Device %s: cannot search for this plug at another address because its MAC address is unknown "
                    '(it has never been connected). Add  mac: "AA:BB:CC:DD:EE:FF"  (Tapo app > Device info) to config.yaml.',
                    self.settings.id,
                )
            return False
        if error.kind != "identity" and self.connect_failures < self.locator.settings.failures_before_rediscovery:
            return False
        return self.clock() >= self.next_rediscovery_at

    async def _rediscover(self, error: DeviceReadError) -> Any:
        assert self.locator is not None
        settings = self.locator.settings
        device_id, mac = self.settings.id, self.expected_mac
        self.rediscovery_attempts += 1
        self.last_rediscovery_at = self.wall_clock()
        log.warning(
            "Device %s: rediscovery started: looking for MAC %s (last address %s, %d failed connection attempt(s): %s)",
            device_id,
            mac,
            self.host,
            self.connect_failures,
            error,
        )
        try:
            hinted, summary = await self.locator.candidates(mac)
        except Exception as scan_error:
            hinted, summary = [], f"discovery failed: {str(scan_error) or type(scan_error).__name__}"
        tried = {self.host}
        order: list[str] = []
        for host in [*hinted, self.settings.host, *settings.candidate_hosts]:
            if host not in tried and host not in order:
                order.append(host)
        auth_error: DeviceReadError | None = None
        for host in order:
            try:
                device = await asyncio.wait_for(self.connect(host), self.timeout * 4)
            except DeviceReadError as candidate_error:
                if candidate_error.kind == "auth" and host in hinted:
                    auth_error = candidate_error  # our plug (by MAC hint) rejected the login
                log.debug("Device %s: candidate %s rejected: %s", device_id, host, candidate_error)
                continue
            except Exception as candidate_error:
                log.debug("Device %s: candidate %s failed: %s", device_id, host, str(candidate_error) or type(candidate_error).__name__)
                continue
            self.rediscovery_failures = 0
            self.next_rediscovery_at = 0.0
            self.rediscovery_state = "idle"
            self.last_rediscovery_result = f"found at {host}"
            log.info("Device %s: reconnected after rediscovery at %s (MAC %s)", device_id, host, mac)
            return device
        self.rediscovery_failures += 1
        delay = min(settings.max_interval_seconds, settings.min_interval_seconds * 2 ** min(self.rediscovery_failures - 1, 20))
        self.next_rediscovery_at = self.clock() + delay
        self.rediscovery_state = "waiting"
        tried_text = ", ".join(order) if order else "none"
        self.last_rediscovery_result = f"not found ({summary}; candidates tried: {tried_text})"
        log.warning(
            "Device %s: rediscovery failed: MAC %s not found (%s; candidates tried: %s); next attempt in %.0f s",
            device_id,
            mac,
            summary,
            tried_text,
            delay,
        )
        if auth_error is not None:
            raise auth_error
        raise DeviceReadError(f"{error}; rediscovery did not find the plug (next attempt in {delay:.0f} s)", error.kind)

    def connection_status(self) -> dict[str, Any]:
        next_in = max(0.0, self.next_rediscovery_at - self.clock()) if self.rediscovery_state == "waiting" else None
        return {
            "current_host": self.host,
            "configured_host": self.settings.host,
            "mac": self.expected_mac or None,
            "identity": self.identity_source,
            "connected": self.connected,
            "failed_connection_attempts": self.connect_failures,
            "last_host_change": self.last_host_change,
            "rediscovery": {
                "state": self.rediscovery_state,
                "attempts": self.rediscovery_attempts,
                "consecutive_failures": self.rediscovery_failures,
                "last_attempt": _iso(self.last_rediscovery_at),
                "last_result": self.last_rediscovery_result,
                "next_attempt_in_seconds": round(next_in) if next_in is not None else None,
            },
        }

    # ---------------------------------------------------------------- reading

    async def read(self) -> Reading:
        """Poll the plug once. Raises DeviceReadError; the connection is dropped so the next call reconnects."""
        try:
            device = self._device or await self._reconnect()
            started_wall = time.time()
            started = time.monotonic()
            await asyncio.wait_for(device.update(), self.timeout)
            latency = time.monotonic() - started
        except DeviceReadError:
            await self.close()
            raise
        except Exception as error:
            await self.close()
            self.connect_failures += 1  # the open connection broke: first failed attempt at this address
            kind = classify_error(error)
            message = "TP-Link login rejected" if kind == "auth" else f"read failed: {str(error) or type(error).__name__}"
            raise DeviceReadError(f"{self.host}: {message}", kind) from error
        mac = normalize_mac(getattr(device, "mac", ""))
        if self.expected_mac and mac and mac != self.expected_mac:  # the address now belongs to another plug
            await self.close()
            self.connect_failures += 1
            raise DeviceReadError(f"{self.host} now answers as MAC {mac}, not {self.settings.id} (MAC {self.expected_mac})", "identity")
        # Timestamp at the middle of the request: the device sampled its meter while answering.
        timestamp = datetime.fromtimestamp(started_wall, UTC) + timedelta(seconds=latency / 2)
        from kasa import Module

        metrics = metrics_from_energy_module(device.modules[Module.Energy], self.settings.id)
        return Reading(timestamp=timestamp, metrics=metrics, latency_seconds=latency)

    @staticmethod
    async def _safe_disconnect(device: Any) -> None:
        try:
            await asyncio.wait_for(device.disconnect(), 5)
        except Exception as error:
            log.debug("Ignoring error while disconnecting: %s", error)

    async def close(self) -> None:
        device, self._device = self._device, None
        if device is not None:
            await self._safe_disconnect(device)
