# install-guard

**install-guard stops coding agents from installing or downloading anything except from the
places your organisation allows**, such as an internal registry. It works in Claude Code,
GitHub Copilot CLI and Kiro, on Windows, macOS and Linux.

- Every install or download an agent tries (package managers, containers, `curl`/`wget`/
  `Invoke-WebRequest`, toolchains, system packages, `git clone`, web fetches, manifest edits)
  is checked against your allowed routes before it runs.
- Allowed routes run; anything else is refused or needs your approval.
- Adding a route or removing a rule always needs your approval and a reason, and is logged.
- A watcher notices downloads the guard missed, and the guard can learn a new rule for them.

install-guard is in design. The first release (1.0.0) will add install steps and the commands.
See [Releases](https://github.com/Zidane786/install-guard/releases).

## License

[MIT](LICENSE)
