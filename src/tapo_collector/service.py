"""The long-running collector: one polling task per plug, one upload task, one maintenance task.

Failure isolation: each plug has its own task and its own backoff, so an offline or slow plug never
delays the others. Upload problems never block polling: readings keep going into SQLite.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from tapo_collector import __version__
from tapo_collector.config import DeviceSettings, Secrets, Settings
from tapo_collector.devices import DeviceReadError, Reading, TapoPlug, kasa_credentials
from tapo_collector.measurements import Measurement, format_timestamp, offline_measurement, online_measurement
from tapo_collector.rediscovery import Rediscovery
from tapo_collector.storage import Storage
from tapo_collector.uploader import ApiClient, Outcome, UploadManager

log = logging.getLogger(__name__)

MAX_DEVICE_BACKOFF_SECONDS = 300.0
# A Raspberry Pi has no battery-backed clock: until NTP has synchronised after boot the time can be far in the past.
# Readings are not stored while the clock is obviously wrong (before this collector was written).
EARLIEST_VALID_TIME = datetime(2026, 1, 1, tzinfo=UTC).timestamp()


class Plug(Protocol):
    settings: DeviceSettings

    async def read(self) -> Reading: ...
    async def close(self) -> None: ...


@dataclass
class DeviceState:
    device_id: str
    name: str
    host: str
    last_success: float | None = None
    last_attempt: float | None = None
    offline_since: float | None = None
    consecutive_failures: int = 0
    last_error: str = ""
    last_error_kind: str = ""
    last_power_w: float | None = None
    last_alert: float | None = None
    connection: dict[str, Any] | None = None  # TapoPlug.connection_status(): address, identity, rediscovery

    def as_dict(self) -> dict[str, Any]:
        data = {
            "device_id": self.device_id,
            "name": self.name,
            "host": self.host,
            "online": self.consecutive_failures == 0 and self.last_success is not None,
            "last_success": _iso(self.last_success),
            "last_attempt": _iso(self.last_attempt),
            "offline_since": _iso(self.offline_since),
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "last_power_w": self.last_power_w,
        }
        if self.connection is not None:
            data["connection"] = self.connection
        return data


def _iso(value: float | None) -> str | None:
    return format_timestamp(datetime.fromtimestamp(value, UTC)) if value is not None else None


def device_retry_delay(interval: float, failures: int) -> float:
    """After a failed poll: interval, 2x, 4x ... capped at 5 minutes (but never shorter than the interval)."""
    return max(interval, min(MAX_DEVICE_BACKOFF_SECONDS, interval * 2 ** min(max(failures - 1, 0), 10)))


class DevicePoller:
    def __init__(self, plug: Plug, storage: Storage, collector_id: str, *, clock: Callable[[], float] = time.time) -> None:
        self.plug = plug
        self.storage = storage
        self.collector_id = collector_id
        self.clock = clock
        s = plug.settings
        self.state = DeviceState(device_id=s.id, name=s.name, host=getattr(plug, "host", s.host))
        self._refresh_connection()

    def _refresh_connection(self) -> None:
        self.state.host = getattr(self.plug, "host", self.plug.settings.host)
        status = getattr(self.plug, "connection_status", None)
        if callable(status):
            try:
                self.state.connection = status()
            except Exception as error:
                log.debug("Device %s: connection status unavailable: %s", self.state.device_id, error)

    async def poll_once(self) -> bool:
        try:
            return await self._poll_once()
        finally:
            self._refresh_connection()

    async def _poll_once(self) -> bool:
        """Read the plug once and store the result (online or offline). Returns True on success."""
        settings = self.plug.settings
        self.state.last_attempt = self.clock()
        if self.state.last_attempt < EARLIEST_VALID_TIME:
            log.warning("System clock is not set (%s); skipping this poll until time is synchronised (NTP)", _iso(self.state.last_attempt))
            return False
        try:
            reading = await self.plug.read()
        except DeviceReadError as error:
            return self._failure(str(error), error.kind)
        except Exception as error:  # never let one plug kill its task
            log.exception("Device %s: unexpected error while reading", settings.id)
            return self._failure(f"unexpected error: {error}", "error")
        measurement = online_measurement(self.collector_id, settings.id, reading.timestamp, reading.metrics)
        self._save(measurement)
        if self.state.consecutive_failures:
            log.info(
                "Device %s (%s) reconnected at %s after %d failed poll(s); offline for %.0f s",
                settings.id,
                settings.name,
                getattr(self.plug, "host", settings.host),
                self.state.consecutive_failures,
                self.clock() - (self.state.offline_since or self.clock()),
            )
        self.state.consecutive_failures = 0
        self.state.offline_since = None
        self.state.last_error = ""
        self.state.last_error_kind = ""
        self.state.last_alert = None
        self.state.last_success = self.clock()
        self.state.last_power_w = measurement.power_w
        log.debug("Device %s: %.1f W (%.2f s)", settings.id, measurement.power_w or 0.0, reading.latency_seconds)
        return True

    def _save(self, measurement: Measurement) -> None:
        if not self.storage.save(measurement):  # IDs are random UUIDs, so this indicates a bug: make it visible
            log.error("Device %s: reading %s was not stored (measurement ID already exists)", measurement.device_id, measurement.measurement_id)

    def _failure(self, message: str, kind: str) -> bool:
        settings = self.plug.settings
        now = self.clock()
        self.state.consecutive_failures += 1
        self.state.last_error = message
        self.state.last_error_kind = kind
        if self.state.offline_since is None:
            self.state.offline_since = now
        # Record that the plug did not answer. Metrics stay empty: missing readings are never invented.
        self._save(offline_measurement(self.collector_id, settings.id, datetime.fromtimestamp(now, UTC)))
        level = logging.ERROR if kind in ("auth", "unsupported") else logging.WARNING
        if self.state.consecutive_failures == 1:
            log.log(level, "Device %s (%s): connection lost (%s): %s", settings.id, settings.name, kind, message)
        elif kind in ("auth", "unsupported"):
            log.log(level, "Device %s (%s): %s", settings.id, settings.name, message)
        else:
            log.debug("Device %s: still failing (%d): %s", settings.id, self.state.consecutive_failures, message)
        return False

    async def run(self, stop: asyncio.Event) -> None:
        interval = self.plug.settings.polling_interval_seconds
        next_due = time.monotonic()
        while not stop.is_set():
            ok = await self.poll_once()
            if ok:
                next_due += interval
                if next_due < time.monotonic():  # we fell behind (slow plug, suspended PC): resync
                    next_due = time.monotonic() + interval
            else:
                next_due = time.monotonic() + device_retry_delay(interval, self.state.consecutive_failures)
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(0.0, next_due - time.monotonic()))
            except TimeoutError:
                pass


class Collector:
    def __init__(
        self,
        settings: Settings,
        secrets: Secrets,
        *,
        storage: Storage | None = None,
        plugs: list[Plug] | None = None,
        client: ApiClient | None = None,
        clock: Callable[[], float] = time.time,
        rediscovery: Rediscovery | None = None,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self.storage = storage or Storage(settings.storage.database_path, max_pending=settings.storage.max_pending_measurements)
        self._prepare_identities()
        self.rediscovery = rediscovery or Rediscovery(settings.rediscovery)
        if plugs is None:
            credentials = kasa_credentials(secrets.tapo_username, secrets.tapo_password)
            plugs = [TapoPlug(d, credentials, self.storage, timeout=settings.device_timeout_seconds, locator=self.rediscovery) for d in settings.devices]
        self.pollers = [DevicePoller(p, self.storage, settings.collector_id, clock=clock) for p in plugs]
        self.rediscovery.online_plugs = self._online_plugs
        self.rediscovery.known_macs = self._known_macs
        self.client = client or ApiClient(settings.upload, secrets.api_token)
        self.uploader = UploadManager(self.storage, self.client, settings.upload, settings.collector_id, clock=clock)
        self.started_at = clock()
        self._last_upload_alert: float | None = None

    def _prepare_identities(self) -> None:
        """Pin MACs from the configuration and forget identities of removed plugs, so that no plug can learn a MAC
        that belongs to another configured plug."""
        try:
            self.storage.forget_identities_except(d.id for d in self.settings.devices)
            for device in self.settings.devices:
                if device.mac:
                    owner = self.storage.identity_owner(device.mac)
                    if owner and owner != device.id:
                        log.warning("MAC %s is configured for %s but was learned for %s; the configuration wins", device.mac, device.id, owner)
                        self.storage.forget_identity(owner)
                    self.storage.set_identity(device.id, device.mac, "config")
        except Exception:
            log.exception("Could not prepare device identities")

    def _online_plugs(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for poller in self.pollers:
            plug = poller.plug
            if getattr(plug, "connected", False) and getattr(plug, "expected_mac", ""):
                result[str(getattr(plug, "host", ""))] = str(getattr(plug, "expected_mac", ""))
        return result

    def _known_macs(self) -> set[str]:
        return {str(getattr(p.plug, "expected_mac", "")) for p in self.pollers if getattr(p.plug, "expected_mac", "")} | {
            d.mac for d in self.settings.devices if d.mac
        }

    # ---------------------------------------------------------------- reporting

    def status(self) -> dict[str, Any]:
        stats = self.storage.stats(now=self.clock())
        up = self.uploader
        return {
            "collector_id": self.settings.collector_id,
            "version": __version__,
            "pid": os.getpid(),
            "updated_at": _iso(self.clock()),
            "started_at": _iso(self.started_at),
            "endpoint_url": self.settings.upload.endpoint_url,
            "queue": stats.as_dict(),
            "upload": {
                "last_success": _iso(up.last_success),
                "failing_since": _iso(up.failing_since),
                "consecutive_failures": up.consecutive_failures,
                "last_error": up.last_error,
                "last_status": up.last_status,
            },
            "devices": [p.state.as_dict() for p in self.pollers],
            "rediscovery": self.rediscovery.status(),
        }

    def write_status(self) -> None:
        path = self.settings.status_path
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.status(), indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as error:
            log.warning("Could not write status file %s: %s", path, error)

    def check_alerts(self) -> None:
        now = self.clock()
        alerts = self.settings.alerts
        for poller in self.pollers:
            s = poller.state
            if s.offline_since is None or now - s.offline_since < alerts.device_offline_after_seconds:
                continue
            if s.last_alert is None or now - s.last_alert >= alerts.repeat_every_seconds:
                s.last_alert = now
                log.error(
                    "ALERT device %s (%s, %s) has not answered for %.0f min: %s",
                    s.device_id,
                    s.name,
                    s.host,
                    (now - s.offline_since) / 60,
                    s.last_error,
                )
        up = self.uploader
        if up.failing_since is not None and now - up.failing_since >= alerts.upload_failing_after_seconds:
            if self._last_upload_alert is None or now - self._last_upload_alert >= alerts.repeat_every_seconds:
                self._last_upload_alert = now
                stats = self.storage.stats(now=now)
                log.error(
                    "ALERT uploads to the website have failed for %.0f min (%s). %d reading(s) are safely queued "
                    "locally (oldest %s) and will be sent when the connection works again.",
                    (now - up.failing_since) / 60,
                    up.last_error,
                    stats.pending,
                    stats.oldest_pending,
                )
        elif up.failing_since is None:
            self._last_upload_alert = None

    def maintenance(self) -> None:
        removed = self.storage.prune(
            delivered_days=self.settings.storage.delivered_retention_days,
            rejected_days=self.settings.storage.rejected_retention_days,
            now=self.clock(),
        )
        if removed:
            log.info("Removed %d old delivered/rejected reading(s) from the local buffer", removed)

    async def _maintenance_loop(self, stop: asyncio.Event) -> None:
        last_prune = 0.0
        while not stop.is_set():
            try:
                self.check_alerts()
                if self.clock() - last_prune >= 3600:
                    self.maintenance()
                    last_prune = self.clock()
                self.write_status()
            except Exception:
                log.exception("Maintenance step failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=30)
            except TimeoutError:
                pass

    # ---------------------------------------------------------------- lifecycle

    async def run(self, stop: asyncio.Event) -> None:
        stats = self.storage.stats()
        log.info(
            "Collector %s v%s started: %d device(s), upload to %s, buffer %s",
            self.settings.collector_id,
            __version__,
            len(self.pollers),
            self.settings.upload.endpoint_url,
            self.settings.storage.database_path,
        )
        if stats.pending:
            log.info("%d reading(s) from before the restart are still queued (oldest %s); uploading them first", stats.pending, stats.oldest_pending)
        if not self.pollers:
            log.warning("No devices configured. Add plugs under 'devices:' in %s", self.settings.config_path)
        self.write_status()
        tasks = [asyncio.create_task(p.run(stop), name=f"poll:{p.state.device_id}") for p in self.pollers]
        upload_task = asyncio.create_task(self.uploader.run(stop), name="upload")
        maintenance_task = asyncio.create_task(self._maintenance_loop(stop), name="maintenance")
        try:
            await stop.wait()
        finally:
            log.info("Stopping: finishing current work")
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Give an in-flight upload a moment to finish so its acknowledgement is recorded.
            try:
                await asyncio.wait_for(asyncio.gather(upload_task, maintenance_task, return_exceptions=True), timeout=15)
            except TimeoutError:
                upload_task.cancel()
                maintenance_task.cancel()
                await asyncio.gather(upload_task, maintenance_task, return_exceptions=True)
            await self._final_flush()
            await self.close()

    async def _final_flush(self, budget_seconds: float = 10.0) -> None:
        """Try to send the last readings before exiting. Whatever is not sent stays queued for the next start."""
        deadline = time.monotonic() + budget_seconds
        try:
            while time.monotonic() < deadline:
                outcome = await asyncio.wait_for(self.uploader.upload_once(), max(0.1, deadline - time.monotonic()))
                if outcome is not Outcome.SENT:
                    break
        except TimeoutError:
            pass
        except Exception as error:
            log.debug("Final upload before stopping failed: %s", error)

    async def close(self) -> None:
        for poller in self.pollers:
            try:
                await poller.plug.close()
            except Exception as error:
                log.debug("Closing %s: %s", poller.state.device_id, error)
        self.write_status()
        await self.client.close()
        stats = self.storage.stats()
        self.storage.close()
        log.info("Collector stopped cleanly; %d reading(s) queued for the next start", stats.pending)


def install_signal_handlers(stop: asyncio.Event) -> None:
    """SIGTERM (systemd stop) and SIGINT (Ctrl+C) trigger a graceful shutdown."""
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows: fall back to the classic handler
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
