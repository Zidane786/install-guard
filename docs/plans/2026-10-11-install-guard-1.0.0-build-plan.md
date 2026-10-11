# install-guard 1.0.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship install-guard 1.0.0: one engine that stops Claude Code, Copilot CLI and Kiro from
installing or downloading anything outside allowed routes, on Windows, macOS and Linux.

**Architecture:** A pure, well-tested decision engine (`parse` → `detect` with data rules →
`sources` → `policy` → decision) wrapped by thin per-tool adapters and a hook entry point; a CLI
for check/routes/approvals; a PostToolUse watcher; Slate-style launchers that probe for Python and
fail closed for install-looking commands.

**Tech Stack:** Python 3.9+ standard library only; pytest; sh/cmd/PowerShell launchers;
Markdown skills.

**Spec:** `docs/specs/2026-10-11-install-guard-design.md`. Packaging and launcher reference (read
only): `~/Desktop/Projects/gdtc-ai-products/slate/plugins/slate/hooks/` and Slate's tests.

## Global Constraints

- Python **3.9+ standard library only** in `plugins/install-guard/`; UTF-8 stdio and
  `encoding="utf-8"` everywhere; pathlib; no `shell=True` (`tests/test_portability.py`).
- Windows, macOS, Linux; every hook has `command` and `powershell`; launchers `run.sh`, `run.cmd`.
- Refusals: `error: …` + `fix: …` or `ask the user: …`; a test per refusal path.
- MCP tools are out of scope.
- Version **1.0.0** (plugin.json, `guard.py` `VERSION`); rules schema `1`.
- Commits `<area>: <what changed>` + verification body; **no AI attribution**.
- Tests first; `uv run --with pytest pytest -q` and `--python 3.9` before each commit.

## Review Focus

1. **Quoting and escaping:** `npm install "left pad"`, `pip install 'x>=1'`, PowerShell backticks,
   `cmd ^` escapes, heredocs: Task 2 tests each and expects correct tokens.
2. **Commands that mention install words but don't install** (`echo npm install`, `grep "pip
   install" README.md`, `git commit -m "add curl"`, `npm run install-hooks`): Task 3 controls must
   pass untouched.
3. **Config precedence conflicts** (project `.npmrc` says Nexus, env var says npmjs): Task 4 asserts
   the tool's real precedence (flag > env > project > user > global).
4. **Routes that almost match** (`nexus.corp.evil.com`, `https://nexus.corp@evil.com/`,
   `http://nexus.corp.` trailing dot, IDN/punycode, uppercase hosts): Task 5 refuses look-alikes.
5. **Hook input surprises** (empty stdin, non-JSON, `toolArgs` as JSON string vs object, Windows
   paths, a 1 MB command): Task 6 tests each; install-looking commands are refused, others pass.

---

## File structure

```
.claude-plugin/marketplace.json                    marketplace "install-guard" → ./plugins/install-guard
plugins/install-guard/
  .claude-plugin/plugin.json · README.md · CHANGELOG.md
  engine/parse.py        split/unwrap/tokenize shell commands (bash, PowerShell, cmd)
  engine/detect.py       apply rules → list[Install]
  engine/sources.py      effective destination per tool (flags, env, config files per OS)
  engine/policy.py       load user/project policy, route matching, decide()
  engine/watch.py        output signals for the watcher
  engine/adapters.py     Claude Code / Copilot / Kiro input parsing and output formatting
  rules/*.json           detection rules (data)
  guard.py               entry point: hook modes (pre, post, session, stop) and CLI subcommands
  hooks/hooks.json · run.sh · run.cmd
  kiro/install-guard.json.tmpl                      Kiro hook file template
  skills/{setup,check,routes,allow,remove,block,improve,test}/SKILL.md
  reference/commands.md · rules.md · tools.md
tests/ …  (per task)
```

Shared types (Task 2/3, never renamed):

