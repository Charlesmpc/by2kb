"""Opt-in offline smoke test against a real Hermes source checkout.

Uses a disposable local Git source and HERMES_HOME, not the user's live profile.
Requires the dependencies of the selected Hermes plugin installer (e.g. PyYAML).
No gateway, model, cloud ASR, or external Git repository is contacted.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-repo", required=True, type=Path)
    parser.add_argument("--legacy", action="store_true", help="Use runtime doctor for older Hermes without the catalog validator")
    parser.add_argument("--accept-reviewed-caution", action="store_true",
                        help="Accept caution findings only after reviewing their report; dangerous remains blocked")
    args = parser.parse_args()
    hermes_repo = args.hermes_repo.resolve()
    repo = Path(__file__).resolve().parents[1]
    upstream_sha = subprocess.check_output(
        ["git", "-C", str(hermes_repo), "rev-parse", "HEAD"], text=True
    ).strip()
    with tempfile.TemporaryDirectory(prefix="by2kb-hermes-smoke-") as temporary:
        root = Path(temporary).resolve()
        profile = root / "profile"
        os.environ["HERMES_HOME"] = str(profile)
        os.environ["BY2KB_HOME"] = str(root / "by2kb-state")
        os.environ.pop("BY2KB_HERMES_SKILL", None)
        os.environ["HERMES_ENABLE_PROJECT_PLUGINS"] = "0"
        bundled = root / "empty-bundled"
        bundled.mkdir()
        os.environ["HERMES_BUNDLED_PLUGINS"] = str(bundled)
        # Prevent discovery of plugins/config under the caller's current directory.
        os.chdir(hermes_repo)
        sys.path[:0] = [str(hermes_repo), str(repo)]
        from hermes_cli.plugins_cmd import _install_plugin_core, cmd_enable, _read_install_metadata
        from hermes_cli.plugin_dev import _doctor_runtime
        from by2kb.agent_install import install_hermes_plugin
        from by2kb.errors import ConfigError

        source = root / "source"
        plugin_source = source / "by2kb/integrations/hermes"
        shutil.copytree(repo / "by2kb/integrations/hermes", plugin_source,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for command in (["init"], ["add", "."],
                        ["-c", "user.name=Install Test", "-c", "user.email=install-test@example.invalid",
                         "commit", "-m", "Isolated plugin test snapshot"]):
            subprocess.run(["git", "-C", str(source), *command], check=True, capture_output=True)
        sha = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
        def reviewed_scan(result):
            from tools.plugin_guard import format_scan_report
            print(format_scan_report(result))
            return args.accept_reviewed_caution

        target, manifest, name = _install_plugin_core(
            source.as_uri() + "#by2kb/integrations/hermes", force=False, ref=sha,
            scan_decision_cb=reviewed_scan)
        assert name == "by2kb" and manifest["name"] == name
        metadata = _read_install_metadata()[name]
        assert metadata["pinned"] and metadata["revision"] == sha
        assert target == profile / "plugins/by2kb"
        if args.legacy:
            from hermes_cli.plugin_dev import doctor_plugin
            report = doctor_plugin(target)
            print(report.format_text())
            assert report.ok, "Legacy runtime doctor failed"
        else:
            from hermes_cli.plugin_validate import validate_plugin_dir
            report = validate_plugin_dir(target)
            print(json.dumps(report.to_dict(), indent=2))
            assert report.ok, "Official catalog validator failed"
        cmd_enable(name, allow_tool_override=False)
        import yaml
        config = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
        assert name in config["plugins"]["enabled"]
        with _doctor_runtime(target) as runtime:
            skills = runtime.manager._plugin_skills
            assert {"by2kb:install-by2kb", "by2kb:video-to-knowledge"} <= set(skills)
            assert runtime.registered_hooks == ("pre_gateway_dispatch",)
            for key in ("by2kb:install-by2kb", "by2kb:video-to-knowledge"):
                assert skills[key]["path"].is_file()
        before = (target / "__init__.py").read_bytes()
        try:
            install_hermes_plugin(hermes_home=profile, force=True, enable=False)
        except ConfigError as exc:
            assert "managed by Hermes" in str(exc)
        else:
            raise AssertionError("CLI overwrote a Hermes-managed pin")
        assert (target / "__init__.py").read_bytes() == before
        assert _read_install_metadata()[name] == metadata
        print(f"PASS: pinned subdir install, enable, skill/hook registration, ownership protection; Hermes {upstream_sha}")


if __name__ == "__main__":
    main()
