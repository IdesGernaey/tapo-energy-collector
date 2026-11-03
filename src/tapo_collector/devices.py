"""Connecting to and reading one Tapo P110 with python-kasa.

Connection strategy (designed for networks where UDP broadcast/discovery may be blocked):

1. Reuse cached connection parameters (``DeviceConfig.to_dict()`` without credentials) with
   ``Device.connect()`` - direct TCP/HTTP, no UDP needed.
2. ``Discover.discover_single(host)`` - unicast UDP discovery to that address (port 20002).
3. ``Discover.try_connect_all(host)`` - tries every known protocol over TCP.

After a successful connection the parameters are cached in SQLite for the next start.
The collector only calls ``update()`` and reads properties; it never switches a plug.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from tapo_collector.config import DeviceSettings
from tapo_collector.measurements import metrics_from_energy_module

log = logging.getLogger(__name__)

# Keys of DeviceConfig.to_dict() that must never be written to disk.
_SECRET_CONFIG_KEYS = {"credentials", "credentials_hash", "aes_keys", "http_client"}


class DeviceReadError(Exception):
    """A plug could not be read. ``kind`` is one of: unreachable, auth, unsupported, error."""

    def __init__(self, message: str, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind


class ConnectionCache(Protocol):
    def get_connection(self, device_id: str, host: str) -> dict[str, Any] | None: ...
    def set_connection(self, device_id: str, host: str, config: dict[str, Any]) -> None: ...
    def forget_connection(self, device_id: str) -> None: ...


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


class TapoPlug:
    """One configured plug. Keeps the python-kasa connection open between polls and reconnects on failure."""

    def __init__(
        self,
        settings: DeviceSettings,
        credentials: Any,
        cache: ConnectionCache,
        *,
        timeout: float = 10.0,
    ) -> None:
        self.settings = settings
        self.credentials = credentials
        self.cache = cache
        self.timeout = timeout
        self._device: Any | None = None
        self.model: str | None = None
        self.alias: str | None = None

    @property
    def connected(self) -> bool:
        return self._device is not None

    async def _connect_cached(self) -> Any | None:
        from kasa import Device, DeviceConfig

        cached = self.cache.get_connection(self.settings.id, self.settings.host)
        if not cached:
            return None
        try:
            config = DeviceConfig.from_dict({**cached, "host": self.settings.host})
            config.credentials = self.credentials
            config.timeout = int(self.timeout)
            return await asyncio.wait_for(Device.connect(config=config), self.timeout * 2)
        except Exception as error:
            if classify_error(error) == "auth":
                raise
            log.info("Device %s: cached connection parameters failed (%s); trying discovery", self.settings.id, error)
            return None

    async def _connect_discover(self) -> Any:
        from kasa import Discover

        try:
            device = await Discover.discover_single(self.settings.host, credentials=self.credentials, discovery_timeout=5, timeout=int(self.timeout))
            if device is not None:
                await asyncio.wait_for(device.update(), self.timeout)
                return device
        except Exception as error:
            if classify_error(error) == "auth":
                raise
            log.info("Device %s: UDP discovery failed (%s); trying direct TCP connection", self.settings.id, error)
        device = await asyncio.wait_for(
            Discover.try_connect_all(self.settings.host, credentials=self.credentials, timeout=int(self.timeout)),
            self.timeout * 6,
        )
        if device is None:
            raise DeviceReadError(f"No supported Tapo device answered at {self.settings.host}", "unreachable")
        return device

    async def connect(self) -> Any:
        try:
            device = await self._connect_cached() or await self._connect_discover()
        except DeviceReadError:
            raise
        except Exception as error:
            kind = classify_error(error)
            if kind == "auth":
                self.cache.forget_connection(self.settings.id)
                raise DeviceReadError(
                    f"TP-Link login rejected by {self.settings.host}. Check TAPO_USERNAME/TAPO_PASSWORD (the Tapo app account that owns the plug).",
                    "auth",
                ) from error
            raise DeviceReadError(f"Cannot connect to {self.settings.host}: {str(error) or type(error).__name__}", kind) from error

        from kasa import Module

        if Module.Energy not in device.modules:
            await self._safe_disconnect(device)
            raise DeviceReadError(
                f"{self.settings.host} ({getattr(device, 'model', '?')}) has no energy monitoring; a Tapo P110 is required.",
                "unsupported",
            )
        self._device = device
        self.model = getattr(device, "model", None)
        self.alias = getattr(device, "alias", None)
        try:
            self.cache.set_connection(self.settings.id, self.settings.host, safe_connection_dict(device))
        except Exception as error:  # caching is an optimisation only
            log.debug("Device %s: could not cache connection parameters: %s", self.settings.id, error)
        log.info("Device %s: connected to %s (%s, '%s')", self.settings.id, self.settings.host, self.model, self.alias)
        return device

    async def read(self) -> Reading:
        """Poll the plug once. Raises DeviceReadError; the connection is dropped so the next call reconnects."""
        try:
            device = self._device or await self.connect()
            started_wall = time.time()
            started = time.monotonic()
            await asyncio.wait_for(device.update(), self.timeout)
            latency = time.monotonic() - started
        except DeviceReadError:
            await self.close()
            raise
        except Exception as error:
            await self.close()
            kind = classify_error(error)
            message = "TP-Link login rejected" if kind == "auth" else f"read failed: {str(error) or type(error).__name__}"
            raise DeviceReadError(f"{self.settings.host}: {message}", kind) from error
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