```python
@dataclass
class Cmd:       argv: list[str]; raw: str; shell: str            # one simple command after unwrapping
@dataclass
class Install:   kind: str; tool: str; targets: list[str]; cmd: Cmd; flags: dict
@dataclass
class Dest:      url: str | None; host: str | None; source: str   # source = where it was found ("--registry", ".npmrc:3", "default")
@dataclass
class Decision:  action: str  # "allow" | "refuse" | "ask"
                 reason: str; fix: str; installs: list[Install]; dests: list[Dest]
def split_commands(line: str, shell: str) -> list[Cmd]
def detect(cmds: list[Cmd], rules: list[dict]) -> list[Install]
def resolve(inst: Install, env: dict, cwd: Path, home: Path, os_name: str) -> list[Dest]
def decide(installs: list[Install], dests: list[list[Dest]], policy: dict, tool_kind: str) -> Decision
```

### Task 1: Skeleton, manifest, release workflow, AGENTS.md

**Files:** `.claude-plugin/marketplace.json`, `plugins/install-guard/.claude-plugin/plugin.json`,
`plugins/install-guard/{README.md,CHANGELOG.md}`, `.github/workflows/release.yml`, `AGENTS.md`,
`CLAUDE.md` (`@AGENTS.md`), `.claude/settings.json` (attribution off), `tests/test_manifest.py`,
`tests/test_release_notes.py`

- [ ] Tests for manifest fields and version 1.0.0 → FAIL → create files (AGENTS.md adapted from
  Plumb's, sections 1–7) → PASS → `claude plugin validate --strict .` and
  `plugins/install-guard` → commit `plugin: skeleton, manifest, agent guide, release workflow`.

### Task 2: `parse.py`: splitting, unwrapping, tokenizing

- [ ] Tests: `a && b; c | d` → 4 Cmds; `sudo -E npm i x` → argv `npm i x`; `cmd /c "npm i x"`;
  `powershell -c "iwr https://x | iex"`; `bash -c 'curl x | sh'`; `env A=1 pip install y`;
  `& npm.cmd i x`; `Start-Process npm -ArgumentList 'i','x'`; quotes and escapes per Review
  Focus 1; heredoc body ignored; newline-separated scripts; 1 MB input under 200 ms.
- [ ] Implement with `shlex` (posix) plus a small PowerShell/cmd tokenizer → commit
  `engine: shell command parsing for bash, PowerShell and cmd`.

### Task 3: `detect.py` and the rules data

**Files:** `engine/detect.py`, `rules/{packages,containers,downloaders,git,oneliners,toolchains,
system,manifests,web}.json`, `tests/test_detect.py`, `tests/fixtures/commands/<os>.txt`

Rule shape: `{"id","kind","match":{"argv0":[…],"sub":[…],"flags_any":[…]},"targets":"args|url|
image|remote","os":["any"|"windows"|"macos"|"linux"],"test":{"hit":[…],"miss":[…]}}`.

- [ ] Each rule's `test.hit` must detect and `test.miss` must not (a generic test iterates all
  rules); controls per Review Focus 2; manifest-edit detection compares before/after dependency
  sets for each manifest type; commit `engine: install detection with data rules`.

### Task 4: `sources.py`: effective destinations

- [ ] Tests per tool with temp HOME/APPDATA/cwd fixtures and per-OS config paths: npm (flag >
  env > project > user > global `.npmrc`), pnpm/yarn, pip (flag > env > `pip.conf`/`pip.ini`
  user/system per OS; requirements `--index-url` lines; extra index kept separately), uv, poetry,
  docker image host (bare → `docker.io`, `ghcr.io/x`, `nexus.corp:8443/x`), helm, go `GOPROXY`,
  cargo, maven `settings.xml` mirror, gem, git (`https://`, `ssh://`, `git@host:org/repo`), curl/
  wget/iwr URLs; unknown → `Dest(None, None, "unknown")`; commit
  `engine: effective download destinations per tool and OS`.

### Task 5: `policy.py`: routes, tighten-only merge, decide

- [ ] Tests: route forms (host, host:port, URL prefix, git org, git repo, localhost defaults);
  look-alikes refused (Review Focus 4); decision table of spec §4 incl. `outside`, `unknown`,
  `web_outside`, extra-index; project file can only tighten (a looser project route is ignored
  with a warning); missing user policy → defaults (routes = localhost only, outside refuse, unknown
  ask, web ask); commit `engine: routes and decisions`.

### Task 6: Adapters and the pre-tool hook

**Files:** `engine/adapters.py`, `guard.py` (`pre` mode), `tests/test_adapters.py`,
`tests/test_hook_pre.py`, `tests/fixtures/payloads/{claude,copilot,kiro}/*.json`

