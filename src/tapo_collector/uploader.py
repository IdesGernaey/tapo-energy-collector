"""Upload of queued readings to the website (``POST /api/energy/measurements``).

Response handling (see docs/API.md for the contract):

=========  =============================================================================
status     collector behaviour
=========  =============================================================================
200/201    per-record acknowledgement: ``accepted`` and ``duplicates`` -> delivered;
           ``rejected`` with reason ``unknown_device`` -> kept pending, retried later;
           other rejections -> marked rejected (kept locally, see ``status``).
           IDs missing from the acknowledgement stay pending and are retried.
400 / 413  batch split in halves to isolate the bad record; a single record -> rejected.
401 / 403  token missing/invalid/revoked: nothing is changed, upload pauses
           ``auth_failure_retry_seconds`` and an error is logged.
409        conflict: the server already holds these IDs -> treated as delivered.
429        rate limited: waits ``Retry-After`` (or backoff) and retries.
5xx, network errors, timeouts, malformed replies: exponential backoff with jitter.
other 4xx  configuration error (wrong URL?): long pause, readings stay pending.
=========  =============================================================================
"""

from __future__ import annotations

import asyncio
import email.utils
import enum
import logging
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from tapo_collector import __version__
from tapo_collector.config import UploadSettings
from tapo_collector.measurements import Measurement
from tapo_collector.storage import Storage

log = logging.getLogger(__name__)

RETRY_LATER_REASONS = {"unknown_device", "device_disabled"}


class Outcome(enum.Enum):
    IDLE = "idle"  # nothing to send
    SENT = "sent"  # request acknowledged (some records may be rejected)
    RETRY = "retry"  # transient failure, back off
    AUTH = "auth"  # token rejected
    CONFIG = "config"  # endpoint/configuration problem


@dataclass
class Acknowledgement:
    accepted: set[str] = field(default_factory=set)
    duplicates: set[str] = field(default_factory=set)
    rejected: dict[str, str] = field(default_factory=dict)


@dataclass
class SendResult:
    outcome: Outcome
    status: int | None = None
    ack: Acknowledgement | None = None
    retry_after: float | None = None
    message: str = ""


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    now = time.time() if now is None else now
    return max(0.0, when.timestamp() - now)


def backoff_delay(failures: int, *, base: float, cap: float, rng: Callable[[], float] = random.random) -> float:
    """Exponential backoff with "equal jitter": half fixed, half random, capped at ``cap``."""
    exponent = min(max(failures - 1, 0), 30)
    ceiling = min(cap, base * (2**exponent))
    return ceiling / 2 + rng() * ceiling / 2


def parse_acknowledgement(body: Any, sent_ids: set[str]) -> Acknowledgement:
    if not isinstance(body, dict):
        raise ValueError("acknowledgement is not a JSON object")
    ack = Acknowledgement()
    for key, target in (("accepted", ack.accepted), ("duplicates", ack.duplicates)):
        values = body.get(key, [])
        if not isinstance(values, list):
            raise ValueError(f"'{key}' is not a list")
        target.update(v for v in values if isinstance(v, str) and v in sent_ids)
    rejected = body.get("rejected", [])
    if not isinstance(rejected, list):
        raise ValueError("'rejected' is not a list")
    for item in rejected:
        if isinstance(item, dict) and item.get("measurement_id") in sent_ids:
            ack.rejected[item["measurement_id"]] = str(item.get("reason") or "rejected")[:200]
    return ack


