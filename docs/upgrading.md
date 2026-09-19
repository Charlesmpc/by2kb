# Upgrading

An ordinary by2kb upgrade replaces executable code, not personal state.

## User-owned state

The following paths must survive every package and Agent-adapter upgrade:

- `$BY2KB_HOME/config.toml` and `$BY2KB_HOME/.env`;
- `$BY2KB_HOME/by2kb.db` and `$BY2KB_HOME/models/`;
- `$BY2KB_HOME/skills/`, including a personalized
  `video-to-knowledge/SKILL.md`;
- the configured knowledge-base folder and all generated artifacts;
- any custom paths referenced from configuration or environment variables.

`pipx upgrade` operates on its isolated application environment and does not own these
paths. `by2kb init` refuses to replace existing configuration unless the user explicitly
passes `--force`; installers and Agents must never add that flag during an upgrade.

## Managed code

The CLI environment and plugin code have separate owners. Upgrade the CLI using
the package manager that installed it (`pipx upgrade by2kb` or `uv tool upgrade by2kb`).
Inspect `hermes plugins list` before updating the adapter:

- **Catalog install:** use `hermes plugins update by2kb`, which follows the reviewed
  catalog pin. Do not overwrite it with `by2kb agent install`.
- **Git-pinned install:** choose a new full SHA explicitly using Hermes install.
  An ordinary update must not move that pin.
- **Legacy copy install:** refresh the adapter from the CLI package as below.

For a legacy pipx + copy installation only:

```bash
pipx upgrade by2kb
by2kb agent install hermes --force
by2kb doctor
```

The copy installer refuses to overwrite an adapter recorded in Hermes'
`plugins/.install-metadata.json`, carrying `.hermes-catalog.json`, or containing a
Git checkout, even with `--force`. Invalid ownership metadata requires inspection,
not deletion. PyPI upgrades do not automatically update a directory-managed plugin.
Use the intended Hermes profile for all commands; `--hermes-home` also applies to
the copy installer's enable step. Ask before restarting a running gateway.

The plugin's staged request-file workflow requires by2kb CLI 0.5.3+. Check release
notes before adopting a future incompatible CLI or plugin version. Catalog
submission is pending; the bare `hermes plugins install by2kb` command is not yet
advertised as available.

Do not personalize files inside the managed plugin directory. Put a Hermes runtime
Skill at `$BY2KB_HOME/skills/video-to-knowledge/SKILL.md`, or set
`BY2KB_HERMES_SKILL` to another file. The plugin loads that file ahead of its packaged
default, so adapter replacement does not overwrite the user's workflow.

If a pinned direct-URL pipx installation requires uninstalling and reinstalling the
application, confirm the action first. Removing the pipx environment still must not
remove `$BY2KB_HOME` or the knowledge-base folder.

## Bilibili and YouTube defaults

The source-default fix following 0.6.0 adds yt-dlp to the base package and makes
`["bilibili_native", "yt_dlp"]` the default in the loader, interactive setup, and
agent-local setup. A normal upgrade with the existing package manager installs the
dependency; no browser, ASR model, or cloud credentials are installed automatically.

Configuration is **not rewritten or reset** during an upgrade:

- No `sources.providers` key: automatically inherit both routes. This includes an
  existing file containing only browser fallback and/or yt-dlp options. No edit or
  reinitialization is needed. Existing ASR, knowledge-base and authentication settings
  stay unchanged.
- Explicit `sources.providers`: preserve the exact list and its priority. An older
  initializer may have written `["bilibili_native"]`; this cannot be distinguished
  from an intentional restriction, so the upgrade does not silently broaden it.
  If YouTube is wanted, edit that one line to
  `providers = ["bilibili_native", "yt_dlp"]`, or remove just the `providers` key
  to inherit defaults. Do not replace the whole configuration or duplicate an existing
  TOML section. Keep any custom provider order if it is intentional.
- `BY2KB_SOURCE_PROVIDERS` in the service environment or `.env` overrides the file.
  Update or remove that override explicitly if it still restricts the sources.
- An explicit `[sources.yt_dlp] enabled = false` stays disabled. An explicit empty
  provider list remains a configuration error; it is never silently reset.

Run `by2kb doctor --json` to see the effective `source_providers` and any disabled
yt-dlp or missing-dependency diagnostics. Passing checks are not proof that a
particular video is accessible: authentication, network restrictions, and unavailable
media can still fail later. Do not run `by2kb init --force` to adopt these defaults.


## 0.5.1 title provenance

No automatic title migration runs. Existing archives stay untouched until explicit
re-enrichment. Raw-only jobs do not gain LLM requirements. See
[0.5.1 behavior and Agent protocol](releases/0.5.1.md).