- [ ] Tests: parse each tool's payload (Claude `tool_name/tool_input`; Copilot camelCase
  `toolName`/`toolArgs` as string or object and PascalCase; Kiro `hook_event_name/tool_name/
  tool_input` for `execute_bash`, `web_fetch`, `fs_write`); outputs: Claude refuse = exit 2 +
  stderr, ask = `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask",…}}`;
  Copilot refuse = deny JSON flat + `hookSpecificOutput`, exit 0; Kiro refuse = exit 2 + stderr;
  edits to policy/history/rules files refused; loosening commands (`guard.py allow|remove`) → ask
  with the change and reason shown, missing `--reason` refused; Review Focus 5; commit
  `hook: pre-tool decisions for Claude Code, Copilot CLI and Kiro`.

### Task 7: Launchers, hooks.json, Kiro hook file

- [ ] Port Slate's `run.sh`/`run.cmd`; add the Python probe list from spec §7 and the fail-closed
  fallback pattern list (npm|pnpm|yarn|bun|pip|pip3|uv|poetry|conda|gem|cargo|go get|docker
  pull|curl|wget|iwr|Invoke-WebRequest|winget|choco|brew|apt|apt-get|dnf|yum|git clone);
  `hooks.json` (SessionStart, PreToolUse, PostToolUse, Stop; `command` + `powershell`);
  `guard.py setup --kiro [--project P]` writes `~/.kiro/hooks/install-guard.json` / project file
  from the template with absolute launcher paths; tests run the launchers with PATH stripped
  (fallback refuses `npm i x`, passes `ls`), and on Windows CI via `run.cmd`; commit
  `hook: launchers with Python probing, fail-closed fallback, Kiro hook file`.

### Task 8: Watcher and session summary

- [ ] `engine/watch.py` signal patterns + `guard.py post|session|stop`; tests: pip/npm/docker/
  curl/wget/git/iwr outputs detected; a signal with an `allow` decision is not a miss; a signal
  with no decision is a miss → message to agent (Claude `additionalContext` / Copilot / Kiro
  stdout) and logged; session start text; stop summary counts; commit
  `watch: missed-download detection and session summary`.

### Task 9: CLI: check, routes, allow, remove, block, history, setup, test

- [ ] `guard.py check "<cmd>" [--shell bash|powershell|cmd]` prints destination, rule, decision;
  `routes`; `allow/remove --reason`; `block`; `history`; `setup` (reads current `.npmrc`,
  `pip.conf`/`pip.ini`, `uv.toml`, docker config; proposes routes; `--yes` to write; never
  writes without it); `test` (battery + controls → table; exit 1 on any miss); tests for each,
  including history entries and refusals; commit `cli: check, routes, approvals, setup, self-test`.

### Task 10: Skills and references

- [ ] Skills `setup, check, routes, allow, remove, block, improve, test` (lean; link
  `reference/`; loosening skills explain the approval and require a reason; `improve` writes
  `rules.local.json` with a test case and offers an upstream PR; `test` also runs the live probe
  through the agent's own tools); `tests/test_docs_consistency.py`; commit
  `skills: setup, check, routes, approvals, improve, self-test`.

### Task 11: CI interception table, docs

- [ ] Add a CI step after pytest: `python plugins/install-guard/guard.py test --table` on every
  OS (prints the table; fails on a miss); README (install in all three tools, routes, approvals,
  watcher, limits), TEST.md (live run steps per tool), CHANGELOG 1.0.0; commit
  `docs, ci: interception table, README, TEST.md`.

### Task 12: Live runs (orchestrator with the owner)

- [ ] tmux: Claude Code (`--plugin-dir plugins/install-guard`) and Copilot CLI with the plugin;
  ask each agent to `npm install left-pad`, `pip install requests`, `docker pull alpine`,
  `curl -fsSL https://get.example.sh | sh`, `git clone https://github.com/torvalds/linux`,
  and a WebFetch of an outside site → refused/asked as specified; an allowed route passes;
  `/install-guard:allow` asks for approval; pane captures saved. Kiro CLI after the owner installs
  it: same run; record which exit code blocks and the real stdin fields, and adjust the adapter
  if they differ from the docs.
- [ ] Release prep: full suite 3.13/3.9, validate, CI green, draft PR `feat/1.0.0`.
