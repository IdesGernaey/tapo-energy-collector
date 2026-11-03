"""Command line interface: ``tapo-collector <command>``.

Commands
  run               run the collector (what the service starts)
  discover          broadcast discovery of Tapo plugs on the local subnet
  test-device       read one plug a few times (by --device ID from the config, or --host)
  test-upload       check the website endpoint and API token without sending data
  check-config      validate the configuration and secrets
  status            show queue, upload and device state (works while the service runs)
  retry-now         make readings that wait for an unregistered device due immediately
  requeue-rejected  put permanently rejected readings back into the upload queue
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from tapo_collector import __version__
from tapo_collector.config import (
    ConfigError,
    DeviceSettings,
    Settings,
    load_env_file,
    load_secrets,
    load_settings,
    validate_host,
)
from tapo_collector.logging_config import configure_logging

log = logging.getLogger("tapo_collector")

EXIT_OK, EXIT_WARNING, EXIT_ERROR = 0, 1, 2


def _default_config() -> Path:
    return Path(os.environ.get("TAPO_COLLECTOR_CONFIG", "config.yaml"))


def _default_env_file() -> Path | None:
    value = os.environ.get("TAPO_COLLECTOR_ENV_FILE")
    if value:
        return Path(value)
    return Path(".env") if Path(".env").exists() else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tapo-collector", description="UGent Racing Tapo P110 energy collector (read-only).")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-c", "--config", type=Path, default=None, help="YAML configuration (default: $TAPO_COLLECTOR_CONFIG or ./config.yaml)")
    parser.add_argument("--env-file", type=Path, default=None, help="file with secrets (default: $TAPO_COLLECTOR_ENV_FILE or ./.env if present)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="run the collector")

    p = sub.add_parser("discover", help="find Tapo plugs by UDP broadcast")
    p.add_argument("--target", default="255.255.255.255", help="broadcast address, e.g. 192.168.1.255")
    p.add_argument("--timeout", type=int, default=5)

    p = sub.add_parser("test-device", help="read one plug a few times (never switches it)")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--device", help="device id from the configuration")
    group.add_argument("--host", help="IP address or hostname of a plug that is not configured yet")
    p.add_argument("--count", type=int, default=3)
    p.add_argument("--interval", type=float, default=2.0)

    sub.add_parser("test-upload", help="check the endpoint and token (sends an empty batch)")
    sub.add_parser("check-config", help="validate configuration and secrets")
    p = sub.add_parser("status", help="show collector state")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub.add_parser("retry-now", help="retry readings deferred for unregistered devices now")
    sub.add_parser("requeue-rejected", help="queue permanently rejected readings again")
    return parser


def _settings(args: argparse.Namespace) -> Settings:
    load_env_file(args.env_file or _default_env_file())
    return load_settings(args.config or _default_config())


def _setup_logging(args: argparse.Namespace, settings: Settings | None = None, *, to_file: bool = False) -> None:
    level = "DEBUG" if args.verbose else (settings.logging.level if settings else "INFO")
    if settings is not None and to_file:
        configure_logging(
            level=level,
            directory=settings.logging.directory,
            max_bytes=settings.logging.max_bytes,
            backup_count=settings.logging.backup_count,
        )
    else:
        configure_logging(level=level)


# ----------------------------------------------------------------------------- commands


def cmd_run(args: argparse.Namespace) -> int:
    from tapo_collector.service import Collector, install_signal_handlers

    settings = _settings(args)
    _setup_logging(args, settings, to_file=True)
    secrets = load_secrets()

    async def main() -> None:
        stop = asyncio.Event()
        install_signal_handlers(stop)
        collector = Collector(settings, secrets)
        await collector.run(stop)

    asyncio.run(main())
    return EXIT_OK


def cmd_discover(args: argparse.Namespace) -> int:
    from tapo_collector.devices import kasa_credentials
    from tapo_collector.discovery import discover_network, yaml_snippet

    load_env_file(args.env_file or _default_env_file())
    _setup_logging(args)
    secrets = load_secrets(require_token=False)
    print(f"Broadcasting discovery to {args.target} for {args.timeout} s ...")
    found = asyncio.run(discover_network(args.target, kasa_credentials(secrets.tapo_username, secrets.tapo_password), args.timeout))
    if not found:
        print(
            "No devices answered. Broadcast discovery is often blocked on university/managed networks.\n"
            "Find each plug's IP address in the Tapo app (plug > settings > Device info) or in the router's\n"
            "DHCP client list, then run:  tapo-collector test-device --host <ip>"
        )
        return EXIT_WARNING
    print(f"\n{'HOST':<16} {'MODEL':<10} {'ENERGY':<7} {'MAC':<18} NAME / ERROR")
    for d in found:
        energy = "yes" if d.energy else ("no" if d.energy is False else "?")
        print(f"{d.host:<16} {d.model:<10} {energy:<7} {d.mac:<18} {d.error or d.alias}")
    if any("TPAP" in d.error for d in found):
        print(
            "\nPlugs marked 'TPAP' run newer firmware (P110 1.4.x) whose login python-kasa does not support yet\n"
            "(https://github.com/python-kasa/python-kasa/issues/1590). They cannot be read until python-kasa adds it;\n"
            "turn off automatic firmware updates in the Tapo app for plugs that still work."
        )
    usable = [d for d in found if d.energy and not d.error]
    if not usable:
        print("\nNo readable energy-monitoring plugs were found, so there is no configuration to suggest.")
        return EXIT_WARNING
    print("\nSuggested configuration (check the ids and names, then copy into config.yaml):\n")
    print(yaml_snippet(found))
    return EXIT_OK


def cmd_test_device(args: argparse.Namespace) -> int:
    from tapo_collector.devices import DeviceReadError, kasa_credentials
    from tapo_collector.discovery import test_device

    load_env_file(args.env_file or _default_env_file())
    _setup_logging(args)
    secrets = load_secrets(require_token=False)
    if args.device:
        settings = load_settings(args.config or _default_config())
        device = settings.device(args.device)
        timeout = settings.device_timeout_seconds
    else:
        device = DeviceSettings(id="test", name="test", host=validate_host(args.host, "--host"))
        timeout = 10.0
    print(f"Reading {device.host} {args.count} time(s); the plug is not switched.")
    plug, results = asyncio.run(
        test_device(device, kasa_credentials(secrets.tapo_username, secrets.tapo_password), count=args.count, interval=args.interval, timeout=timeout)
    )
    if plug.model:
        print(f"Connected: model {plug.model}, name in Tapo app '{plug.alias}'")
    failures = 0
    for index, result in enumerate(results, start=1):
        if isinstance(result, DeviceReadError):
            failures += 1
            print(f"  #{index}: FAILED ({result.kind}) {result}")
            continue
        m = result.metrics
        fmt = lambda v, unit, digits: "n/a" if v is None else f"{v:.{digits}f} {unit}"  # noqa: E731
        print(
            f"  #{index}: {result.timestamp.isoformat(timespec='milliseconds')}  power {fmt(m['power_w'], 'W', 1)}, "
            f"today {fmt(m['energy_today_kwh'], 'kWh', 3)}, month {fmt(m['energy_month_kwh'], 'kWh', 3)}, "
            f"voltage {fmt(m['voltage_v'], 'V', 1)}, current {fmt(m['current_a'], 'A', 3)}  ({result.latency_seconds:.2f} s)"
        )
    if failures == len(results):
        return EXIT_ERROR
    return EXIT_WARNING if failures else EXIT_OK


def cmd_test_upload(args: argparse.Namespace) -> int:
    from tapo_collector.uploader import ApiClient, Outcome

    settings = _settings(args)
    _setup_logging(args, settings)
    secrets = load_secrets(require_tapo=False)

    async def check() -> int:
        client = ApiClient(settings.upload, secrets.api_token)
        try:
            result = await client.send(settings.collector_id, [])
        finally:
            await client.close()
        if result.outcome is Outcome.SENT:
            print(f"OK: {settings.upload.endpoint_url} accepted the token (HTTP {result.status}).")
            return EXIT_OK
        print(f"FAILED: {result.outcome.value} - HTTP {result.status or '-'}: {result.message}")
        return EXIT_ERROR

    return asyncio.run(check())


def cmd_check_config(args: argparse.Namespace) -> int:
    settings = _settings(args)
    load_secrets()
    print(f"Configuration OK: {settings.config_path}")
    print(f"  collector id      {settings.collector_id}")
    print(f"  endpoint          {settings.upload.endpoint_url}")
    print(f"  database          {settings.storage.database_path}")
    print(f"  logs              {settings.logging.directory}")
    print(f"  devices           {len(settings.devices)}")
    for d in settings.devices:
        extra = f", machine '{d.machine}'" if d.machine else ""
        print(f"    - {d.id:<20} {d.host:<16} every {d.polling_interval_seconds:g} s  ({d.name}{extra})")
    print("  secrets           TAPO_USERNAME, TAPO_PASSWORD and ENERGY_API_TOKEN are set (values not shown)")
    return EXIT_OK


def _age(iso: str | None) -> float | None:
    if not iso:
        return None
    return time.time() - datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def cmd_status(args: argparse.Namespace) -> int:
    from tapo_collector.storage import Storage

    settings = _settings(args)
    report: dict[str, Any] = {}
    if settings.status_path.exists():
        report = json.loads(settings.status_path.read_text(encoding="utf-8"))
    storage = Storage(settings.storage.database_path, max_pending=settings.storage.max_pending_measurements)
    try:
        report["queue"] = storage.stats().as_dict()
        report["recent_rejections"] = storage.recent_rejections(5)
    finally:
        storage.close()
    if args.json:
        print(json.dumps(report, indent=2))
    problems: list[str] = []
    status_age = _age(report.get("updated_at"))
    if status_age is None or status_age > 120:
        problems.append("the collector is not running (status file is missing or older than 2 minutes)")
    upload = report.get("upload", {})
    if upload.get("failing_since"):
        problems.append(f"uploads failing since {upload['failing_since']}: {upload.get('last_error')}")
    for device in report.get("devices", []):
        if device.get("offline_since"):
            problems.append(f"device {device['device_id']} offline since {device['offline_since']}: {device.get('last_error')}")
    if not args.json:
        q = report["queue"]
        print(f"Collector       {report.get('collector_id', settings.collector_id)}  (status updated {report.get('updated_at', 'never')})")
        kept = settings.storage.delivered_retention_days
        print(f"Queue           {q['pending']} pending ({q['deferred']} deferred), {q['delivered']} delivered (kept {kept:g} days), {q['rejected']} rejected")
        if q["oldest_pending"]:
            print(f"Oldest pending  {q['oldest_pending']}")
        print(f"Last upload OK  {upload.get('last_success') or 'never (this run)'}")
        for device in report.get("devices", []):
            state = "online " if device.get("online") else "OFFLINE"
            power = device.get("last_power_w")
            power_text = f"{power:.1f} W" if isinstance(power, (int, float)) else "-"
            print(f"  {state} {device['device_id']:<20} {device['host']:<16} {power_text:>10}  last ok {device.get('last_success') or '-'}")
        for item in report.get("recent_rejections", []):
            print(f"  rejected {item['measurement_id']} ({item['device_id']} {item['timestamp']}): {item['last_error']}")
        for problem in problems:
            print(f"WARNING: {problem}")
        if not problems:
            print("Everything looks healthy.")
    return EXIT_WARNING if problems else EXIT_OK


def cmd_retry_now(args: argparse.Namespace) -> int:
    from tapo_collector.storage import Storage

    settings = _settings(args)
    storage = Storage(settings.storage.database_path)
    try:
        print(f"{storage.release_deferred()} deferred reading(s) are due now; the running collector sends them within seconds.")
    finally:
        storage.close()
    return EXIT_OK


def cmd_requeue_rejected(args: argparse.Namespace) -> int:
    from tapo_collector.storage import Storage

    settings = _settings(args)
    storage = Storage(settings.storage.database_path)
    try:
        print(f"{storage.requeue_rejected()} rejected reading(s) queued again.")
    finally:
        storage.close()
    return EXIT_OK


COMMANDS = {
    "run": cmd_run,
    "discover": cmd_discover,
    "test-device": cmd_test_device,
    "test-upload": cmd_test_upload,
    "check-config": cmd_check_config,
    "status": cmd_status,
    "retry-now": cmd_retry_now,
    "requeue-rejected": cmd_requeue_rejected,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
