import json
from types import SimpleNamespace

import pytest

from by2kb.agent_install import install_hermes_plugin
from by2kb.errors import ConfigError
from by2kb.integrations import hermes


@pytest.mark.parametrize("marker", ["metadata", "catalog", "git", "corrupt", "invalid"])
def test_force_preserves_hermes_owned_code(tmp_path, marker):
    target = tmp_path / "plugins" / "by2kb"
    target.mkdir(parents=True)
    original = target / "__init__.py"
    original.write_text("# reviewed code", encoding="utf-8")
    metadata = target.parent / ".install-metadata.json"
    if marker == "metadata":
        metadata.write_text(json.dumps({"by2kb": {"pinned": True}}), encoding="utf-8")
    elif marker == "catalog":
        (target / ".hermes-catalog.json").write_text("{}", encoding="utf-8")
    elif marker == "git":
        (target / ".git").mkdir()
    elif marker == "corrupt":
        metadata.write_text("{", encoding="utf-8")
    else:
        metadata.write_text("[]", encoding="utf-8")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ConfigError):
        install_hermes_plugin(hermes_home=tmp_path, force=True, enable=False)
    assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_metadata_pin_is_protected_even_without_plugin_directory(tmp_path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / ".install-metadata.json").write_text('{"by2kb": {}}', encoding="utf-8")
    with pytest.raises(ConfigError, match="managed by Hermes"):
        install_hermes_plugin(hermes_home=tmp_path, force=True, enable=False)
    assert not (plugins / "by2kb").exists()


def test_legacy_refresh_keeps_other_plugins_and_user_state(tmp_path):
    home = tmp_path / "hermes"
    target = install_hermes_plugin(hermes_home=home, enable=False)
    metadata = home / "plugins" / ".install-metadata.json"
    metadata.write_text('{"other": {"pinned": true}}', encoding="utf-8")
    state = tmp_path / "user-config.toml"
    state.write_text("personal settings", encoding="utf-8")
    (target / "old-file.txt").write_text("old code", encoding="utf-8")
    install_hermes_plugin(hermes_home=home, force=True, enable=False)
    assert not (target / "old-file.txt").exists()
    assert (target / "skills/install-by2kb/SKILL.md").is_file()
    assert state.read_text(encoding="utf-8") == "personal settings"
    assert json.loads(metadata.read_text())["other"]["pinned"]


def test_enable_uses_requested_profile(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("by2kb.agent_install.shutil.which", lambda _: "hermes")
    monkeypatch.setattr("by2kb.agent_install.subprocess.run", lambda *a, **kw: (
        calls.append((a, kw)) or SimpleNamespace(returncode=0)))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "unrelated"))
    selected = tmp_path / "selected"
    install_hermes_plugin(hermes_home=selected)
    assert calls[0][1]["env"]["HERMES_HOME"] == str(selected.resolve())


def test_plugin_registers_both_readable_skills_and_hook():
    skills, hooks = {}, {}
    ctx = SimpleNamespace(
        register_skill=lambda name, path, **kw: skills.update({name: path}),
        register_hook=lambda name, callback: hooks.update({name: callback}),
    )
    hermes.register(ctx)
    assert set(skills) == {"install-by2kb", "video-to-knowledge"}
    assert all(path.is_file() and path.read_text(encoding="utf-8") for path in skills.values())
    assert callable(hooks["pre_gateway_dispatch"])


def test_missing_cli_points_to_bundled_setup(monkeypatch):
    monkeypatch.delenv("BY2KB_COMMAND", raising=False)
    monkeypatch.setattr(hermes.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="by2kb:install-by2kb"):
        hermes._run_by2kb(["version"])
