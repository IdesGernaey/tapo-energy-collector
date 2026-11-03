#!/usr/bin/env python3
"""One-shot health check for a new installation (read-only; never switches a plug).

Checks, in order:
  1. configuration and secrets are valid,
  2. the website accepts the API token (an empty batch is posted),
  3. every configured plug can be read once.

Usage (from the repository, with the virtual environment active):
    python scripts/test_connection.py --config config.yaml --env-file .env
On the Linux service installation use the equivalent built-in commands instead:
    sudo tapo-collector check-config && sudo tapo-collector test-upload && sudo tapo-collector test-device --device <id>
Exit code 0 = everything OK, 1 = some plugs failed, 2 = configuration or website problem.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from tapo_collector.config import ConfigError, load_env_file, load_secrets, load_settings
from tapo_collector.devices import DeviceReadError, kasa_credentials
from tapo_collector.discovery import test_device
from tapo_collector.uploader import ApiClient, Outcome


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    try:
        load_env_file(args.env_file if args.env_file.exists() else None)
        settings = load_settings(args.config)
        secrets = load_secrets()
    except ConfigError as error:
        print(f"[FAIL] configuration: {error}")
        return 2
    print(f"[ OK ] configuration {settings.config_path} ({len(settings.devices)} plug(s))")

    client = ApiClient(settings.upload, secrets.api_token)
    try:
        result = await client.send(settings.collector_id, [])
    finally:
        await client.close()
    if result.outcome is not Outcome.SENT:
        print(f"[FAIL] website {settings.upload.endpoint_url}: HTTP {result.status or '-'} {result.message}")
        return 2
    print(f"[ OK ] website accepted the token ({settings.upload.endpoint_url})")

    credentials = kasa_credentials(secrets.tapo_username, secrets.tapo_password)
    failed = 0
    for device in settings.devices:
        plug, results = await test_device(device, credentials, count=1, timeout=settings.device_timeout_seconds)
        reading = results[0] if results else None
        if isinstance(reading, DeviceReadError) or reading is None:
            failed += 1
            print(f"[FAIL] {device.id} at {device.host}: {reading}")
        else:
            m = reading.metrics
            print(f"[ OK ] {device.id} at {device.host} ({plug.model}): {m['power_w']} W, today {m['energy_today_kwh']} kWh")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