class ApiClient:
    """Thin HTTP client. Never logs the token or request bodies."""

    def __init__(self, settings: UploadSettings, token: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            timeout=settings.timeout_seconds,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": f"tapo-energy-collector/{__version__}",
                "Accept": "application/json",
            },
            follow_redirects=False,  # a redirect would drop the POST body / leak the token to another host
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def send(self, collector_id: str, batch: Sequence[Measurement]) -> SendResult:
        payload = {"collector_id": collector_id, "measurements": [m.to_api() for m in batch]}
        try:
            response = await self._client.post(self.settings.endpoint_url, json=payload)
        except httpx.TimeoutException:
            return SendResult(Outcome.RETRY, message="request timed out")
        except httpx.HTTPError as error:
            return SendResult(Outcome.RETRY, message=f"network error: {type(error).__name__}: {error}")

        status = response.status_code
        detail = _error_detail(response)
        if status in (200, 201):
            try:
                ack = parse_acknowledgement(response.json(), {m.measurement_id for m in batch})
            except ValueError as error:
                return SendResult(Outcome.RETRY, status, message=f"unreadable acknowledgement ({error})")
            return SendResult(Outcome.SENT, status, ack=ack)
        if status == 409:
            ack = Acknowledgement(duplicates={m.measurement_id for m in batch})
            return SendResult(Outcome.SENT, status, ack=ack, message="409 conflict: treated as already stored")
        if status in (401, 403):
            return SendResult(Outcome.AUTH, status, message=detail or "token rejected")
        if status in (400, 413, 422):
            return SendResult(Outcome.CONFIG, status, message=detail or "request rejected")
        if status == 429:
            return SendResult(Outcome.RETRY, status, retry_after=parse_retry_after(response.headers.get("Retry-After")), message="rate limited")
        if status >= 500:
            return SendResult(Outcome.RETRY, status, retry_after=parse_retry_after(response.headers.get("Retry-After")), message=detail or "server error")
        if 300 <= status < 400:
            return SendResult(Outcome.CONFIG, status, message=f"redirect to {response.headers.get('Location')!r}; use the final https URL")
        return SendResult(Outcome.CONFIG, status, message=detail or "unexpected response")


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        return body["error"][:300]
    return ""


