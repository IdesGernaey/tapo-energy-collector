"""Configuration loading and validation.

Non-secret settings come from a YAML file (see ``config.example.yaml``).
Secrets come only from environment variables, optionally loaded from a ``.env`` file:

* ``TAPO_USERNAME`` / ``TAPO_PASSWORD`` - the TP-Link (Tapo app) account.
* ``ENERGY_API_TOKEN`` - the ingestion token issued by the CCBOM website.
* ``ENERGY_API_URL`` - optional override of ``upload.endpoint_url``.

Relative paths in the YAML file are resolved against the directory of that file.
"""

from __future__ import annotations

import ipaddress
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

DEVICE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
COLLECTOR_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
HOSTNAME_PATTERN = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


class ConfigError(ValueError):
    """Raised when the configuration is missing or invalid. The message is user facing."""


@dataclass(frozen=True)
class DeviceSettings:
    id: str
    name: str
    host: str  # initial / fallback address; the collector follows the plug when its DHCP address changes
    machine: str = ""
    category: str = ""
    polling_interval_seconds: float = 10.0
    mac: str = ""  # permanent identity "AA:BB:CC:DD:EE:FF"; empty = learned from the plug on first verified connection


@dataclass(frozen=True)
class RediscoverySettings:
    """How a plug whose address changed is found again. Every candidate is verified by MAC after login."""

    enabled: bool = True
    failures_before_rediscovery: int = 3
    min_interval_seconds: float = 30.0
    max_interval_seconds: float = 900.0
    discovery_timeout_seconds: int = 5
    broadcast_targets: tuple[str, ...] = ("255.255.255.255",)
    # Network-approved alternatives for networks that block broadcast (e.g. UGentSurfing):
    scan_networks: tuple[str, ...] = ()  # unicast UDP probe of every address in these ranges (max /22 each)
    candidate_hosts: tuple[str, ...] = ()  # addresses/hostnames to try directly (TCP login + MAC check)
    scan_concurrency: int = 32
    result_cache_seconds: float = 15.0


@dataclass(frozen=True)
class UploadSettings:
    endpoint_url: str
    batch_size: int = 200
    timeout_seconds: float = 20.0
    min_retry_seconds: float = 5.0
    max_retry_seconds: float = 600.0
    auth_failure_retry_seconds: float = 300.0
    unknown_device_retry_seconds: float = 600.0
    idle_seconds: float = 5.0


@dataclass(frozen=True)
class StorageSettings:
    database_path: Path
    delivered_retention_days: float = 7.0
    rejected_retention_days: float = 30.0
    max_pending_measurements: int = 2_000_000


@dataclass(frozen=True)
class LoggingSettings:
    directory: Path
    level: str = "INFO"
    max_bytes: int = 5_000_000
    backup_count: int = 5


@dataclass(frozen=True)
class AlertSettings:
    device_offline_after_seconds: float = 300.0
    upload_failing_after_seconds: float = 900.0
    repeat_every_seconds: float = 3600.0


@dataclass(frozen=True)
class Secrets:
    tapo_username: str
    tapo_password: str
    api_token: str

    def __repr__(self) -> str:  # never print secrets, even by accident
        return "Secrets(<redacted>)"


@dataclass(frozen=True)
class Settings:
    collector_id: str
    polling_interval_seconds: float
    device_timeout_seconds: float
    devices: tuple[DeviceSettings, ...]
    upload: UploadSettings
    storage: StorageSettings
    logging: LoggingSettings
    alerts: AlertSettings
    config_path: Path
    rediscovery: RediscoverySettings = field(default_factory=RediscoverySettings)
    status_path: Path = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "status_path", self.storage.database_path.with_name("status.json"))

    def device(self, device_id: str) -> DeviceSettings:
        for device in self.devices:
            if device.id == device_id:
                return device
        raise ConfigError(f"Unknown device id '{device_id}'. Known ids: {', '.join(d.id for d in self.devices) or '(none)'}")


# --------------------------------------------------------------------------- helpers


def _number(section: dict[str, Any], key: str, default: float, *, minimum: float, maximum: float, where: str) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}.{key} must be a number (got {value!r}).")
    if not minimum <= float(value) <= maximum:
        raise ConfigError(f"{where}.{key} must be between {minimum:g} and {maximum:g} (got {value}).")
    return float(value)


def _integer(section: dict[str, Any], key: str, default: int, *, minimum: int, maximum: int, where: str) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}.{key} must be a whole number (got {value!r}).")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{where}.{key} must be between {minimum} and {maximum} (got {value}).")
    return value


