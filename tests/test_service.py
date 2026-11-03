"""End-to-end behaviour of the collector process with fake plugs and a fake website."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx
import respx

from conftest import ENDPOINT, TOKEN
from tapo_collector.config import DeviceSettings
from tapo_collector.devices import DeviceReadError, Reading
from tapo_collector.service import Collector
from tapo_collector.storage import Storage
from tapo_collector.uploader import ApiClient


class FakePlug:
    def __init__(self, settings: DeviceSettings, *, online: bool = True) -> None:
        self.settings = settings
        self.online = online
        self.reads = 0
        self.closed = False

    async def read(self) -> Reading:
        self.reads += 1
        if not self.online:
            raise DeviceReadError(f"{self.settings.host}: no answer", "unreachable")
        return Reading(datetime.now(UTC), {"power_w": 100.0 + self.reads, "energy_today_kwh": 0.5}, 0.01)

    async def close(self) -> None:
        self.closed = True


class FakeWebsite:
    """Idempotent server: stores each measurement ID once, answers duplicates for repeats."""

    def __init__(self) -> None:
        self.up = True
        self.stored: dict[str, dict[str, Any]] = {}
        self.requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if not self.up:
            raise httpx.ConnectError("no internet")
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        accepted, duplicates = [], []
        for m in json.loads(request.content)["measurements"]:
            (duplicates if m["measurement_id"] in self.stored else accepted).append(m["measurement_id"])
            self.stored.setdefault(m["measurement_id"], m)
        return httpx.Response(200, json={"accepted": accepted, "duplicates": duplicates, "rejected": []})


def build(settings, secrets, plugs) -> Collector:
    storage = Storage(settings.storage.database_path)
    return Collector(settings, secrets, storage=storage, plugs=plugs, client=ApiClient(settings.upload, TOKEN))


async def run_for(collector: Collector, seconds: float) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(collector.run(stop))
    await asyncio.sleep(seconds)
    stop.set()
    await asyncio.wait_for(task, 20)


@respx.mock
async def test_outage_then_restart_delivers_everything_exactly_once(make_settings, secrets) -> None:
    settings = make_settings(polling_interval_seconds=2, upload={"min_retry_seconds": 0.2, "max_retry_seconds": 1, "idle_seconds": 0.1})
    website = FakeWebsite()
    respx.post(ENDPOINT).mock(side_effect=website)

    # 1) Internet down: readings are collected and kept locally; one plug is unplugged.
    website.up = False
    plugs = [FakePlug(settings.devices[0]), FakePlug(settings.devices[1], online=False)]
    first = build(settings, secrets, plugs)
    await run_for(first, 2.5)
    assert all(p.closed for p in plugs)
    check = Storage(settings.storage.database_path)
    queued = check.stats().pending
    check.close()
    assert queued >= 4  # 2+ polls per plug, including offline markers
    assert website.stored == {}

    # 2) Collector restarted (e.g. after a reboot) with the internet back: the backlog is uploaded.
    website.up = True
    second = build(settings, secrets, [FakePlug(settings.devices[0]), FakePlug(settings.devices[1])])
    await run_for(second, 1.5)
    check = Storage(settings.storage.database_path)
    stats = check.stats()
    check.close()
    assert stats.pending == 0
    assert stats.delivered == len(website.stored) >= queued
    offline = [m for m in website.stored.values() if m["device_id"] == "soldering_01" and not m["online"]]
    assert offline and all(m["power_w"] is None for m in offline)  # nothing invented for the offline plug


@respx.mock
async def test_status_file_and_alerts(make_settings, secrets, caplog) -> None:
    settings = make_settings(alerts={"device_offline_after_seconds": 10, "upload_failing_after_seconds": 10})
    website = FakeWebsite()
    website.up = False
    respx.post(ENDPOINT).mock(side_effect=website)
    now = [1_800_000_000.0]
    collector = Collector(
        settings,
        secrets,
        storage=Storage(settings.storage.database_path),
        plugs=[FakePlug(settings.devices[0], online=False)],
        client=ApiClient(settings.upload, TOKEN),
        clock=lambda: now[0],
    )
    await collector.pollers[0].poll_once()
    await collector.uploader.upload_once()
    now[0] += 60
    with caplog.at_level(logging.ERROR):
        collector.check_alerts()
    assert "ALERT device printer_01" in caplog.text
    assert "ALERT uploads to the website have failed" in caplog.text
    collector.write_status()
    status = json.loads(settings.status_path.read_text(encoding="utf-8"))
    assert status["devices"][0]["online"] is False
    assert status["upload"]["consecutive_failures"] == 1
    assert status["queue"]["pending"] == 1
    assert TOKEN not in settings.status_path.read_text(encoding="utf-8")
    await collector.close()