class UploadManager:
    """Moves readings from the SQLite queue to the website, one batch at a time."""

    def __init__(
        self,
        storage: Storage,
        client: ApiClient,
        settings: UploadSettings,
        collector_id: str,
        *,
        clock: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.storage = storage
        self.client = client
        self.settings = settings
        self.collector_id = collector_id
        self.clock = clock
        self.rng = rng
        self.consecutive_failures = 0
        self.failing_since: float | None = None
        self.last_success: float | None = None
        self.last_error: str = ""
        self.last_status: int | None = None
        self.paused_until: float = 0.0
        self.single_record_refusals = 0

    # ----------------------------------------------------------------- one step

    async def upload_once(self) -> Outcome:
        """Send one batch (splitting it when the server refuses the body). Returns the outcome."""
        now = self.clock()
        if now < self.paused_until:
            return Outcome.RETRY
        batch = self.storage.next_batch(self.settings.batch_size, now=now)
        if not batch:
            return Outcome.IDLE
        outcome = await self._deliver(batch)
        if outcome in (Outcome.SENT, Outcome.IDLE):
            self._succeeded()
        return outcome

    async def _deliver(self, batch: list[Measurement]) -> Outcome:
        result = await self.client.send(self.collector_id, batch)
        self.last_status = result.status
        ids = [m.measurement_id for m in batch]

        if result.outcome is Outcome.SENT and result.ack is not None:
            self.single_record_refusals = 0
            self._apply_ack(batch, result.ack, result.message)
            return Outcome.SENT

        if result.outcome is Outcome.CONFIG and result.status in (400, 413, 422):
            if len(batch) > 1:
                middle = len(batch) // 2
                log.warning("Upload: server refused a batch of %d (%s %s); splitting it", len(batch), result.status, result.message)
                first = await self._deliver(batch[:middle])
                if first is not Outcome.SENT:
                    return first
                return await self._deliver(batch[middle:])
            reason = f"HTTP {result.status}: {result.message}"
            self.single_record_refusals += 1
            if self.single_record_refusals > 3:
                # Many single readings refused in a row: more likely a server/config problem than bad data.
                # Stop rejecting (which would slowly empty the queue) and pause instead.
                self.storage.record_attempt(ids, reason)
                self._failed(SendResult(Outcome.CONFIG, result.status, message=result.message))
                return Outcome.CONFIG
            self.storage.mark_rejected({ids[0]: reason})
            log.error("Upload: reading %s (%s) permanently rejected: %s", ids[0], batch[0].device_id, reason)
            return Outcome.SENT

        self.storage.record_attempt(ids, f"HTTP {result.status or '-'}: {result.message}")
        self._failed(result)
        return result.outcome

    def _apply_ack(self, batch: list[Measurement], ack: Acknowledgement, note: str) -> None:
        now = self.clock()
        delivered = ack.accepted | ack.duplicates
        self.storage.mark_delivered(delivered, now=now)
        later = {i: r for i, r in ack.rejected.items() if r in RETRY_LATER_REASONS and i not in delivered}
        final = {i: r for i, r in ack.rejected.items() if i not in later and i not in delivered}
        if later:
            devices = sorted({m.device_id for m in batch if m.measurement_id in later})
            self.storage.defer(later, now + self.settings.unknown_device_retry_seconds, "device not registered/enabled on website")
            log.error(
                "Upload: the website does not accept device(s) %s. Register them on the website's Energy page "
                "(administrators) with exactly these IDs. %d reading(s) kept and retried in %.0f s.",
                ", ".join(devices),
                len(later),
                self.settings.unknown_device_retry_seconds,
            )
        if final:
            self.storage.mark_rejected(final)
            log.error("Upload: %d reading(s) permanently rejected, e.g. %s", len(final), next(iter(final.values())))
        missing = [m.measurement_id for m in batch if m.measurement_id not in delivered and m.measurement_id not in ack.rejected]
        if missing:
            self.storage.record_attempt(missing, "not acknowledged by server")
            log.warning("Upload: %d reading(s) were not acknowledged and will be retried", len(missing))
        if note:
            log.warning("Upload: %s", note)
        log.debug("Upload: %d accepted, %d duplicates, %d rejected", len(ack.accepted), len(ack.duplicates), len(ack.rejected))

    # ----------------------------------------------------------------- state

    def _succeeded(self) -> None:
        if self.failing_since is not None:
            log.info("Upload: connection to the website restored after %d failed attempt(s)", self.consecutive_failures)
        self.consecutive_failures = 0
        self.failing_since = None
        self.last_success = self.clock()
        self.last_error = ""
        self.paused_until = 0.0

    def _failed(self, result: SendResult) -> None:
        now = self.clock()
        self.consecutive_failures += 1
        if self.failing_since is None:
            self.failing_since = now
        self.last_error = f"HTTP {result.status}: {result.message}" if result.status else result.message
        if result.outcome is Outcome.AUTH:
            delay = self.settings.auth_failure_retry_seconds
            log.error(
                "Upload: the website rejected the API token (HTTP %s: %s). Check ENERGY_API_TOKEN; readings are kept. Retrying in %.0f s.",
                result.status,
                result.message,
                delay,
            )
        elif result.outcome is Outcome.CONFIG:
            delay = self.settings.auth_failure_retry_seconds
            log.error(
                "Upload: unexpected response from %s (HTTP %s: %s). Check upload.endpoint_url; readings are kept. Retrying in %.0f s.",
                self.settings.endpoint_url,
                result.status,
                result.message,
                delay,
            )
        else:
            delay = backoff_delay(self.consecutive_failures, base=self.settings.min_retry_seconds, cap=self.settings.max_retry_seconds, rng=self.rng)
            if result.retry_after is not None:
                delay = max(delay, min(result.retry_after, self.settings.max_retry_seconds))
            level = logging.WARNING if self.consecutive_failures <= 3 else logging.ERROR
            log.log(level, "Upload failed (%s); attempt %d, retrying in %.1f s", self.last_error, self.consecutive_failures, delay)
        self.paused_until = now + delay

    # ----------------------------------------------------------------- loop

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                outcome = await self.upload_once()
            except Exception:
                log.exception("Upload: unexpected error; retrying")
                self.consecutive_failures += 1
                self.paused_until = self.clock() + backoff_delay(
                    self.consecutive_failures, base=self.settings.min_retry_seconds, cap=self.settings.max_retry_seconds, rng=self.rng
                )
                outcome = Outcome.RETRY
            if outcome is Outcome.SENT:
                continue  # more may be waiting: drain the backlog quickly
            if outcome is Outcome.IDLE:
                wait = self.settings.idle_seconds
            else:
                wait = max(0.1, self.paused_until - self.clock())
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait)
            except TimeoutError:
                pass
