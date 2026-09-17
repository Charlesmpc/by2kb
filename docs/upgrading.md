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


## 0.5.1 title provenance

No automatic title migration runs. Existing archives stay untouched until explicit
re-enrichment. Raw-only jobs do not gain LLM requirements. See
[0.5.1 behavior and Agent protocol](releases/0.5.1.md).
