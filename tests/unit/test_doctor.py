from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from by2kb import cli
from by2kb.config import Config, SourceConfig
from by2kb.doctor import DoctorCheck, _directory_check, run_doctor


def _config(tmp_path: Path, *, provider: str = "faster_whisper") -> Config:
    return Config(
        home=tmp_path / "home",
        library_root=tmp_path / "library",
        db_path=tmp_path / "home" / "jobs.db",
        asr_provider=provider,
        enrichment_executor="disabled",
    )


def _passing_system_checks(monkeypatch):
    monkeypatch.setattr(
        "by2kb.doctor._command_check",
        lambda check_id, command: DoctorCheck(check_id, True, f"{command} works"),
    )
    monkeypatch.setattr(
        "by2kb.doctor._directory_check",
        lambda check_id, path, label: DoctorCheck(check_id, True, f"{label} works"),
    )
    monkeypatch.setattr(
        "by2kb.doctor._database_check",
        lambda path: DoctorCheck("database_writable", True, "database works"),
    )


def test_doctor_success_schema_is_agent_readable(tmp_path, monkeypatch):
    _passing_system_checks(monkeypatch)
    monkeypatch.setattr(
        "by2kb.doctor.faster_whisper_status",
        lambda _config: {
            "dependency_installed": True,
            "model_installed": True,
            "model": "large-v3-turbo",
        },
    )

    payload = run_doctor(_config(tmp_path)).to_dict()

    assert payload["schema_version"] == 1
    assert payload["ok"] is True
    assert payload["provider"] == "faster_whisper"
    assert all(set(check) == {"id", "ok", "message", "remediation"} for check in payload["checks"])


def test_doctor_reports_missing_local_dependency(tmp_path, monkeypatch):
    _passing_system_checks(monkeypatch)
    monkeypatch.setattr(
        "by2kb.doctor.faster_whisper_status",
        lambda _config: {
            "dependency_installed": False,
            "model_installed": True,
            "model": "large-v3-turbo",
        },
    )

    report = run_doctor(_config(tmp_path))
    dependency = next(check for check in report.checks if check.id == "asr_dependency")

    assert report.ok is False
    assert dependency.ok is False
    assert "pipx inject" in dependency.remediation


def test_directory_check_reports_unwritable_destination(tmp_path, monkeypatch):
    destination = tmp_path / "library"
    destination.mkdir()
    monkeypatch.setattr("by2kb.doctor.os.access", lambda _path, _mode: False)

    check = _directory_check("library_writable", destination, "Library")

    assert check.ok is False
    assert "grant write permission" in check.remediation


def test_doubao_report_never_contains_secret_values(tmp_path, monkeypatch):
    _passing_system_checks(monkeypatch)
    secrets = {
        "VOLC_ACCESS_KEY_ID": "secret-access-id",
        "VOLC_SECRET_ACCESS_KEY": "secret-access-key",
        "TOS_BUCKET": "private-bucket",
        "TOS_S3_ENDPOINT": "https://tos.example.test",
        "DOUBAO_API_KEY": "secret-doubao-key",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("by2kb.doctor.find_spec", lambda _name: object())
    monkeypatch.setattr("by2kb.doctor._tos_accessible", lambda: True)

    rendered = json.dumps(run_doctor(_config(tmp_path, provider="doubao_auc")).to_dict())

    assert all(value not in rendered for value in secrets.values())


def test_doctor_json_cli_uses_stable_schema(tmp_path, monkeypatch):
    _passing_system_checks(monkeypatch)
    monkeypatch.setattr(
        "by2kb.doctor.faster_whisper_status",
        lambda _config: {
            "dependency_installed": True,
            "model_installed": True,
            "model": "large-v3-turbo",
        },
    )
    monkeypatch.setattr(cli, "load_config", lambda: _config(tmp_path))

    result = CliRunner().invoke(cli.app, ["doctor", "--json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["schema_version"] == 1
    assert payload["ok"] is True


def test_doctor_reports_missing_configured_ytdlp_dependency(tmp_path, monkeypatch):
    _passing_system_checks(monkeypatch)
    config = _config(tmp_path)
    config.sources = SourceConfig(providers=["bilibili_native", "yt_dlp"])
    monkeypatch.setattr(
        "by2kb.doctor.faster_whisper_status",
        lambda _config: {
            "dependency_installed": True,
            "model_installed": True,
            "model": "large-v3-turbo",
        },
    )
    monkeypatch.setattr(
        "by2kb.doctor.find_spec",
        lambda name: None if name == "yt_dlp" else object(),
    )

    report = run_doctor(config)
    check = next(
        item for item in report.checks if item.id == "source_yt_dlp_dependency"
    )

    assert report.source_providers == ("bilibili_native", "yt_dlp")
    assert check.ok is False
    assert "pipx inject" in check.remediation


def test_interactive_init_supports_local_whisper(tmp_path):
    home = tmp_path / "local-home"
    answers = "\n\n\n\n\n\ndisabled\n"

    result = CliRunner().invoke(
        cli.app,
        ["init", "--home", str(home)],
        input=answers,
    )

    assert result.exit_code == 0, result.stdout
    rendered = (home / "config.toml").read_text(encoding="utf-8")
    assert 'provider = "faster_whisper"' in rendered
    assert 'executor = "disabled"' in rendered
    assert "by2kb doctor" in result.stdout


def test_interactive_init_supports_cloud_doubao_without_echoing_secrets(tmp_path):
    home = tmp_path / "cloud-home"
    answers = (
        "\n"
        "\n"
        "doubao\n"
        "tos-access-id\n"
        "tos-secret-value\n"
        "private-bucket\n"
        "\n"
        "\n"
        "\n"
        "doubao-secret-key\n"
        "disabled\n"
    )

    result = CliRunner().invoke(
        cli.app,
        ["init", "--home", str(home)],
        input=answers,
    )

    assert result.exit_code == 0, result.stdout
    assert 'provider = "doubao_auc"' in (home / "config.toml").read_text(
        encoding="utf-8"
    )
    assert "tos-secret-value" not in result.stdout
    assert "doubao-secret-key" not in result.stdout


def test_interactive_init_can_enable_youtube_source(tmp_path):
    home = tmp_path / "youtube-home"
    answers = "\nbilibili+youtube\n\n\n\n\n\ndisabled\n"

    result = CliRunner().invoke(
        cli.app,
        ["init", "--home", str(home)],
        input=answers,
    )

    assert result.exit_code == 0, result.stdout
    rendered = (home / "config.toml").read_text(encoding="utf-8")
    assert 'providers = ["bilibili_native", "yt_dlp"]' in rendered
    assert '[sources.yt_dlp]' in rendered
    assert "pipx inject by2kb 'yt-dlp" in result.stdout