def _text(section: dict[str, Any], key: str, default: str = "", *, where: str, max_length: int = 120) -> str:
    value = section.get(key, default)
    if value is None:
        value = ""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ConfigError(f"{where}.{key} must be text (got {value!r}).")
    text = str(value).strip()
    if len(text) > max_length:
        raise ConfigError(f"{where}.{key} is longer than {max_length} characters.")
    return text


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"'{key}' must be a mapping of settings.")
    return value


def _path(value: Any, base: Path, where: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} must be a file system path.")
    path = Path(os.path.expandvars(os.path.expanduser(value.strip())))
    return path if path.is_absolute() else (base / path).resolve()


def validate_host(host: str, where: str) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not HOSTNAME_PATTERN.match(host):
            raise ConfigError(f"{where} '{host}' is not a valid IP address or hostname.") from None
        return host
    if address.version != 4:
        raise ConfigError(f"{where} must be an IPv4 address or hostname (Tapo plugs use IPv4).")
    return host


def normalize_mac(value: Any) -> str:
    """``a8-42-a1-01-02-03`` / ``A842A1010203`` -> ``A8:42:A1:01:02:03``; anything that is not a MAC -> ``""``."""
    if not isinstance(value, str):
        return ""
    digits = re.sub(r"[^0-9A-Fa-f]", "", value)
    if len(digits) != 12 or len(re.sub(r"[0-9A-Fa-f:.\- ]", "", value)) > 0:
        return ""
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2)).upper()


def _string_list(section: dict[str, Any], key: str, default: tuple[str, ...], where: str) -> tuple[str, ...]:
    value = section.get(key, list(default))
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ConfigError(f"{where}.{key} must be a list of text values.")
    return tuple(v.strip() for v in value)


MAX_SCAN_ADDRESSES = 1024


def _rediscovery(raw: dict[str, Any]) -> RediscoverySettings:
    where = "rediscovery"
    section = _section(raw, where)
    enabled = section.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("rediscovery.enabled must be true or false.")
    targets = _string_list(section, "broadcast_targets", ("255.255.255.255",), where)
    for target in targets:
        try:
            if ipaddress.ip_address(target).version != 4:
                raise ValueError
        except ValueError:
            raise ConfigError(f"rediscovery.broadcast_targets: '{target}' is not an IPv4 address (e.g. 192.168.1.255).") from None
    networks = _string_list(section, "scan_networks", (), where)
    for cidr in networks:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            raise ConfigError(f"rediscovery.scan_networks: '{cidr}' is not a network such as 192.168.1.0/24.") from None
        if network.version != 4 or network.num_addresses > MAX_SCAN_ADDRESSES:
            raise ConfigError(f"rediscovery.scan_networks: '{cidr}' must be an IPv4 range of at most {MAX_SCAN_ADDRESSES} addresses (/22).")
    candidates = tuple(validate_host(h, "rediscovery.candidate_hosts entry") for h in _string_list(section, "candidate_hosts", (), where))
    settings = RediscoverySettings(
        enabled=enabled,
        failures_before_rediscovery=_integer(section, "failures_before_rediscovery", 3, minimum=1, maximum=100, where=where),
        min_interval_seconds=_number(section, "min_interval_seconds", 30, minimum=0, maximum=86400, where=where),
        max_interval_seconds=_number(section, "max_interval_seconds", 900, minimum=1, maximum=86400, where=where),
        discovery_timeout_seconds=_integer(section, "discovery_timeout_seconds", 5, minimum=1, maximum=60, where=where),
        broadcast_targets=targets,
        scan_networks=networks,
        candidate_hosts=candidates,
        scan_concurrency=_integer(section, "scan_concurrency", 32, minimum=1, maximum=256, where=where),
        result_cache_seconds=_number(section, "result_cache_seconds", 15, minimum=0, maximum=3600, where=where),
    )
    if settings.min_interval_seconds > settings.max_interval_seconds:
        raise ConfigError("rediscovery.min_interval_seconds must not exceed rediscovery.max_interval_seconds.")
    return settings


def validate_endpoint(url: str) -> str:
    """Require HTTPS, except for local test servers on loopback."""
    parts = urlsplit(url)
    if parts.username or parts.password:
        raise ConfigError("upload.endpoint_url must not contain credentials; use ENERGY_API_TOKEN.")
    if parts.scheme == "https" and parts.hostname:
        return url
    if parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"}:
        return url
    raise ConfigError(
        "upload.endpoint_url must be an https:// URL such as https://<website>/api/energy/measurements (plain http is only allowed for localhost tests)."
    )


