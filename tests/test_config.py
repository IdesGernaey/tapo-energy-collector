from __future__ import annotations

import os
from pathlib import Path

import pytest

from tapo_collector.config import ConfigError, load_env_file, load_secrets, load_settings, parse_env_file


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_example_configuration_is_valid() -> None:
    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    settings = load_settings(example)
    assert [d.id for d in settings.devices] == ["printer_01", "soldering_01"]
    assert settings.devices[0].polling_interval_seconds == 10
    assert settings.storage.database_path.is_absolute()


def test_relative_paths_resolve_next_to_config(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, "upload: {endpoint_url: 'https://x.example/api/energy/measurements'}\n"))
    assert settings.storage.database_path == (tmp_path / "data" / "collector.sqlite").resolve()
    assert settings.status_path.name == "status.json"


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        ("upload: {endpoint_url: 'http://x.example/api'}", "https"),
        ("upload: {endpoint_url: 'https://user:pw@x.example/api'}", "credentials"),
        ("upload: {}", "endpoint_url"),
        ("upload: {endpoint_url: 'https://x.example/a'}\ndevices: [{id: Printer 1, host: 192.0.2.1}]", "lowercase"),
        ("upload: {endpoint_url: 'https://x.example/a'}\ndevices: [{id: a, host: 192.0.2.1}, {id: a, host: 192.0.2.2}]", "more than once"),
        ("upload: {endpoint_url: 'https://x.example/a'}\ndevices: [{id: a, host: 192.0.2.1}, {id: b, host: 192.0.2.1}]", "already used"),
        ("upload: {endpoint_url: 'https://x.example/a'}\ndevices: [{id: a, host: 'not a host!'}]", "not a valid"),
        ("upload: {endpoint_url: 'https://x.example/a'}\npolling_interval_seconds: 0.5", "between"),
        ("upload: {endpoint_url: 'https://x.example/a', batch_size: 1000}", "between"),
        ("- just\n- a list", "mapping"),
    ],
)
def test_invalid_configurations_are_explained(tmp_path: Path, yaml_text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_settings(write(tmp_path, yaml_text))


def test_localhost_http_is_allowed_for_tests(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, "upload: {endpoint_url: 'http://localhost:3000/api/energy/measurements'}"))
    assert settings.upload.endpoint_url.startswith("http://localhost")


def test_env_url_overrides_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENERGY_API_URL", "https://override.example/api/energy/measurements")
    settings = load_settings(write(tmp_path, "upload: {endpoint_url: 'https://x.example/a'}"))
    assert settings.upload.endpoint_url == "https://override.example/api/energy/measurements"


def test_env_file_parsing_and_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# comment\nTAPO_USERNAME=team@example.test\nexport TAPO_PASSWORD='p@ss word#1'\nENERGY_API_TOKEN=\"tec_abcdefghijklmnopqrstuvwxyz0123456789ABCD\"\n\n",
        encoding="utf-8",
    )
    assert parse_env_file(env)["TAPO_PASSWORD"] == "p@ss word#1"
    load_env_file(env)
    secrets = load_secrets()
    assert secrets.tapo_username == "team@example.test"
    assert "p@ss" not in repr(secrets)
    for key in ("TAPO_USERNAME", "TAPO_PASSWORD", "ENERGY_API_TOKEN"):
        os.environ.pop(key, None)


def test_missing_secrets_are_named(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match="TAPO_USERNAME, TAPO_PASSWORD, ENERGY_API_TOKEN"):
        load_secrets()
    monkeypatch.setenv("ENERGY_API_TOKEN", "short")
    monkeypatch.setenv("TAPO_USERNAME", "u")
    monkeypatch.setenv("TAPO_PASSWORD", "p")
    with pytest.raises(ConfigError, match="does not look like"):
        load_secrets()


def test_env_file_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("TAPO_USERNAME=stdin@example.test" + chr(10) + "TAPO_PASSWORD='with $pecial chars & spaces'" + chr(10)))
    load_env_file(Path("-"))
    assert os.environ.pop("TAPO_USERNAME") == "stdin@example.test"
    assert os.environ.pop("TAPO_PASSWORD") == "with $pecial chars & spaces"
