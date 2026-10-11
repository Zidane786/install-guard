# install-guard

**install-guard stops coding agents from installing or downloading anything except from the
places your organisation allows**, such as an internal registry. It works in Claude Code,
GitHub Copilot CLI and Kiro, on Windows, macOS and Linux.

## Install

Claude Code:

```
/plugin marketplace add Zidane786/install-guard
/plugin install install-guard@install-guard
```

GitHub Copilot CLI:

```
copilot plugin marketplace add Zidane786/install-guard
copilot plugin install install-guard@install-guard
```

Then run `/install-guard:setup` to pick your allowed routes.

## What it does

- Checks every install or download an agent tries against your allowed routes before it runs.
- Runs allowed routes; refuses (or asks about) everything else, naming the route to use.
- Needs your approval and a reason to add a route or remove a rule, and logs every change.
- Notices downloads it missed and can learn a rule for them.

See [CHANGELOG.md](CHANGELOG.md) for what each version changes.