def parse_env_text(text: str, source: str = ".env") -> dict[str, str]:
    """Minimal ``.env`` parser: KEY=VALUE lines, # comments, optional single/double quotes (same rules as systemd)."""
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :]
        key, sep, value = stripped.partition("=")
        key = key.strip()
        if not sep or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            raise ConfigError(f"{source}:{number} is not a KEY=VALUE line.")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def parse_env_file(path: Path) -> dict[str, str]:
    return parse_env_text(path.read_text(encoding="utf-8"), str(path))


def load_env_file(path: Path | None) -> None:
    """Load a .env file into ``os.environ`` without overriding variables that are already set.

    ``-`` reads the file from standard input; the Linux ``tapo-collector`` wrapper uses this to pass the root-only
    secrets file to a process that runs as the unprivileged service user.
    """
    if path is None:
        return
    if str(path) == "-":
        values = parse_env_text(sys.stdin.read(), "<stdin>")
    elif not path.exists():
        raise ConfigError(f"Environment file {path} does not exist.")
    else:
        values = parse_env_file(path)
    for key, value in values.items():
        os.environ.setdefault(key, value)


def load_secrets(*, require_tapo: bool = True, require_token: bool = True) -> Secrets:
    username = os.environ.get("TAPO_USERNAME", "").strip()
    password = os.environ.get("TAPO_PASSWORD", "")
    token = os.environ.get("ENERGY_API_TOKEN", "").strip()
    missing = []
    if require_tapo and not username:
        missing.append("TAPO_USERNAME")
    if require_tapo and not password:
        missing.append("TAPO_PASSWORD")
    if require_token and not token:
        missing.append("ENERGY_API_TOKEN")
    if missing:
        raise ConfigError(
            "Missing secret environment variable(s): " + ", ".join(missing) + ". Put them in the collector's .env "
            "file (Linux service: /etc/tapo-collector/collector.env)."
        )
    if token and not re.match(r"^[A-Za-z0-9_\-]{32,200}$", token):
        raise ConfigError("ENERGY_API_TOKEN does not look like a token issued by the website (letters, digits, _ and -).")
    return Secrets(tapo_username=username, tapo_password=password, api_token=token)


# --------------------------------------------------------------------------- loading


def load_settings(config_path: Path) -> Settings:
    config_path = config_path.expanduser().resolve()
    if not config_path.exists():
        raise ConfigError(f"Configuration file {config_path} does not exist. Copy config.example.yaml first.")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"{config_path} is not valid YAML: {error}") from error
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} must contain a YAML mapping.")
    return settings_from_dict(raw, config_path)


