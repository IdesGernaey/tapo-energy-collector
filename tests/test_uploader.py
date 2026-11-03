from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from conftest import ENDPOINT, TOKEN
from tapo_collector.measurements import online_measurement
from tapo_collector.storage import Storage
from tapo_collector.uploader import (
    ApiClient,
    Outcome,
    UploadManager,
    backoff_delay,
    parse_acknowledgement,
    parse_retry_after,
)

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def fill(storage: Storage, count: int, device: str = "printer_01") -> list[str]:
    ids = []
    for i in range(count):
        m = online_measurement("c", device, T0 + timedelta(seconds=10 * i), {"power_w": float(i)})
        storage.save(m)
        ids.append(m.measurement_id)
    return ids


def ack_all(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(200, json={"accepted": [m["measurement_id"] for m in body["measurements"]], "duplicates": [], "rejected": []})


@pytest.fixture
def manager(make_settings, storage: Storage):
    settings = make_settings()
    client = ApiClient(settings.upload, TOKEN)
    clock = Clock()
    mgr = UploadManager(storage, client, settings.upload, "test-collector", clock=clock, rng=lambda: 0.5)
    mgr.test_clock = clock  # type: ignore[attr-defined]
    yield mgr


@respx.mock
async def test_successful_upload_marks_delivered_and_sends_token(manager: UploadManager, storage: Storage) -> None:
    fill(storage, 3)
    route = respx.post(ENDPOINT).mock(side_effect=ack_all)
    assert await manager.upload_once() is Outcome.SENT
    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["Content-Type"] == "application/json"
    body = json.loads(request.content)
    assert body["collector_id"] == "test-collector"
    assert body["measurements"][0]["timestamp"] == "2026-11-03T12:00:00.000Z"
    assert storage.stats().pending == 0 and storage.stats().delivered == 3
    assert await manager.upload_once() is Outcome.IDLE


@respx.mock
async def test_batches_respect_batch_size(make_settings, storage: Storage) -> None:
    settings = make_settings(upload={"batch_size": 2})
    mgr = UploadManager(storage, ApiClient(settings.upload, TOKEN), settings.upload, "c", clock=Clock())
    fill(storage, 5)
    route = respx.post(ENDPOINT).mock(side_effect=ack_all)
    while await mgr.upload_once() is Outcome.SENT:
        pass
    assert [len(json.loads(c.request.content)["measurements"]) for c in route.calls] == [2, 2, 1]


@respx.mock
async def test_duplicates_count_as_delivered(manager: UploadManager, storage: Storage) -> None:
    ids = fill(storage, 2)
    respx.post(ENDPOINT).respond(200, json={"accepted": [ids[0]], "duplicates": [ids[1]], "rejected": []})
    assert await manager.upload_once() is Outcome.SENT
    assert storage.stats().delivered == 2


@respx.mock
async def test_retry_after_lost_response_does_not_duplicate(manager: UploadManager, storage: Storage) -> None:
    """The server stored the batch but the response was lost; the retry is acknowledged as duplicates."""
    ids = fill(storage, 2)
    respx.post(ENDPOINT).mock(side_effect=[httpx.ReadTimeout("lost"), httpx.Response(200, json={"accepted": [], "duplicates": ids, "rejected": []})])
    assert await manager.upload_once() is Outcome.RETRY
    assert storage.stats().pending == 2
    manager.test_clock.now = manager.paused_until  # type: ignore[attr-defined]
    assert await manager.upload_once() is Outcome.SENT
    assert storage.stats().delivered == 2


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failure_keeps_readings_and_pauses(manager: UploadManager, storage: Storage, status: int, caplog) -> None:
    fill(storage, 2)
    respx.post(ENDPOINT).respond(status, json={"error": "Invalid or revoked energy ingestion token."})
    assert await manager.upload_once() is Outcome.AUTH
    assert storage.stats().pending == 2
    assert manager.paused_until - manager.test_clock.now == pytest.approx(300)  # type: ignore[attr-defined]
    assert "ENERGY_API_TOKEN" in caplog.text
    assert TOKEN not in caplog.text
    # While paused nothing is sent.
    assert await manager.upload_once() is Outcome.RETRY
    assert len(respx.calls) == 1


@respx.mock
async def test_server_errors_back_off_exponentially_then_recover(manager: UploadManager, storage: Storage) -> None:
    fill(storage, 1)
    respx.post(ENDPOINT).mock(side_effect=[httpx.Response(503), httpx.Response(500), httpx.ConnectError("down"), ack_all])
    delays = []
    for _ in range(3):
        assert await manager.upload_once() is Outcome.RETRY
        delays.append(manager.paused_until - manager.test_clock.now)  # type: ignore[attr-defined]
        manager.test_clock.now = manager.paused_until  # type: ignore[attr-defined]
    assert delays == sorted(delays) and delays[0] < delays[2]
    assert storage.stats().pending == 1
    assert await manager.upload_once() is Outcome.SENT
    assert manager.consecutive_failures == 0 and manager.failing_since is None
    assert storage.stats().delivered == 1


@respx.mock
async def test_rate_limit_honours_retry_after(manager: UploadManager, storage: Storage) -> None:
    fill(storage, 1)
    respx.post(ENDPOINT).respond(429, headers={"Retry-After": "42"})
    assert await manager.upload_once() is Outcome.RETRY
    assert manager.paused_until - manager.test_clock.now == pytest.approx(42)  # type: ignore[attr-defined]


@respx.mock
async def test_bad_request_is_split_to_isolate_the_bad_record(manager: UploadManager, storage: Storage) -> None:
    ids = fill(storage, 4)
    bad = ids[2]

    def server(request: httpx.Request) -> httpx.Response:
        sent = [m["measurement_id"] for m in json.loads(request.content)["measurements"]]
        if bad in sent:
            return httpx.Response(400, json={"error": "Invalid request body."})
        return httpx.Response(200, json={"accepted": sent, "duplicates": [], "rejected": []})

    respx.post(ENDPOINT).mock(side_effect=server)
    assert await manager.upload_once() is Outcome.SENT
    stats = storage.stats()
    assert (stats.delivered, stats.rejected, stats.pending) == (3, 1, 0)
    assert storage.recent_rejections()[0]["measurement_id"] == bad


@respx.mock
async def test_many_single_record_refusals_pause_instead_of_rejecting_everything(manager: UploadManager, storage: Storage) -> None:
    fill(storage, 10)
    respx.post(ENDPOINT).respond(400, json={"error": "Invalid request body."})
    outcome = await manager.upload_once()
    assert outcome is Outcome.CONFIG
    assert storage.stats().rejected == 3
    assert storage.stats().pending == 7


@respx.mock
async def test_per_record_rejections(manager: UploadManager, storage: Storage, caplog) -> None:
    ids = fill(storage, 2) + fill(storage, 1, device="unknown_plug")
    respx.post(ENDPOINT).respond(
        200,
        json={
            "accepted": [ids[0]],
            "duplicates": [],
            "rejected": [{"measurement_id": ids[1], "reason": "invalid_value"}, {"measurement_id": ids[2], "reason": "unknown_device"}],
        },
    )
    with caplog.at_level(logging.ERROR):
        assert await manager.upload_once() is Outcome.SENT
    stats = storage.stats()
    assert (stats.delivered, stats.rejected, stats.pending, stats.deferred) == (1, 1, 1, 1)
    assert "unknown_plug" in caplog.text  # tells the operator which device to register
    assert storage.next_batch(10) == []  # deferred: not retried immediately
    assert len(storage.next_batch(10, now=manager.test_clock.now + 601)) == 1  # type: ignore[attr-defined]


@respx.mock
async def test_unacknowledged_records_stay_pending(manager: UploadManager, storage: Storage) -> None:
    ids = fill(storage, 3)
    respx.post(ENDPOINT).respond(200, json={"accepted": [ids[0]], "duplicates": [], "rejected": []})
    await manager.upload_once()
    assert storage.stats().pending == 2


@respx.mock
async def test_conflict_is_treated_as_already_stored(manager: UploadManager, storage: Storage) -> None:
    fill(storage, 2)
    respx.post(ENDPOINT).respond(409, json={"error": "conflict"})
    assert await manager.upload_once() is Outcome.SENT
    assert storage.stats().delivered == 2


@respx.mock
async def test_malformed_acknowledgement_is_retried(manager: UploadManager, storage: Storage) -> None:
    fill(storage, 1)
    respx.post(ENDPOINT).respond(200, text="<html>proxy login page</html>")
    assert await manager.upload_once() is Outcome.RETRY
    assert storage.stats().pending == 1


@respx.mock
async def test_redirects_are_not_followed(manager: UploadManager, storage: Storage) -> None:
    fill(storage, 1)
    respx.post(ENDPOINT).respond(301, headers={"Location": "https://elsewhere.example/"})
    other = respx.post("https://elsewhere.example/").respond(200)
    assert await manager.upload_once() is Outcome.CONFIG
    assert not other.called and storage.stats().pending == 1


@respx.mock
async def test_not_found_is_a_configuration_problem(manager: UploadManager, storage: Storage, caplog) -> None:
    fill(storage, 1)
    respx.post(ENDPOINT).respond(404)
    assert await manager.upload_once() is Outcome.CONFIG
    assert "endpoint_url" in caplog.text and storage.stats().pending == 1


def test_backoff_delay_has_jitter_and_cap() -> None:
    assert backoff_delay(1, base=5, cap=600, rng=lambda: 0.0) == 2.5
    assert backoff_delay(1, base=5, cap=600, rng=lambda: 1.0) == 5
    assert backoff_delay(4, base=5, cap=600, rng=lambda: 1.0) == 40
    assert backoff_delay(50, base=5, cap=600, rng=lambda: 1.0) == 600
    assert 300 <= backoff_delay(50, base=5, cap=600) <= 600


def test_parse_retry_after() -> None:
    assert parse_retry_after("120") == 120
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", now=1445412480 - 60) == pytest.approx(60)
    assert parse_retry_after("soon") is None
    assert parse_retry_after(None) is None


def test_parse_acknowledgement_ignores_ids_not_sent() -> None:
    ack = parse_acknowledgement({"accepted": ["a", "zzz"], "duplicates": ["b"], "rejected": [{"measurement_id": "c", "reason": "x"}]}, {"a", "b", "c"})
    assert ack.accepted == {"a"} and ack.duplicates == {"b"} and ack.rejected == {"c": "x"}
    with pytest.raises(ValueError):
        parse_acknowledgement(["not", "an", "object"], set())
