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

The pipx virtual environment and `~/.hermes/plugins/by2kb` are program-owned. Refresh a
Hermes installation after upgrading the package:

```bash
pipx upgrade by2kb
by2kb agent install hermes --force
by2kb doctor
```

Do not personalize files inside the managed plugin directory. Put a Hermes runtime
Skill at `$BY2KB_HOME/skills/video-to-knowledge/SKILL.md`, or set
`BY2KB_HERMES_SKILL` to another file. The plugin loads that file ahead of its packaged
default, so adapter replacement does not overwrite the user's workflow.

If a pinned direct-URL pipx installation requires uninstalling and reinstalling the
application, confirm the action first. Removing the pipx environment still must not
remove `$BY2KB_HOME` or the knowledge-base folder.