def settings_from_dict(raw: dict[str, Any], config_path: Path) -> Settings:
    base = config_path.parent
    collector_id = _text(raw, "collector_id", "workshop", where="config")
    if not COLLECTOR_ID_PATTERN.match(collector_id):
        raise ConfigError("collector_id may only contain letters, digits, '.', '_' and '-' (max 64 characters).")
    interval = _number(raw, "polling_interval_seconds", 10, minimum=2, maximum=3600, where="config")
    device_timeout = _number(raw, "device_timeout_seconds", 10, minimum=2, maximum=120, where="config")

    devices_raw = raw.get("devices") or []
    if not isinstance(devices_raw, list):
        raise ConfigError("'devices' must be a list.")
    devices: list[DeviceSettings] = []
    seen_ids: set[str] = set()
    seen_hosts: dict[str, str] = {}
    seen_macs: dict[str, str] = {}
    for index, item in enumerate(devices_raw):
        where = f"devices[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} must be a mapping with id, name and host.")
        device_id = _text(item, "id", where=where, max_length=64)
        if not DEVICE_ID_PATTERN.match(device_id):
            raise ConfigError(f"{where}.id '{device_id}' must use lowercase letters, digits, '_' or '-' (e.g. printer_01).")
        if device_id in seen_ids:
            raise ConfigError(f"Device id '{device_id}' is used more than once.")
        seen_ids.add(device_id)
        host = validate_host(_text(item, "host", where=where, max_length=253), f"{where}.host")
        if host in seen_hosts:
            raise ConfigError(f"{where}.host {host} is already used by device '{seen_hosts[host]}'.")
        seen_hosts[host] = device_id
        name = _text(item, "name", device_id, where=where) or device_id
        mac_text = _text(item, "mac", where=where, max_length=32)
        mac = normalize_mac(mac_text)
        if mac_text and not mac:
            raise ConfigError(f"{where}.mac '{mac_text}' is not a MAC address such as A8:42:A1:01:02:03 (see the Tapo app: Device info).")
        if mac and mac in seen_macs:
            raise ConfigError(f"{where}.mac {mac} is already used by device '{seen_macs[mac]}'.")
        if mac:
            seen_macs[mac] = device_id
        devices.append(
            DeviceSettings(
                id=device_id,
                name=name,
                host=host,
                machine=_text(item, "machine", where=where),
                category=_text(item, "category", where=where),
                polling_interval_seconds=_number(item, "polling_interval_seconds", interval, minimum=2, maximum=3600, where=where),
                mac=mac,
            )
        )

    upload_raw = _section(raw, "upload")
    endpoint = os.environ.get("ENERGY_API_URL", "").strip() or _text(upload_raw, "endpoint_url", where="upload", max_length=500)
    if not endpoint:
        raise ConfigError("Set upload.endpoint_url in the configuration (or ENERGY_API_URL in the environment).")
    upload = UploadSettings(
        endpoint_url=validate_endpoint(endpoint),
        batch_size=_integer(upload_raw, "batch_size", 200, minimum=1, maximum=500, where="upload"),
        timeout_seconds=_number(upload_raw, "timeout_seconds", 20, minimum=1, maximum=300, where="upload"),
        min_retry_seconds=_number(upload_raw, "min_retry_seconds", 5, minimum=0.1, maximum=3600, where="upload"),
        max_retry_seconds=_number(upload_raw, "max_retry_seconds", 600, minimum=1, maximum=86400, where="upload"),
        auth_failure_retry_seconds=_number(upload_raw, "auth_failure_retry_seconds", 300, minimum=1, maximum=86400, where="upload"),
        unknown_device_retry_seconds=_number(upload_raw, "unknown_device_retry_seconds", 600, minimum=1, maximum=86400, where="upload"),
        idle_seconds=_number(upload_raw, "idle_seconds", 5, minimum=0.1, maximum=3600, where="upload"),
    )
    if upload.min_retry_seconds > upload.max_retry_seconds:
        raise ConfigError("upload.min_retry_seconds must not exceed upload.max_retry_seconds.")

    storage_raw = _section(raw, "storage")
    storage = StorageSettings(
        database_path=_path(storage_raw.get("database_path", "data/collector.sqlite"), base, "storage.database_path"),
        delivered_retention_days=_number(storage_raw, "delivered_retention_days", 7, minimum=0, maximum=3650, where="storage"),
        rejected_retention_days=_number(storage_raw, "rejected_retention_days", 30, minimum=0, maximum=3650, where="storage"),
        max_pending_measurements=_integer(storage_raw, "max_pending_measurements", 2_000_000, minimum=1000, maximum=100_000_000, where="storage"),
    )

    logging_raw = _section(raw, "logging")
    level = _text(logging_raw, "level", "INFO", where="logging").upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ConfigError("logging.level must be DEBUG, INFO, WARNING or ERROR.")
    logging_settings = LoggingSettings(
        directory=_path(logging_raw.get("directory", "logs"), base, "logging.directory"),
        level=level,
        max_bytes=_integer(logging_raw, "max_bytes", 5_000_000, minimum=10_000, maximum=1_000_000_000, where="logging"),
        backup_count=_integer(logging_raw, "backup_count", 5, minimum=1, maximum=100, where="logging"),
    )

    alerts_raw = _section(raw, "alerts")
    alerts = AlertSettings(
        device_offline_after_seconds=_number(alerts_raw, "device_offline_after_seconds", 300, minimum=10, maximum=7 * 86400, where="alerts"),
        upload_failing_after_seconds=_number(alerts_raw, "upload_failing_after_seconds", 900, minimum=10, maximum=7 * 86400, where="alerts"),
        repeat_every_seconds=_number(alerts_raw, "repeat_every_seconds", 3600, minimum=60, maximum=7 * 86400, where="alerts"),
    )

    return Settings(
        collector_id=collector_id,
        polling_interval_seconds=interval,
        device_timeout_seconds=device_timeout,
        devices=tuple(devices),
        upload=upload,
        storage=storage,
        logging=logging_settings,
        alerts=alerts,
        config_path=config_path,
        rediscovery=_rediscovery(raw),
    )
