# AGENTS.md — working on the install-guard repo

This repo is **install-guard**, a Claude Code, GitHub Copilot CLI and Kiro plugin that stops
coding agents from installing or downloading anything outside the **allowed routes** (for example
an internal Nexus registry), on Windows, macOS and Linux. Every install or download is checked
before it runs; loosening the rules needs the user's approval and a written reason; a watcher
notices downloads the guard missed. Always with a human in the loop.

**This is the only instructions file.** `CLAUDE.md` just imports it (`@AGENTS.md`), so Claude Code,
Copilot CLI, Kiro and other agent tools all read the same text. Edit this file, never `CLAUDE.md`.

## 1. Read first

| Need | Read |
|---|---|
| What install-guard is, every decision | `docs/specs/2026-10-11-install-guard-design.md` |
| How a release was built (contracts) | `docs/plans/*-build-plan.md` (newest last) |
| User-facing overview | `README.md`, `plugins/install-guard/README.md` |
| Every command and flag, "use it when…" | `plugins/install-guard/reference/` |
| What changed per version | `plugins/install-guard/CHANGELOG.md` |
| Reference implementations (**read only, never write there**) | below |

The spec is the source of truth; update this file when it changes the layout or the rules.

**Reference implementations.** Read them, copy the approach, never write in their folders:

- **Plugin shape, CI, releases, hooks for both tools:** Slate,
  `~/Desktop/Projects/gdtc-ai-products/slate/` (`github.com/Zidane786/slate`).
- **Agent guide, hooks, Copilot CLI behaviour, launchers:** Plumb,
  `~/Desktop/Projects/gdtc-ai-products/plumb/` (`github.com/Zidane786/plumb`).

## 2. Layout

```
.claude-plugin/marketplace.json      marketplace "install-guard" → plugins/install-guard
.claude/settings.json                 repo settings for agents (no AI attribution)
plugins/install-guard/                the installed plugin (only this folder ships)
  .claude-plugin/plugin.json          name + VERSION (source of truth for the plugin version)
  engine/parse.py                     split/unwrap/tokenize shell commands (bash, PowerShell, cmd)
  engine/detect.py                    apply rules → list[Install]
  engine/sources.py                   effective destination per tool (flags, env, config files per OS)
  engine/policy.py                    load user/project policy, route matching, decide()
  engine/watch.py                     output signals for the watcher
  engine/adapters.py                  Claude Code / Copilot / Kiro input parsing and output formatting
  rules/*.json                        detection rules (data)
  guard.py                            entry point: hook modes (pre, post, session, stop) and CLI subcommands
  hooks/                              hooks.json, run.sh, run.cmd (python launchers)
  kiro/install-guard.json.tmpl        Kiro hook file template
  skills/<name>/SKILL.md              setup check routes allow remove block improve test
  reference/                          commands.md, rules.md, tools.md
  CHANGELOG.md  README.md
tests/                                pytest suite
.github/workflows/test.yml            CI: Windows/macOS/Linux × Python 3.9/3.13
.github/workflows/release.yml         publishes vX.Y.Z releases from the CHANGELOG on merge
.github/dependabot.yml                keeps the GitHub Actions up to date
scripts/release_notes.py              prints a version's release notes from the CHANGELOG
scripts/check_version_bump.py         CI guard: plugin changes carry a new version + CHANGELOG section
docs/                                 specs and build plans
```

## 3. Rules that must hold in every change

- **Standard library only**, Python **3.9+**, in everything that ships. No pip dependencies at
  runtime, no npm or npx for install-guard's own tooling.
- **Works on Windows, macOS, Linux, over SSH and in tmux.** Every Python entry point reconfigures
  stdout/stderr to UTF-8; every text read/write and subprocess call names `encoding="utf-8"`
  (`tests/test_portability.py` enforces both). UTF-8 + `\n` for every file write, pathlib, no
  `shell=True`, no POSIX-only calls without a Windows path. Every hook has a `command` (through
  `hooks/run.sh`) and a `powershell` command (through `hooks/run.cmd`).
- **Every error/refusal is actionable:** `error: …` then `fix: <exact command>` or
  `ask the user: …`. A test asserts this for every refusal path.
- **Rules are data.** Detection rules live in `rules/*.json` (schema `1`); a new rule needs no code
  change, only a test case.
- **Agents never loosen the policy alone.** `allow` and `remove` need the user's approval and a
  `--reason`; agents may not edit the policy, history or rules files directly, and the guard
  refuses those edits. Project `.install-guard.json` may only tighten.
- **Fail closed on installs, open on the rest.** If the guard crashes or no Python is found, a
  command that looks like an install or download is refused and everything else runs (with a
  logged `fix:` line).
- **MCP tools are out of scope**, as are sandboxing and VS Code Copilot Chat. Don't add
  half-support.
- **Design with the user before building.** New features and commands go through brainstorming:
  one question at a time (the question tool, options with a recommendation), the agreed design
  written to `docs/specs/`, a build plan in `docs/plans/`, and the user's approval of both before
  any code.
- **Human in the loop, always.** Skills ask one question at a time with options and a
  recommendation, never write into a project without saying where, never merge, never
  force-push. In this repo: never push to `main` (the ruleset blocks it); every change goes
  through a branch and a PR that the user merges.
