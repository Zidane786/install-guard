# install-guard design spec

- **Status:** approved in chat · 2026-10-11
- **Release:** everything here ships in **1.0.0**
- **Repo:** `github.com/Zidane786/install-guard` (default branch `main`)

## 1. Purpose

On enterprise Windows and Mac machines (and Linux), stop coding agents from installing or
downloading anything except from **allowed routes** (for example an internal Nexus registry), in
every project, in **Claude Code, GitHub Copilot CLI and Kiro**. Loosening the rules always needs
the user's approval and a written reason. The guard notices downloads it missed and can learn
new rules for them.

Out of scope: MCP tools (the organisation approves those explicitly); sandboxing (this is a
guardrail; registry controls remain the real enforcement); VS Code Copilot Chat.

## 2. Tools and how the guard plugs in

| Tool | Install | Hook | Block | Notes |
|---|---|---|---|---|
| Claude Code | plugin at user scope (`/plugin marketplace add Zidane786/install-guard`, `/plugin install install-guard@install-guard`) | PreToolUse `Bash\|PowerShell\|Monitor\|WebFetch\|WebSearch\|Write\|Edit\|MultiEdit`; PostToolUse (watcher); SessionStart; Stop | exit 2 + stderr, or `permissionDecision: "ask"` | |
| Copilot CLI | same plugin (`copilot plugin marketplace add …`, `copilot plugin install install-guard@install-guard`) | preToolUse on `bash\|powershell\|web_fetch\|web_search\|create\|edit\|task`; postToolUse; sessionStart | `{"permissionDecision":"deny"}` JSON (exit-2 stderr is hidden); `ask` in interactive sessions | Copilot denies on hook crashes and non-zero exits; timeouts fail open |
| Kiro (CLI v3+ and IDE) | `guard.py setup --kiro` writes `~/.kiro/hooks/install-guard.json` (global, CLI) and, per project on request, `.kiro/hooks/install-guard.json` (the IDE reads workspace hooks only) | `PreToolUse` matchers `shell`, `web`, `write`; `PostToolUse`; `SessionStart`; `Stop` | exit 2 + stderr (verify live; docs also say any non-zero blocks) | stdin fields `hook_event_name`, `cwd`, `session_id`, `tool_name`, `tool_input` (verify live) |

One engine (`guard.py`) with an adapter per tool for input and output formats.

## 3. What is checked

Shell commands in bash, zsh, sh, PowerShell, cmd and Git Bash on all three OSes. Commands are
split on `&&`, `||`, `;`, `|`, newlines and subshells, and unwrapped from `sudo`, `doas`,
`env X=Y`, `cmd /c`, `powershell -c`, `pwsh -c`, `bash -c`, `sh -c`, `Start-Process`, `&`, `time`,
`nohup`, `xargs`.

- **Package managers:** npm, npx, pnpm (add, install, dlx), yarn (add, dlx), bun (add, x), pip,
  pip3, `python -m pip`, `py -m pip`, uv (add, pip install, tool install, `uvx`), poetry add,
  pipx, conda/mamba, gem, `bundle install`, cargo (add, install), go (get, install), composer
  require, `dotnet add package`, `dotnet tool install`, pod install, `deno install`, mvn
  (install, `dependency:get`), gradle with new repositories; Windows `.cmd`/`.exe` forms and full
  interpreter paths.
- **Containers and infra:** `docker pull`, `docker run/create/build` (pulled images and base
  images), `docker compose pull/up`, podman, nerdctl, buildah, `helm install`/`repo add`,
  `kubectl apply -f <url>`, `terraform init`.
- **Generic downloaders:** curl, wget, `Invoke-WebRequest`/`iwr`, `Invoke-RestMethod`/`irm`,
  `Start-BitsTransfer`, `bitsadmin`, `certutil -urlcache`, aria2c, `scp`/`rsync`/`sftp`/`ftp` from
  remote hosts; script installers (`curl … | sh`, `iwr … | iex`).
