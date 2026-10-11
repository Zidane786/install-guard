# Changelog

## 1.0.0 — 2026-10-11

### Highlights

- install-guard stops coding agents from installing or downloading anything except from the
  places you allow, such as your internal Nexus registry. It works in Claude Code, GitHub Copilot
  CLI and Kiro, on Windows, macOS and Linux.
- Allowed routes run without a prompt. Anything else is refused with a plain message that names
  the route to use instead, or asks you first.
- Adding a route or removing a rule always needs your approval and a written reason, and every
  change is logged.

### Added

- Checks for package managers (npm, pip, uv, cargo, go and many more), containers, downloaders
  such as `curl`, `wget` and `Invoke-WebRequest`, `git clone`, toolchain and system package
  installers, web fetches and searches, and edits that add a dependency to a manifest.
- Works out where a command would really download from (flags, environment variables and config
  files such as `.npmrc` and `pip.conf`), so a flag or a config file cannot sneak past an allowed
  route.
- Routes can be a host, a URL prefix, a git organisation or a single repo. `localhost` is always
  allowed so testing your own app keeps working.
- Commands and skills: `setup`, `check`, `routes`, `allow`, `remove`, `block`, `improve` and `test`.
- A watcher that notices downloads the guard missed, tells you at once, and lets
  `/install-guard:improve` turn each one into a new rule.
- Detection rules are plain data files, so a new rule needs no code change.