- **Lean skills.** `SKILL.md` files stay short; details go in `reference/` and are loaded only
  when needed.
- **Product text never mentions internal projects.** install-guard is generic.
- **Three tools, one plugin.** Claude Code and Copilot CLI load the same `.claude-plugin/`
  manifest, skills and Claude-format `hooks.json`; Kiro gets a hook file from `guard.py setup
  --kiro`. One engine with an adapter per tool for input and output formats. Copilot hides exit-2
  stderr (refuse with JSON), fails open on timeouts and denies on crashes; keep that pinned in
  tests.

## 4. Develop and test

```
uv run --with pytest pytest -q                  # full suite
uv run --python 3.9 --with pytest pytest -q     # oldest supported Python
claude plugin validate --strict .
claude plugin validate --strict plugins/install-guard
```

- Write tests first for every change: the happy path, every refusal, and for every rule a refused
  case plus a control that must pass (`echo npm install`, `git status`, `npm run build`).
- Test quoting and escaping (`npm install "left pad"`, PowerShell backticks, `cmd ^`), config
  precedence (flag > env > project > user > global), look-alike hosts (`nexus.corp.evil.com`,
  `https://nexus.corp@evil.com/`, uppercase, punycode) and odd hook input (empty stdin, non-JSON,
  `toolArgs` as string or object, Windows paths, a 1 MB command).
- Live-test in a throwaway repo: `claude --plugin-dir plugins/install-guard`, in Copilot CLI
  (`copilot plugin marketplace add Zidane786/install-guard`, `copilot plugin install
  install-guard@install-guard`, restart) and in Kiro. Ask the agent to install and download
  things and confirm it is stopped.
- **CI (`.github/workflows/test.yml`)** runs on every PR and push to `main`, but the full suite
  runs only when one of these changed: `plugins/**`, `tests/**`, `scripts/**`,
  `.github/workflows/**`, `.claude-plugin/**`, `.gitattributes`. Docs-only changes pass in
  seconds with "no code changed". Keep this list and the workflow's list in step.

## 5. Keeping docs in step (do this in the SAME change as the feature)

Every feature, fix or behaviour change updates **all** that apply, before it's considered done:

- [ ] **`plugins/install-guard/reference/`** — new or changed commands, rules, tools.
- [ ] **Skills** (`plugins/install-guard/skills/*/SKILL.md`) that should use or mention it.
- [ ] **`README.md`** and **`plugins/install-guard/README.md`** for anything user-visible.
- [ ] **`plugins/install-guard/CHANGELOG.md`** — plain-words entry under the next version.
- [ ] **Spec** (`docs/specs/…`) for new decisions or commands.
- [ ] **Tests** — unit tests, portability and per-tool format checks as relevant.
- [ ] **This file (`AGENTS.md`)** if the rules, layout or workflow changed.

## 6. Releasing a version

**Every PR that changes `plugins/**` is a release.** CI (`scripts/check_version_bump.py`) fails
unless it bumps the version above `main`'s and adds that version's CHANGELOG section; there is no
"Unreleased" section. Claude Code only updates installed plugins when the version changes, so an
unversioned change would never reach users. Docs-only PRs outside `plugins/` need no bump.
Each CHANGELOG section lists **only** what changed since the previous version; a released
version's section is frozen. New changes go under the new version.

**Versioning (semver).** The first release is **1.0.0**. After that: **patch** (1.0.x) for fixes
and wording, **minor** (1.x.0) for new rules, tools, commands or options that existing setups keep
working with, **major** (x.0.0) only for a change that breaks existing policy files or drops
something.

1. Bump the version in **all** of: `plugins/install-guard/.claude-plugin/plugin.json` and
   `guard.py` (`VERSION`), and the rules schema version if the rules format changed.
2. Finish `CHANGELOG.md`: `## X.Y.Z — YYYY-MM-DD` with the headings **Highlights**, **Added**,
   **Changed**, **Fixed**, **Security**, **Removed**, **Upgrade notes** (leave out empty ones).
   `tests/test_release_notes.py` enforces this; preview with `python3 scripts/release_notes.py`.
3. Full suite on 3.13 and 3.9, `claude plugin validate --strict`, a live check in Claude Code,
   Copilot CLI and Kiro, CI green on all three OSes.
4. Branch `feat/<version>`, draft PR with summary, verification and follow-ups; merge only when
   the user says so. Default branch is `main`, protected by a ruleset: no direct pushes (PR
   required, squash only, review threads resolved), all 6 CI checks must pass, no force-push or
   deletion, no bypass.
5. **Releases are automatic:** when a merge to `main` carries a new version in `plugin.json`,
   `.github/workflows/release.yml` tags `vX.Y.Z` and publishes a GitHub Release with that
   CHANGELOG section. Don't create release tags by hand (tags `v*` are protected).

## 7. Commits

Small, focused commits: `<area>: <what changed>` with a short body listing verification. Never
commit generated files (`__pycache__`, `.pytest_cache`, `.DS_Store` — see `.gitignore`).
**No AI attribution** anywhere: no `Co-Authored-By` trailer, no "Generated with" line in commits
or PR descriptions (`.claude/settings.json` turns Claude Code's off). Tell subagents the same.