- **git:** clone, fetch, pull, submodule update, `ls-remote` against a remote URL.
- **One-liners:** `python -c` / `node -e` / `ruby -e` / `perl -e` containing a URL or a network
  call (`urllib`, `requests`, `fetch(`, `http.get`, `Net::HTTP`, `LWP`).
- **Toolchains and add-ons:** nvm, pyenv, rustup, sdk, asdf, volta install; `code
  --install-extension`; `gh extension install`.
- **System packages:** macOS brew, port, `mas install`, `installer -pkg`, `softwareupdate -i`;
  Linux apt, apt-get, `dpkg -i`, yum, dnf, zypper, `pacman -S`, `apk add`, snap, flatpak; Windows
  winget, choco, scoop, msiexec, `Install-Module`, `Install-Package`, `Install-Script`,
  `Add-AppxPackage`.
- **Web tools:** WebFetch/WebSearch (Claude Code), web_fetch/web_search (Copilot), `web` group
  (Kiro).
- **Manifest edits** that add a dependency: `package.json`, `requirements*.txt`,
  `pyproject.toml`, `Pipfile`, `Gemfile`, `Cargo.toml`, `go.mod`, `build.gradle(.kts)`,
  `Package.swift`, `Podfile`.
- Detection rules are **data** (`rules/*.json`), so a new rule needs no code change.

## 4. Routes and decisions

A **route** is a host (`nexus.corp`, `nexus.corp:8443`), a URL prefix
(`https://nexus.corp/repository/npm-group/`), or for git a host, an org
(`github.com/Zidane786/`) or a single repo (`github.com/Zidane786/plumb`). `localhost`,
`127.0.0.1` and `::1` are routes by default, so testing your own app with curl always works.

The guard works out each command's **effective destination** the way the tool would:
npm/pnpm/yarn `--registry`, `.npmrc` (project, user, global), `npm_config_registry`,
`.yarnrc.yml`; pip `-i`/`--index-url`/`--extra-index-url`, `PIP_INDEX_URL`,
`PIP_EXTRA_INDEX_URL`, `pip.conf`/`pip.ini` (project, user, system, per OS), index lines in
requirements files; uv flags, `UV_INDEX_URL`/`UV_DEFAULT_INDEX`/`UV_INDEX`, `uv.toml`,
`[tool.uv]`; poetry sources; image registry host (a bare `python:3.12` is `docker.io`); helm repo
URL; `GOPROXY`; cargo source replacement; Maven `settings.xml` mirrors; gem `--source`/`.gemrc`;
git remote URL (including `git@host:org/repo`); any URL's host.

| Situation | Decision |
|---|---|
| every destination inside a route | runs, no prompt (one optional `install-guard: allowed …` line) |
| a package-manager, container, script, git or system destination outside every route | **refused** (`error:` + `fix:` naming the allowed route to use) |
| a secondary index (`--extra-index-url`, extra npm registry) outside every route | refused, even when the main index is allowed |
| a web tool (WebFetch, web_fetch, web_search) outside every route | approval prompt |
| destination cannot be worked out (`make setup`, `./bootstrap.sh`, an unknown tool) | approval prompt naming the command |

Policy keys: `routes`, `outside` (`refuse` default | `ask`), `unknown` (`ask` default | `refuse`),
`web_outside` (`ask` default | `refuse` | `allow`), `show_allowed` (`false` default).

## 5. Policy files and approvals

- User policy: `$INSTALL_GUARD_POLICY`, else `~/.config/install-guard/policy.json`
  (`%APPDATA%\install-guard\policy.json` on Windows). History: `history.jsonl` beside it. Local
  extra rules: `rules.local.json` beside it.
- Optional project file `.install-guard.json` may only **tighten** (fewer routes, stricter
  decisions); anything looser in it is ignored with a warning.
