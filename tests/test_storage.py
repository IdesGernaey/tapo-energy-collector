from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from tapo_collector.measurements import offline_measurement, online_measurement
from tapo_collector.storage import Storage

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def reading(i: int, device: str = "printer_01"):
    return online_measurement("c", device, T0 + timedelta(seconds=10 * i), {"power_w": float(i), "energy_today_kwh": 0.5})


def test_saved_readings_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "q.sqlite"
    store = Storage(path)
    for i in range(5):
        assert store.save(reading(i))
    store.save(offline_measurement("c", "soldering_01", T0))
    store.close()

    reopened = Storage(path)  # simulates a collector restart / computer reboot
    batch = reopened.next_batch(100)
    assert len(batch) == 6
    assert [m.power_w for m in batch if m.device_id == "printer_01"] == [0.0, 1.0, 2.0, 3.0, 4.0]
    assert next(m for m in batch if m.device_id == "soldering_01").online is False
    assert all(m.timestamp.tzinfo == UTC for m in batch)
    reopened.close()


def test_duplicate_ids_are_ignored(storage: Storage) -> None:
    assert storage.save(reading(1)) is True
    assert storage.save(reading(1)) is False
    assert storage.stats().pending == 1


def test_batches_are_oldest_first_and_limited(storage: Storage) -> None:
    for i in reversed(range(10)):
        storage.save(reading(i))
    batch = storage.next_batch(3)
    assert [m.power_w for m in batch] == [0.0, 1.0, 2.0]


def test_delivered_rows_leave_the_queue(storage: Storage) -> None:
    items = [reading(i) for i in range(4)]
    for m in items:
        storage.save(m)
    assert storage.mark_delivered([items[0].measurement_id, items[1].measurement_id]) == 2
    stats = storage.stats()
    assert (stats.pending, stats.delivered) == (2, 2)
    assert [m.measurement_id for m in storage.next_batch(10)] == [items[2].measurement_id, items[3].measurement_id]


def test_rejected_and_deferred_rows(storage: Storage) -> None:
    items = [reading(i) for i in range(3)]
    for m in items:
        storage.save(m)
    storage.mark_rejected({items[0].measurement_id: "invalid_value"})
    storage.defer([items[1].measurement_id], until=10_000_000_000, error="unknown device")
    assert [m.measurement_id for m in storage.next_batch(10)] == [items[2].measurement_id]
    stats = storage.stats()
    assert (stats.pending, stats.rejected, stats.deferred) == (2, 1, 1)
    assert storage.recent_rejections()[0]["last_error"] == "invalid_value"
    assert storage.release_deferred() == 1
    assert storage.requeue_rejected() == 1
    assert len(storage.next_batch(10)) == 3


def test_prune_keeps_pending(storage: Storage) -> None:
    items = [reading(i) for i in range(3)]
    for m in items:
        storage.save(m, now=1000)
    storage.mark_delivered([items[0].measurement_id], now=1000)
    storage.mark_rejected({items[1].measurement_id: "bad"})
    removed = storage.prune(delivered_days=1, rejected_days=1, now=1000 + 2 * 86400)
    assert removed == 2
    stats = storage.stats()
    assert (stats.pending, stats.delivered, stats.rejected) == (1, 0, 0)


def test_queue_limit_drops_oldest_pending_with_error(tmp_path: Path, caplog) -> None:
    store = Storage(tmp_path / "q.sqlite", max_pending=3)
    for i in range(5):
        store.save(reading(i))
    assert [m.power_w for m in store.next_batch(10)] == [2.0, 3.0, 4.0]
    assert "queue limit" in caplog.text
    store.close()


def test_connection_cache_round_trip(storage: Storage) -> None:
    config = {"host": "192.0.2.10", "connection_type": {"device_family": "SMART.TAPOPLUG", "encryption_type": "KLAP"}}
    storage.set_connection("printer_01", "192.0.2.10", config)
    assert storage.get_connection("printer_01", "192.0.2.10") == config
    assert storage.get_connection("printer_01", "192.0.2.99") is None  # host changed in config: ignore cache
    storage.forget_connection("printer_01")
    assert storage.get_connection("printer_01", "192.0.2.10") is None