- Commands (CLI `python3 guard.py …` and skills `/install-guard:…`): `check "<cmd>"` (dry run:
  destination, rule, decision; runs nothing), `routes` (list with who/when/why), `allow <route>
  --reason "…"`, `remove <route|rule> --reason "…"`, `block <route|pattern>`, `history`,
  `setup` (reads the registries the machine already uses and proposes them as routes; asked),
  `test`.
- **Loosening** (`allow`, `remove`) needs the user's approval, like Slate's unlock: when an agent
  runs it, the guard answers with the tool's approval card showing the change and the reason;
  one approval = one change; a missing `--reason` is refused. Tightening (`block`, improve rules)
  is shown and logged, no approval. Every change goes to `history.jsonl` (who, when, what, why).
- Agents may not edit the policy, history or rules files directly; the guard refuses those
  edits.

## 6. The watcher

A PostToolUse hook records every command each tool ran (in the OS temp folder, per session) and
reads its output for download signs the blocker may have missed (pip "Collecting"/"Downloading",
npm "added N packages", docker "Pulling from", curl's progress meter, wget "Saving to", "Cloning
into", "Fetching", `Invoke-WebRequest` progress). A sign with no matching guard decision is a
**possible miss**: the agent and the user are told at once ("possible download not caught:
`<cmd>`; run /install-guard:improve") and it is logged. Session start tells the agent to run
`/install-guard:check` before any command it is unsure about. At Stop, a one-line summary:
commands run, downloads allowed (where from), blocked, possible misses.

`/install-guard:improve` turns a miss (or a command the user gives) into a new rule in
`rules.local.json` with a test case, shows `check` blocking it, and offers to draft a PR to the
repo so every machine gets the rule.

`/install-guard:test`: (a) runs a battery of known download commands for this OS through the
engine and shows a blocked/allowed/missed table, plus controls that must pass (`git status`,
`npm run build`, `pip list`); (b) has the agent attempt a few harmless real downloads through its
own shell and web tools (e.g. `curl -sI https://example.com`) to prove the hook intercepts in
this tool.

## 7. Launchers and failure

`run.sh` / `run.cmd` / the PowerShell command try `python3`, `python`, `py -3`, then known
install paths (macOS: `/opt/homebrew/bin`, `/usr/local/bin`, `/usr/bin`,
`/Library/Frameworks/Python.framework/Versions/*/bin`, `~/.pyenv/shims`, uv-managed pythons;
Linux: `/usr/bin`, `/usr/local/bin`; Windows: `%LOCALAPPDATA%\Programs\Python\Python3*`,
`C:\Program Files\Python3*`, the `py` launcher; the Microsoft Store stub is skipped), each
verified as 3.9+. Only if none works do the launchers themselves refuse commands that look like
installs or downloads (a short built-in pattern list) and let everything else through, with a
`fix:` line. If `guard.py` crashes on an install-looking command, it refuses; on anything else it
lets the command through and logs the crash.

## 8. Testing

- pytest on Ubuntu, macOS and Windows × Python 3.9 and 3.13 (the six required checks): every rule
  with a refused case and a must-pass control, source resolution from flags, environment and
  config files (per-OS paths via fixtures), extra-index refusal, routes (host, prefix, org, repo,
  localhost), policy tighten-only, approvals for loosening, policy-file edit refusal, watcher
  output signals, launchers (Python probing, fail-closed fallback), and each tool's input and
  output format (Claude Code, Copilot, Kiro), run through the real launchers.
- A CI step prints an **interception table** (command → decision) so a reader can see it work.
- Live in tmux on the owner's machine: Claude Code, Copilot CLI, and Kiro CLI once installed; each
  agent is asked to install and download things and is shown to be stopped; pane captures kept.

## 9. Repo and releases

Same as Plumb and Slate: `AGENTS.md` (+ `CLAUDE.md` importing it), MIT, `main` with the same
rulesets and settings, CHANGELOG-driven releases (`## X.Y.Z — date`, Highlights/Added/…), every
change through a PR the owner merges, no AI attribution, semver from 1.0.0.
