"""Split, unwrap and tokenize shell command lines for bash, PowerShell and cmd.

`split_commands(line, shell)` turns one command line (or a whole script) into the simple commands
it runs, as `Cmd` records. Every later stage (detection, source resolution, decisions) reads this
output, so a command missed here is an install the guard misses: when in doubt this module
returns more commands, never fewer.

Contract (shared type, see the build plan):

    Cmd.argv     the words of one simple command, quotes and escapes removed
    Cmd.raw      the source text of that command
    Cmd.shell    "bash" | "powershell" | "cmd": the syntax the command was parsed in
    Cmd.pipe_to  (addition) argv0 of every command that consumes this command's output: each later
                 stage of the same pipeline, every wrapper a stage unwraps through
                 (`curl x | sudo sh` gives ["sudo", "sh"]), and for `$( … )`, backticks,
                 `<( … )` and PowerShell `( … )` the enclosing command (`iex (iwr x)` gives
                 iwr pipe_to ["iex"]). argv0 is as written (`/bin/sh`, `npm.cmd`).
    Cmd.env      (addition) environment assignments in effect for the command: `A=1 cmd`,
                 `env A=1 cmd`, `sudo A=1 cmd`, and earlier `export A=1`, `A=1`,
                 `$env:A = '1'`, `set A=1` in the same line. Read only, and enforced: one
                 mapping per env state is shared by every Cmd in that state (a dict subclass whose
                 mutators raise TypeError; `dict(cmd.env)` gives a private copy).
    Cmd.cwd      (addition) best-effort working directory relative to the starting cwd, from an
                 earlier `cd`/`pushd` (bash), `cd`/`Set-Location`/`sl`/`chdir` (PowerShell) or
                 `cd`/`cd /d`/`chdir` (cmd) in the same input. None means unchanged OR unknown:
                 `cd`, `cd -`, `popd`, `cd $VAR`/`%VAR%`, and a conditional cd (`cd /b || true`,
                 `false || cd /b`) all reset it to None. A `cd` inside a subshell `( … )` or a
                 background job (`cd /b &`, bash/PowerShell) is not assumed to leak out. Treat it
                 as a hint. Read only.

    Cmd.truncated  (addition) True when this reading of the command is approximate: pipe_to had
                 consumers dropped by the cap below; a `parallel`/`sem` expansion was capped (jobs
                 or bytes) so its arguments were appended to the template once instead; or an
                 unknown or optional-value `parallel`/`sem` option (`-l 1`, `--eof x`, `-Y v`)
                 may or may not take the next word, so both readings were emitted. Task 3 treats
                 a truncated Cmd like budget_exceeded ("ask").
    Cmd.budget_exceeded  (addition) True on every Cmd when the parse budget ran out (see below).

`pipe_to` is deduplicated by program name (`/p0/tee` and `/p1/tee` are both `tee`) and holds at
most 64 names: every program this parser treats as running its input (_EXEC_SINKS: all POSIX
shells, powershell/pwsh/powershell_ise, cmd, iex/Invoke-Expression, parallel, sudo/su/sg/…,
xargs, tee, python/node/perl/ruby/…) is always kept; other consumers are capped (nearest kept),
and `truncated` is set when one was dropped.

Parse budget (defence in depth against inputs crafted to be slow): parsing stops after 200,000
tokenized stages, 1,500,000 units of work (tokenized words, env entries copied into a new env
state, and the words of each argv an unwrap step produces, e.g. `sudo sudo … <many words>`), or
0.45 s of wall clock (time.perf_counter, checked every ~1024 stages / ~4096 words of either kind).
split_commands then returns everything parsed before the cut as a `Cmds` list whose
`budget_exceeded` attribute is True (and every Cmd has `budget_exceeded` set), with one extra
marker Cmd appended whose `raw` starts with BUDGET_EXCEEDED. When the clock runs out while
tokenizing, what was tokenized is still emitted within a grace period of half the budget (so a
leading `npm i x` is kept); when the work budget runs out while emitting env states, emission stops
there (likewise for unwrap work). Text re-parsed after a cut (`bash -c '…'`) is not tokenized. Task 3 reads
`cmds.budget_exceeded` (or any Cmd's flag) and treats the whole line as "ask": commands past the
cut were not seen. `parallel`/`sem` also bound the text a template expands (64 jobs, 64 KB); past
that they emit the template once with the arguments appended and set `truncated`. Without a
template each argument is its own command and nothing is capped (the text is the input's own).

Unresolvable commands (Task 3 treats these as suspicious — "unknown-exec" → ask; they are NOT
marked here with the markers below): an argv0 that contains `$`, a backtick, `(` or a `%…%`
variable (its real program is only known at run time); a shell alias (`alias n=npm; n i x` —
aliases are not expanded); and `eval`/`iex`/`Invoke-Expression` or `bash -c`/`cmd /c` whose
argument is a variable or an expression rather than a literal string (the text to run is unknown
here).

Out of scope: `ssh host <cmd>` (and `ssh … -- <cmd>`) runs on the remote host, so it is not
unwrapped and its inner command is not parsed; only the local `ssh` invocation is returned. The
same holds for other remote front ends.

Splitting: `&&`, `||`, `;`, `|`, `&`, newlines, `$( … )`, backticks, `( … )` and `{ …; }` groups,
`<( … )`; PowerShell `;`, `|`, `&&`, `||`, `$( … )`, `( … )`, `{ … }` script blocks; cmd `&`,
`&&`, `||`, `|`, `( … )`. cmd does not split on `;`. Comments (`#`, `<# #>`, `REM`, `::`) and
heredoc / here-string bodies are dropped, except a body fed to a shell (`bash <<EOF`,
`cat <<EOF | sh`, `bash <<< '…'`, `echo '…' | bash`, `'…' | iex`), which is parsed as a script,
and `$( … )` inside an unquoted heredoc, which runs.

Unwrapping: sudo, doas, env, time, nohup, xargs, exec, command, nice, timeout, stdbuf, ionice,
caffeinate, setsid, busybox, runuser, pkexec, chronic, unbuffer, coproc, chroot, firejail, nsenter,
systemd-run, zsh noglob/nocorrect/`=cmd`, flock (incl. its own `-c`), `script -c`, sg (`-c` or a
command string), parallel/sem (one job per `:::` or `--arg-sep` argument, `{}` or the `-I`/
`--replace` string replaced; documented short and long value options skipped; an optional-value
or unknown option followed by a plain word is read both ways, flagged `truncated`; no template =
the arguments, or stdin lines, are commands), csh/tcsh -c, find -exec, watch, su -c, eval, bash/sh/zsh/… -c,
cmd /c, call, start, runas, wsl, powershell/pwsh -Command / -EncodedCommand (base64 UTF-16LE,
decoded) / positional command, Start-Process (-FilePath, -ArgumentList), Invoke-Expression/iex,
and the PowerShell call operators `&` and `.`. The wrapper's Cmd is kept and each unwrapped
command follows it as its own Cmd (order: source order, a wrapper before what it runs). Wrappers
that re-parse a string (-c, /c, -Command, iex, eval, stdin scripts) nest at most MAX_DEPTH deep;
text beyond that is returned whitespace-split with `raw` starting with DEPTH_LIMIT.

cmd specifics: `cmd /c …` re-parses the rest of the command line from its raw source text. Written
in cmd, the outer cmd's caret layer is removed first (`^&` -> `&`, `^"` -> `"`) and then the
child's outer-quote rule (a leading `"` and the last `"` are removed), so
`cmd /c ^"x ^& pip install y^"` runs `x` and `pip install y` and `cmd /c "start "t" npm i x"` runs
`start "t" npm i x`. From bash or PowerShell there is no outer caret layer: the child parses the
text as given (`^"` is a literal quote that opens no quoting) plus, conservatively, the
caret-stripped readings. After `cmd … /c` parentheses are part of the child's command line, not a
block (`cmd /c (npm ^& pip install y)`). Switches may be glued (`cmd/s/c`, `cmd.exe/c`,
`cmd /q /d/c`, `cmd/q`, `cmd.exe/v:on`) when the glued tail is entirely real cmd switches (values
only on /t /e /v /f, never containing `/`); without /c, /k or /r such a cmd reads its commands from
stdin. Any other tail is a folder named cmd (`cmd/c:x/sudo …` runs sudo). In cmd the first word's
raw text is checked too, so a quote or caret glued to the switch (`cmd/c"pip install x"`,
`cmd/s/c^"x ^& y^"`, `cmd/c^ pip install x`) starts the child's command line. `start` reads a first
argument that begins with `"` as the window title, never as the program (`start npm"" i x` runs
npm).

Malformed input never raises. Unterminated quotes or substitutions make that script level fall
back to a whitespace split on the separators above, with `raw` starting with MALFORMED; an
undecodable -EncodedCommand and an env flood (more than 64 variables) are marked the same way.
Later stages should treat a marked Cmd as suspicious.

Tokenizers are regex-driven instead of `shlex` (which alone takes about 200 ms on 1 MB); the
tests check the POSIX tokens against `shlex.split`.
"""

from __future__ import annotations

import base64
import gc
import re
import shlex
import time
from dataclasses import dataclass, field

MAX_DEPTH = 5
MALFORMED = "[malformed] "
DEPTH_LIMIT = "[depth-limit] "
BUDGET_EXCEEDED = "[budget-exceeded] "

_MAX_NEST = 100  # syntactic nesting ($( … ) inside $( … ) …) before a script counts as malformed
_MAX_ENV = 64  # tracked environment variables before the line counts as malformed
_MAX_UNWRAP = 64  # argv-level unwraps of one command (sudo sudo sudo …)
_MAX_PIPE_TOTAL = 64  # names in one Cmd.pipe_to (sinks always fit; others share the rest)
_MAX_TICKS = 200_000  # tokenized stages before the parse budget runs out
_MAX_SECONDS = 0.45  # wall clock before the parse budget runs out
_MAX_PARALLEL_JOBS = 64  # `parallel … ::: a b c` jobs expanded into commands
_MAX_PARALLEL_BYTES = 64 * 1024  # total text _unwrap_parallel may generate before truncating
_MAX_WORDS = 1_500_000  # tokenized words (plus materialised env entries) before the parse budget runs out
_GRACE = 0.5  # after a time cut while tokenizing, emission of what was parsed gets this share of _MAX_SECONDS


@dataclass
class Cmd:
    argv: list[str]
    raw: str
    shell: str
    pipe_to: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: "str | None" = None
    truncated: bool = False
    budget_exceeded: bool = False


class _FrozenEnv(dict):
    """Cmd.env: a read-only dict shared by every Cmd with the same environment state."""

    __slots__ = ()

    def _ro(self, *_a, **_k):
        raise TypeError("Cmd.env is read only (shared between commands); copy it with dict(cmd.env)")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _ro

    def __reduce__(self):
        return (_FrozenEnv, (dict(self),))

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


_EMPTY_ENV = _FrozenEnv()


class _EnvBox:
    """The current environment of one script level (shared by its groups, not by re-parses)."""

    __slots__ = ("cur",)

    def __init__(self, cur: _FrozenEnv) -> None:
        self.cur = cur


class Cmds(list):
    """split_commands' result: a list of Cmd plus `budget_exceeded` (see the module docstring)."""

    budget_exceeded = False


def split_commands(line: str, shell: str = "bash") -> list[Cmd]:
    """Every simple command `line` runs in `shell` ("bash", "powershell" or "cmd")."""
    if isinstance(line, bytes):
        line = line.decode("utf-8", "replace")
    elif not isinstance(line, str):
        line = "" if line is None else str(line)
    sh = _shell_name(shell)
    if line.startswith("\ufeff"):
        line = line[1:]
    out: list[Cmd] = []
    # Parsing builds many small objects and no reference cycles: pausing the cyclic collector
    # saves up to a third of the time on large input. Reference counting still frees everything.
    collecting = gc.isenabled()
    gc.disable()
    try:
        state = _State(budget=True)
        try:
            _parse(line, sh, 0, _EMPTY_ENV, _EMPTY_PIPE, out, state, None)
        except Exception:  # last resort: never raise into a hook
            out = []
            _emit_crude(_crude(line, sh), sh, out)
        result = Cmds(_dedup(out))
        if state.cut or state.timeup or state.stop:
            result.budget_exceeded = True
            for c in result:
                c.budget_exceeded = True
            # keep what was parsed before the cut AND a marker, so later stages always see the flag
            words = line[:4096].translate(_CRUDE_STRIP[sh]).split()[:16] or ["?"]
            marker = Cmd(words, BUDGET_EXCEEDED + line[:200].strip(), sh)
            marker.budget_exceeded = True
            result.append(marker)
        return result
    finally:
        if collecting:
            gc.enable()


# --- shared structures ---------------------------------------------------------------------------


class _Malformed(Exception):
    pass


class _State:
    """Per-call state: env overflow and the parse budget (a no-limit state when budget=False)."""

    __slots__ = ("overflow", "cut", "stop", "timeup", "ticks", "words", "emits", "deadline", "emit_deadline",
                 "max_ticks", "max_words", "env_keys", "envs")

    def __init__(self, budget: bool = False) -> None:
        self.overflow = False  # more env variables than _MAX_ENV: later Cmds are marked
        self.cut = False  # stop tokenizing (stage, word, env-work or time budget)
        self.stop = False  # stop emitting too (emission past its deadline, or env work over budget)
        self.timeup = False  # the time budget fired
        self.ticks = 0
        self.words = 0  # work: tokenized words plus materialised env entries
        self.emits = 0
        self.env_keys: dict = {}  # id(assigns) -> (assigns, content key): keeps the dict alive
        self.envs: dict = {}  # (id(base env), content key) -> (base, merged): one mapping per env state
        if budget:
            self.deadline = time.perf_counter() + _MAX_SECONDS
            self.max_ticks = _MAX_TICKS
            self.max_words = _MAX_WORDS
        else:
            self.deadline = float("inf")
            self.max_ticks = float("inf")
            self.max_words = float("inf")
        self.emit_deadline = self.deadline

    def _time_cut(self) -> None:
        # tokenizing stops here; what was tokenized is still emitted, within a grace period
        self.cut = self.timeup = True
        if self.emit_deadline == self.deadline:
            self.emit_deadline = time.perf_counter() + max(_MAX_SECONDS * _GRACE, 0.01)

    def check(self) -> None:
        """Called by _Builder every 1024 tokenized stages and once past max_ticks."""
        if self.ticks > self.max_ticks:
            self.cut = True
        elif time.perf_counter() > self.deadline:
            self._time_cut()

    def wcheck(self) -> None:
        """Called by _Builder every 4096 tokenized words."""
        if self.words > self.max_words:
            self.cut = True
        elif time.perf_counter() > self.deadline:
            self._time_cut()

    def late(self) -> bool:
        """One emitted stage; True once emission must stop (the clock is checked every 256 stages)."""
        self.emits += 1
        if self.emits & 255 == 1 and time.perf_counter() > self.emit_deadline:
            self.cut = self.timeup = self.stop = True
        return self.stop

    def work(self, n: int) -> None:
        """Copied words (env entries, unwrapped argv) count toward the word budget; past it, or
        past the emission deadline (checked whenever 4096 more words were copied), emission stops."""
        before = self.words
        self.words += n
        if self.words > self.max_words:
            self.cut = self.stop = True
        elif (before ^ self.words) >> 12 and time.perf_counter() > self.emit_deadline:
            self.cut = self.timeup = self.stop = True


class _Ctx:
    __slots__ = ("s", "n", "heredocs", "state")

    def __init__(self, s: str, state: "_State | None" = None) -> None:
        self.s = s
        self.n = len(s)
        self.heredocs: list = []
        self.state = state if state is not None else _State()


class _Stage:
    """One simple command (or group) while tokenizing."""

    __slots__ = ("words", "joins", "raws", "start", "end", "subs", "stdin", "group", "literal", "bad", "raw", "op")

    def __init__(self, raws: bool = False) -> None:
        self.words: list[str] = []
        self.joins: set[int] | None = None  # PowerShell: indexes of words joined to the one before by `,`
        self.raws: list[str] | None = [] if raws else None  # cmd: each word's source text, quotes kept
        self.start = -1
        self.end = -1
        self.subs: list = []  # nested scripts: $( … ), backticks, <( … ), PS ( … ) and { … }
        self.stdin: list[str] = []  # heredoc and here-string bodies
        self.group = None  # ( … ) / { …; } group: a script
        self.literal = False  # PowerShell: a word was one quoted string (a lone one is a string literal)
        self.bad = False
        self.raw = ""
        self.op = None  # on a pipeline's last stage: the list operator after it ("&&", "||", "&")

    def empty(self) -> bool:
        return not (self.words or self.subs or self.stdin or self.group is not None)


class _Builder:
    __slots__ = ("s", "script", "pipe", "stage", "comma", "state", "keep_raws")

    def __init__(self, s: str, state: _State, keep_raws: bool = False) -> None:
        self.s = s
        self.script: list = []
        self.pipe: list = []
        self.keep_raws = keep_raws
        self.stage = _Stage(keep_raws)
        self.comma = False
        self.state = state

    def span(self, a: int, b: int) -> None:
        st = self.stage
        if st.start < 0:
            st.start = a
        if b > st.end:
            st.end = b

    def add_word(self, w: str, a: int, b: int, literal: bool = False) -> None:
        st = self.stage
        st.words.append(w)
        if st.start < 0:
            st.start = a
        st.end = b  # tokens only move forward
        if self.comma:
            if st.joins is None:
                st.joins = set()
            st.joins.add(len(st.words) - 1)
            self.comma = False
        if literal:
            st.literal = True
        if st.raws is not None:
            st.raws.append(self.s[a:b])
        budget = self.state
        budget.words += 1
        if not budget.words & 4095:
            budget.wcheck()

    def add_group(self, sub: list, a: int, b: int) -> None:
        if self.stage.group is not None:
            self.end_stage()
        self.stage.group = sub
        self.span(a, b)

    def end_stage(self) -> None:
        st = self.stage
        if not st.empty():
            if st.start >= 0:
                st.raw = self.s[st.start : st.end].strip()
            self.pipe.append(st)
            budget = self.state
            budget.ticks += 1
            if not budget.ticks & 1023 or budget.ticks > budget.max_ticks:
                budget.check()
        self.stage = _Stage(self.keep_raws)
        self.comma = False

    def end_pipe(self, op: "str | None" = None) -> None:
        self.end_stage()
        if self.pipe:
            self.pipe[-1].op = op
            self.script.append(self.pipe)
            self.pipe = []

    def finish(self) -> list:
        self.end_pipe()
        return self.script


def _skip_blanks(s: str, i: int) -> int:
    n = len(s)
    while i < n and s[i] in " \t":
        i += 1
    return i


def _subs_in_text(text: str, subs: list, level: int, shell: str, state: "_State | None" = None) -> None:
    """Command substitutions inside an expandable heredoc / here-string body."""
    pat = _B_SUBST if shell == "bash" else _P_SUBST
    if not pat.search(text):
        return
    ctx = _Ctx(text, state)
    pos = 0
    while True:
        m = pat.search(text, pos)
        if not m:
            return
        try:
            if m.group() == "`":
                _, pos = _b_backtick(ctx, m.start(), subs, level)
            elif shell == "bash":
                sub, pos = _b_list(ctx, m.end(), ")", level + 1)
                subs.append(sub)
            else:
                sub, pos = _p_list(ctx, m.end(), ")", level + 1)
                subs.append(sub)
        except _Malformed:
            subs.append(_crude(text[m.end() :], shell))
            return


# --- bash / sh / zsh -----------------------------------------------------------------------------

_B_WS = re.compile(r"[ \t\r\f\v]+")
_B_PLAIN = re.compile(r"[^ \t\r\n\f\v'\"\\$`;&|()<>]+")
_B_DQ = re.compile(r'[^"\\$`]+')
_B_ANSI = re.compile(r"[^\\']+")
_B_BT_STOP = re.compile(r"[\\`]")
_B_REDIR = re.compile(r">>|>&|>\||<&|<>|>|<")
_B_SUBST = re.compile(r"\$\((?!\()|`")
_PARENS = re.compile(r"[()]")
_BRACES = re.compile(r"[{}]")
_FD = re.compile(r"[0-9]+")
_SPLIT = "\x00"  # marks an unquoted $IFS inside a word (bash arguments cannot contain NUL)
_B_END = r"(?=[ \t\r\n\f\v;&|()]|\Z)"
# Fast path, with the blanks after it: 1 a plain word (no comment, brace keyword or `2>`),
# 2 a lone '…' word, 3 a lone "…" word without $ ` \, 4 a list or pipeline operator (`&>` redirects).
_B_TOK = re.compile(
    r"(?:([^ \t\r\n\f\v'\"\\$`;&|()<>#{}][^ \t\r\n\f\v'\"\\$`;&|()<>]*)" + _B_END
    + r"|'([^']*)'" + _B_END
    + r'|"([^"\\$`]*)"' + _B_END
    + r"|(;[;&]*|&&|&(?!>)|\|\||\|&?))[ \t]*"
)
_C_ESCAPES = {
    "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r",
    "t": "\t", "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?",
}  # fmt: skip


def _b_list(ctx: _Ctx, i: int, closer: str | None, level: int) -> tuple[list, int]:
    if level > _MAX_NEST:
        raise _Malformed("nesting")
    s, n = ctx.s, ctx.n
    b = _Builder(s, ctx.state)
    budget = ctx.state
    while i < n and not budget.cut:
        m = _B_TOK.match(s, i)
        if m:
            g = m.lastindex
            if g == 4:
                op = m.group(4)
                if op[0] == "|" and op != "||":
                    b.end_stage()
                else:
                    b.end_pipe(op if op in ("&&", "||", "&") else None)
            else:
                b.add_word(m.group(g), i, m.end(g) + (g > 1))
            i = m.end()
            continue
        c = s[i]
        if c in " \t\r\f\v":
            i = _B_WS.match(s, i).end()
            continue
        if c == "\n":
            if not (b.pipe and b.stage.empty()):  # a newline right after `|` continues the pipeline
                b.end_pipe()
            i += 1
            if ctx.heredocs:
                i = _b_heredocs(ctx, i, level)
            continue
        if c == "\\" and (s.startswith("\\\n", i) or s.startswith("\\\r\n", i)):
            i += 2 if s[i + 1] == "\n" else 3
            continue
        if c == "#":
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "&":  # &> and &>> (the other & operators are _B_SEP)
            i = _b_target(ctx, b, i + (3 if s.startswith("&>>", i) else 2), i, level)
            continue
        if c == "(":
            if b.stage.words:
                b.end_pipe()
                if s.startswith("()", i):  # name () { … }
                    i += 2
                    continue
            a = i
            sub, i = _b_list(ctx, i + 1, ")", level + 1)
            b.add_group(sub, a, i)
            continue
        if c == ")":
            if closer == ")":
                return b.finish(), i + 1
            b.end_pipe()
            i += 1
            continue
        if c in "<>":
            if i + 1 < n and s[i + 1] == "(":  # process substitution
                a = i
                sub, i = _b_list(ctx, i + 2, ")", level + 1)
                b.stage.subs.append(sub)
                b.add_word(s[a:i], a, i)
            else:
                i = _b_redirect(ctx, b, i, i, level)
            continue
        a = i
        w, i = _b_word(ctx, i, b.stage.subs, level)
        if i == a:  # defensive: never loop without progress
            w = s[i]
            i += 1
        if i < n and s[i] in "<>" and _FD.fullmatch(s, a, i):  # 2>file
            i = _b_redirect(ctx, b, i, a, level)
            continue
        if _SPLIT in w or w[:1] == "{":
            parts = _b_expand(w, s[a:i])
            if parts != [w]:
                for part in parts:
                    b.add_word(part, a, i)
                continue
        if i - a == 1:
            if w == "}" and closer == "}" and not b.stage.words:
                return b.finish(), i
            if w == "{" and (not b.stage.words or b.stage.words[0] == "function"):
                b.end_pipe()
                sub, i = _b_list(ctx, i, "}", level + 1)
                b.add_group(sub, a, i)
                continue
        b.add_word(w, a, i)
    if closer is not None and not budget.cut:
        raise _Malformed("unclosed " + closer)
    return b.finish(), i


def _b_redirect(ctx: _Ctx, b: _Builder, i: int, a: int, level: int) -> int:
    s = ctx.s
    if s.startswith("<<<", i):
        i = _skip_blanks(s, i + 3)
        w, i = _b_word(ctx, i, b.stage.subs, level)
        b.stage.stdin.append(w)
        b.span(a, i)
        return i
    if s.startswith("<<", i):
        strip = s.startswith("<<-", i)
        i = _skip_blanks(s, i + (3 if strip else 2))
        d0 = i
        delim, i = _b_word(ctx, i, [], level)
        quoted = any(q in s[d0:i] for q in "'\"\\")
        ctx.heredocs.append((b.stage, delim, strip, quoted))
        b.span(a, i)
        return i
    m = _B_REDIR.match(s, i)
    return _b_target(ctx, b, m.end(), a, level)


def _b_target(ctx: _Ctx, b: _Builder, i: int, a: int, level: int) -> int:
    s, n = ctx.s, ctx.n
    i = _skip_blanks(s, i)
    if i < n and s[i] not in "\n\r;&|()<>":
        _, i = _b_word(ctx, i, b.stage.subs, level)
    b.span(a, i)
    return i


def _b_heredocs(ctx: _Ctx, i: int, level: int) -> int:
    s, n = ctx.s, ctx.n
    pending, ctx.heredocs = ctx.heredocs, []
    for stage, delim, strip, quoted in pending:
        end, after = _heredoc_end(s, i, delim, strip)
        body, i = s[i:end], after
        if strip:
            body = re.sub(r"(?m)^\t+", "", body)
        stage.stdin.append(body)
        if not quoted:
            _subs_in_text(body, stage.subs, level, "bash", ctx.state)
    return i


def _heredoc_end(s: str, i: int, delim: str, strip: bool) -> tuple[int, int]:
    """Where a heredoc body starting at `i` ends, and where the line after its delimiter starts."""
    n = len(s)
    if not delim:  # <<'' ends at the first empty line
        j = i - 1 if s.startswith("\n", i) else s.find("\n\n", i)
        return (n, n) if j < 0 else (j + 1, j + 2)
    k = i
    while True:
        k = s.find(delim, k)
        if k < 0:
            return n, n  # unterminated: the body runs to the end
        a = k
        while strip and a > i and s[a - 1] == "\t":
            a -= 1
        e = k + len(delim)
        if e < n and s[e] == "\r":
            e += 1
        if (a == i or s[a - 1] == "\n") and (e >= n or s[e] == "\n"):
            return a, min(e + 1, n)
        k = s.find("\n", k) + 1  # a delimiter line starts its line: try the next line
        if k == 0:
            return n, n


def _b_word(ctx: _Ctx, i: int, subs: list, level: int) -> tuple[str, int]:
    s, n = ctx.s, ctx.n
    parts: list[str] = []
    while i < n:
        m = _B_PLAIN.match(s, i)
        if m:
            parts.append(m.group())
            i = m.end()
            continue
        c = s[i]
        if c == "'":
            j = s.find("'", i + 1)
            if j < 0:
                raise _Malformed("quote")
            parts.append(s[i + 1 : j])
            i = j + 1
        elif c == '"':
            v, i = _b_dquote(ctx, i + 1, subs, level)
            parts.append(v)
        elif c == "\\":
            if i + 1 >= n:
                parts.append("\\")
                i += 1
            elif s[i + 1] == "\n":
                i += 2
            elif s.startswith("\r\n", i + 1):
                i += 3
            else:
                parts.append(s[i + 1])
                i += 2
        elif c == "$":
            if s.startswith("${IFS}", i):  # unquoted $IFS splits the word: npm${IFS}i
                parts.append(_SPLIT)
                i += 6
            elif s.startswith("$IFS", i) and not (s[i + 4 : i + 5].isalnum() or s[i + 4 : i + 5] == "_"):
                parts.append(_SPLIT)
                i += 4
            else:
                v, i = _b_dollar(ctx, i, subs, level)
                parts.append(v)
        elif c == "`":
            v, i = _b_backtick(ctx, i, subs, level)
            parts.append(v)
        else:
            break
    return "".join(parts), i


def _b_expand(w: str, raw: str) -> list[str]:
    """Words an unquoted word becomes: $IFS splits it, a plain {a,b,c} expands to a b c."""
    if raw == w and len(w) > 2 and w[0] == "{" and w[-1] == "}" and "," in w and not _BRACES.search(w, 1, len(w) - 1):
        return [x for x in w[1:-1].split(",") if x]
    return [x for x in w.split(_SPLIT) if x]


def _b_dquote(ctx: _Ctx, i: int, subs: list, level: int) -> tuple[str, int]:
    s, n = ctx.s, ctx.n
    parts: list[str] = []
    while i < n:
        m = _B_DQ.match(s, i)
        if m:
            parts.append(m.group())
            i = m.end()
            continue
        c = s[i]
        if c == '"':
            return "".join(parts), i + 1
        if c == "\\":
            nx = s[i + 1 : i + 2]
            if nx and nx in '"\\$`':
                parts.append(nx)
                i += 2
            elif nx == "\n":
                i += 2
            else:
                parts.append("\\")
                i += 1
        elif c == "$":
            if s[i + 1 : i + 2] in ("(", "{"):
                v, i = _b_dollar(ctx, i, subs, level)
                parts.append(v)
            else:
                parts.append("$")
                i += 1
        else:  # backtick
            v, i = _b_backtick(ctx, i, subs, level)
            parts.append(v)
    raise _Malformed("double quote")


def _b_dollar(ctx: _Ctx, i: int, subs: list, level: int) -> tuple[str, int]:
    s = ctx.s
    nx = s[i + 1 : i + 2]
    if nx == "(":
        if s.startswith("$((", i):  # arithmetic, not a command
            depth = 2
            for m in _PARENS.finditer(s, i + 3):
                depth += 1 if m.group() == "(" else -1
                if depth == 0:
                    _subs_in_text(s[i + 3 : m.end() - 2], subs, level, "bash", ctx.state)
                    return s[i : m.end()], m.end()
            raise _Malformed("arithmetic")
        sub, j = _b_list(ctx, i + 2, ")", level + 1)
        subs.append(sub)
        return s[i:j], j
    if nx == "{":
        depth = 1
        for m in _BRACES.finditer(s, i + 2):
            depth += 1 if m.group() == "{" else -1
            if depth == 0:
                _subs_in_text(s[i + 2 : m.end() - 1], subs, level, "bash", ctx.state)  # ${x:-$(cmd)} runs cmd
                return s[i : m.end()], m.end()
        raise _Malformed("parameter")
    if nx == "'":
        return _b_ansi(s, i + 2)
    if nx == '"':
        return _b_dquote(ctx, i + 2, subs, level)
    return "$", i + 1


def _b_ansi(s: str, i: int) -> tuple[str, int]:
    """$'…' ANSI-C quoting, decoded the way bash does."""
    n = len(s)
    out: list[str] = []
    while i < n:
        m = _B_ANSI.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        if s[i] == "'":
            return "".join(out), i + 1
        if i + 1 >= n:
            break
        e = s[i + 1]
        if e in _C_ESCAPES:
            out.append(_C_ESCAPES[e])
            i += 2
            continue
        if e in "xuU01234567":
            if e in "01234567":
                digits = re.match(r"[0-7]{1,3}", s[i + 1 : i + 4]).group()
                base, step = 8, 1
            else:
                width = {"x": 2, "u": 4, "U": 8}[e]
                hit = re.match(r"[0-9a-fA-F]{1,%d}" % width, s[i + 2 : i + 2 + width])
                digits, base, step = (hit.group() if hit else ""), 16, 2
            if digits:
                code = int(digits, base)
                out.append(chr(code) if code <= 0x10FFFF else "?")
                i += step + len(digits) - (1 if base == 8 else 0)
                continue
        if e == "c" and i + 2 < n:
            out.append(chr(ord(s[i + 2]) & 31))
            i += 3
            continue
        out.append("\\" + e)
        i += 2
    raise _Malformed("ansi-c quote")


def _b_backtick(ctx: _Ctx, i: int, subs: list, level: int) -> tuple[str, int]:
    s, n = ctx.s, ctx.n
    j = i + 1
    out: list[str] = []
    while True:
        k = _B_BT_STOP.search(s, j)
        if not k:
            raise _Malformed("backtick")
        p = k.start()
        out.append(s[j:p])
        if s[p] == "`":
            break
        if p + 1 < n and s[p + 1] in "`\\$":
            out.append(s[p + 1])
            j = p + 2
        else:
            out.append("\\")
            j = p + 1
    sub, _ = _b_list(_Ctx("".join(out), ctx.state), 0, None, level + 1)
    subs.append(sub)
    return s[i : p + 1], p + 1


# --- PowerShell ----------------------------------------------------------------------------------

_SQ = "'\u2018\u2019\u201a\u201b"
_DQ = '"\u201c\u201d\u201e'
_P_WS = re.compile(r"[^\S\n]+")
_P_PLAIN = re.compile(r"[^\s'\"\u2018\u2019\u201a\u201b\u201c\u201d\u201e`$;|&(){},<>@]+")
_P_SQ = re.compile(r"[^'\u2018\u2019\u201a\u201b]+")
_P_DQ = re.compile(r"[^\"\u201c\u201d\u201e`$]+")
_P_END = r"(?=[\s;|&)},]|\Z)"
# Fast path, with the blanks after it: 1 a plain word (no comment, `2>`), 2 a lone '…' word,
# 3 a lone "…" word without ` $, 4 `;`, `&&`, `||` or `|`.
_P_TOK = re.compile(
    r"(?:([^\s'\"\u2018\u2019\u201a\u201b\u201c\u201d\u201e`$;|&(){},<>@#]"
    r"[^\s'\"\u2018\u2019\u201a\u201b\u201c\u201d\u201e`$;|&(){},<>@]*)" + _P_END
    + r"|['\u2018\u2019\u201a\u201b]([^'\u2018\u2019\u201a\u201b]*)['\u2018\u2019\u201a\u201b]" + _P_END
    + r"|[\"\u201c\u201d\u201e]([^\"\u201c\u201d\u201e`$]*)[\"\u201c\u201d\u201e]" + _P_END
    + r"|(;|&&|\|\|?))[^\S\n]*"
)
_P_REDIR = re.compile(r"[1-6*]?>>?(?:&[12])?|<")
_P_HERE_START = re.compile(r"[ \t]*\r?\n")
_P_HERE_END = re.compile(r"\r?\n[" + _SQ + _DQ + "]@")
_P_SUBST = re.compile(r"\$\(")
_P_ESCAPES = {"0": "\0", "a": "\a", "b": "\b", "e": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}


def _p_list(ctx: _Ctx, i: int, closer: str | None, level: int) -> tuple[list, int]:
    if level > _MAX_NEST:
        raise _Malformed("nesting")
    s, n = ctx.s, ctx.n
    b = _Builder(s, ctx.state)
    budget = ctx.state
    while i < n and not budget.cut:
        m = _P_TOK.match(s, i)
        if m and m.group(1) != "--%":
            g = m.lastindex
            if g == 4:
                if m.group(4) == "|":
                    b.end_stage()
                else:
                    b.end_pipe(m.group(4) if m.group(4) != ";" else None)
            else:
                b.add_word(m.group(g), i, m.end(g) + (g > 1), g > 1)
            i = m.end()
            continue
        c = s[i]
        if c == "\n":
            if not (b.comma or (b.pipe and b.stage.empty())):  # `a,` and `a |` continue on the next line
                b.end_pipe()
            i += 1
            continue
        if c.isspace():
            i = _P_WS.match(s, i).end()
            continue
        if c == "`" and (s.startswith("`\n", i) or s.startswith("`\r\n", i)):
            i += 2 if s[i + 1] == "\n" else 3
            continue
        if c == "#":
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "<" and s.startswith("<#", i):
            j = s.find("#>", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == "&":  # (&& is _P_SEP)
            if not b.stage.words:  # call operator
                b.add_word("&", i, i + 1)
                i += 1
            else:  # background job
                b.end_pipe("&")
                i += 1
            continue
        if c in ")}":
            if c == closer:
                return b.finish(), i + 1
            b.end_pipe()
            i += 1
            continue
        if c == ",":
            b.comma = True
            i += 1
            continue
        if c in "<>" or (c in "123456*" and s[i + 1 : i + 2] == ">"):
            m = _P_REDIR.match(s, i)
            if m:
                a, i = i, m.end()
                if not m.group().endswith(("&1", "&2")):
                    i = _skip_blanks(s, i)
                    if i < n and s[i] not in "\r\n;|&)}":
                        _, i, _, _ = _p_word(ctx, i, b.stage.subs, level)
                b.span(a, i)
                continue
        a = i
        w, i, lit, extra = _p_word(ctx, i, b.stage.subs, level)
        if i == a:  # defensive: never loop without progress
            w, i, lit, extra = s[i], i + 1, False, []
        if w == "--%" and s[a:i] == "--%":  # stop-parsing token: the rest of the line is literal
            j = s.find("\n", i)
            j = n if j < 0 else j
            for t in _win_split(s[i:j]):
                b.add_word(t, i, j)
            i = j
            continue
        b.add_word(w, a, i, lit)
        for ew in extra:  # elements of a string-array literal: ('a','b') -> a b
            b.comma = True  # joined like `'a','b'`, so an enclosing ( … ) is still a literal
            b.add_word(ew, a, i, True)
    if closer is not None and not budget.cut:
        raise _Malformed("unclosed " + closer)
    return b.finish(), i


def _ps_array_literal(sub: list) -> "list[str] | None":
    """`@( … )` / `$( … )`: statements (`;` or newline separated) that are each a pure literal."""
    elems: list = []
    for pipe in sub:
        got = _ps_paren_literal([pipe])
        if got is None:
            return None
        elems.extend(got)
    return elems or None


def _ps_paren_literal(sub: list) -> "list[str] | None":
    """The string elements of `( 'a' )` or `( 'a','b' )`, or None if it is not a pure literal."""
    if len(sub) != 1 or len(sub[0]) != 1:
        return None
    st = sub[0][0]
    if st.subs or st.group is not None or not st.literal or not st.words:
        return None
    words = st.words
    if any("$" in w or "`" in w or "(" in w for w in words):
        return None
    if len(words) == 1:
        return list(words)
    joins = st.joins or set()
    return list(words) if all(j in joins for j in range(1, len(words))) else None


def _p_word(ctx: _Ctx, i: int, subs: list, level: int):
    """One PowerShell argument -> (word, pos, was_one_quoted_string, extra_words).

    `extra_words` are the remaining elements of a bare string-array literal `('a','b')`, or of
    `@( … )` / `$( … )` whose statements are all string literals (`@('a';'b')`, `$('a','b')`).
    """
    s, n = ctx.s, ctx.n
    parts: list[str] = []
    extra: list = []
    pieces = quoted = 0
    while i < n:
        m = _P_PLAIN.match(s, i)
        if m:
            parts.append(m.group())
            i = m.end()
            pieces += 1
            continue
        c = s[i]
        if c in _SQ:
            v, i = _p_squote(s, i + 1)
            quoted += 1
        elif c in _DQ:
            v, i = _p_dquote(ctx, i + 1, subs, level)
            quoted += 1
        elif c == "`":
            if i + 1 >= n:
                v, i = "`", i + 1
            elif s[i + 1] == "\n" or s.startswith("\r\n", i + 1):
                break
            else:
                v, i = _P_ESCAPES.get(s[i + 1], s[i + 1]), i + 2
        elif c == "$":
            nx = s[i + 1 : i + 2]
            if nx == "(":
                a = i
                sub, i = _p_list(ctx, i + 2, ")", level + 1)
                elems = _ps_array_literal(sub) if not parts else None
                if elems is not None:  # $('install','evil') / $('install';'evil') -> install evil
                    v = elems[0]
                    quoted += 1
                    extra = elems[1:]
                else:
                    subs.append(sub)
                    v = s[a:i]
            elif nx == "{":
                j = s.find("}", i + 2)
                if j < 0:
                    raise _Malformed("variable")
                v, i = s[i : j + 1], j + 1
            else:
                v, i = "$", i + 1
        elif c == "@":
            nx = s[i + 1 : i + 2]
            if nx in ("(", "{") and nx:
                a = i
                sub, i = _p_list(ctx, i + 2, ")" if nx == "(" else "}", level + 1)
                elems = _ps_array_literal(sub) if nx == "(" and not parts else None
                if elems is not None:  # @('install','evil'), @('install';'evil') -> install evil
                    v = elems[0]
                    quoted += 1
                    extra = elems[1:]
                else:
                    subs.append(sub)
                    v = s[a:i]
            elif nx and nx in _SQ + _DQ and _P_HERE_START.match(s, i + 2):
                v, i = _p_here(ctx, i, subs, level)
                quoted += 1
            else:
                v, i = "@", i + 1
        elif c == "(" and parts:
            break  # `iex(iwr x)` / `npm('install')`: the `(` starts a separate subexpression token
        elif c in "({":
            a = i
            sub, i = _p_list(ctx, i + 1, ")" if c == "(" else "}", level + 1)
            elems = _ps_paren_literal(sub) if c == "(" else None
            if elems is not None:  # ('install') -> install; ('install','evil') -> install + evil
                v = elems[0]
                quoted += 1
                if len(elems) > 1 and not parts:
                    extra = elems[1:]
            else:
                subs.append(sub)
                v = s[a:i]
        else:
            break
        parts.append(v)
        pieces += 1
    return "".join(parts), i, pieces == 1 and quoted == 1, extra


def _p_squote(s: str, i: int) -> tuple[str, int]:
    n = len(s)
    out: list[str] = []
    while i < n:
        m = _P_SQ.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        if i + 1 < n and s[i + 1] in _SQ:  # '' is a literal quote
            out.append("'")
            i += 2
            continue
        return "".join(out), i + 1
    raise _Malformed("quote")


def _p_dquote(ctx: _Ctx, i: int, subs: list, level: int) -> tuple[str, int]:
    s, n = ctx.s, ctx.n
    out: list[str] = []
    while i < n:
        m = _P_DQ.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        c = s[i]
        if c in _DQ:
            if i + 1 < n and s[i + 1] in _DQ:
                out.append('"')
                i += 2
                continue
            return "".join(out), i + 1
        if c == "`":
            if i + 1 >= n:
                break
            e = s[i + 1]
            if e == "u" and s[i + 2 : i + 3] == "{":
                j = s.find("}", i + 3)
                try:
                    out.append(chr(int(s[i + 3 : j], 16)))
                    i = j + 1
                    continue
                except (ValueError, OverflowError):
                    pass
            out.append(_P_ESCAPES.get(e, e))
            i += 2
        elif s[i + 1 : i + 2] == "(":  # $( … ) subexpression
            a = i
            sub, i = _p_list(ctx, i + 2, ")", level + 1)
            subs.append(sub)
            out.append(s[a:i])
        else:
            out.append("$")
            i += 1
    raise _Malformed("double quote")


def _p_here(ctx: _Ctx, i: int, subs: list, level: int) -> tuple[str, int]:
    """@' … '@ and @" … "@ here-strings."""
    s = ctx.s
    expandable = s[i + 1] in _DQ
    start = _P_HERE_START.match(s, i + 2).end()
    end = _P_HERE_END.search(s, start - 1)
    if not end:
        raise _Malformed("here-string")
    body = s[start : end.start()] if end.start() >= start else ""
    if expandable:
        _subs_in_text(body, subs, level, "powershell", ctx.state)
    return body, end.end()


# --- cmd.exe -------------------------------------------------------------------------------------

_C_WS = re.compile(r"[ \t\r\f\v]+")
_C_PLAIN = re.compile(r'[^ \t\r\n\f\v"^&|<>()]+')
_C_END = r"(?=[ \t\r\n\f\v&|()]|\Z)"
# Fast path, with the blanks after it: 1 a plain word, 2 a lone "…" word, 3 `&`, `&&`, `|`, `||`.
_C_TOK = re.compile(r'(?:([^ \t\r\n\f\v"^&|<>()]+)' + _C_END + r'|"([^"\n]*)"' + _C_END + r"|(&&?|\|\|?))[ \t]*")
_C_REDIR = re.compile(r"[0-9]?(?:>>|>|<)(?:&[0-9])?")


def _c_list(ctx: _Ctx, i: int, closer: str | None, level: int) -> tuple[list, int]:
    if level > _MAX_NEST:
        raise _Malformed("nesting")
    s, n = ctx.s, ctx.n
    b = _Builder(s, ctx.state, True)
    budget = ctx.state
    plit = None  # the stage whose parentheses are literal text (a `cmd /c …` command line)
    pdepth = [0, closer != ")"]
    while i < n and not budget.cut:
        if plit is b.stage and _c_plit_word(s, i, pdepth):
            a = i
            w, i = _c_word(ctx, i, pdepth)
            if i == a:
                w, i = s[i], i + 1
            b.add_word(w, a, i)
            continue
        m = _C_TOK.match(s, i)
        # a first word that may be REM, a :label or start with @ goes the slow way
        if m and (m.lastindex != 1 or b.stage.words or (m.group(1)[0] not in ":@" and m.group(1).lower() != "rem")):
            g = m.lastindex
            if g == 3:
                if m.group(3) == "|":
                    b.end_stage()
                else:  # cmd's `&` runs commands one after another (like `;`)
                    b.end_pipe(m.group(3) if m.group(3) != "&" else None)
            else:
                b.add_word(m.group(g), i, m.end(g) + (g > 1))
            i = m.end()
            continue
        c = s[i]
        if c in " \t\r\f\v":
            i = _C_WS.match(s, i).end()
            continue
        if c == "\n":
            b.end_pipe()
            i += 1
            continue
        if c == "^" and (s.startswith("^\n", i) or s.startswith("^\r\n", i)):
            i += 2 if s[i + 1] == "\n" else 3
            continue
        if c == "(":
            if b.stage.words and _c_runs_child(b.stage.words):
                # cmd only opens a block at the start of a command: after `cmd /c` the parentheses
                # are part of the child's command line (`cmd /c (npm ^& pip install x)`)
                plit, pdepth[0] = b.stage, 0
                continue
            b.end_pipe()
            a = i
            sub, i = _c_list(ctx, i + 1, ")", level + 1)
            inner = s[a + 1 : i - 1].strip()
            if len(inner) > 1 and inner[0] == inner[-1] and inner[0] in "'`":  # for /f … in ('cmd')
                sub, _ = _c_list(_Ctx(inner[1:-1], ctx.state), 0, None, level + 1)
            b.add_group(sub, a, i)
            continue
        if c == ")":
            if closer == ")":
                return b.finish(), i + 1
            b.end_pipe()
            i += 1
            continue
        if c in "<>" or (c in "0123456789" and s[i + 1 : i + 2] in ("<", ">")):
            m = _C_REDIR.match(s, i)
            a, i = i, m.end()
            if "&" not in m.group():
                i = _skip_blanks(s, i)
                if i < n and s[i] not in "\r\n&|()<>":
                    _, i = _c_word(ctx, i)
            b.span(a, i)
            continue
        a = i
        w, i = _c_word(ctx, i)
        if i == a:  # defensive: never loop without progress
            w, i = s[i], i + 1
        if not b.stage.words and (s[a:i].lstrip("@").lower() == "rem" or s[a] == ":"):
            j = s.find("\n", i)  # REM, :: and :labels run to the end of the line, & included
            i = n if j < 0 else j
            continue
        b.add_word(w, a, i)
    if closer is not None and not budget.cut:
        raise _Malformed("unclosed " + closer)
    return b.finish(), i


def _c_runs_child(words: list) -> bool:
    """True when a cmd stage's words so far are `[…] cmd [switches] /c|/k|/r …`."""
    for k, w in enumerate(words):
        w = w.lstrip("@")
        if _is_cmd_prog(w):
            if re.search(r"/[ckr]", _CMD_PROG.match(w).group(2), re.I):
                return True
            return any(x[:1] == "/" and re.search(r"/[ckr]", x, re.I) for x in words[k + 1 :])
    return False


def _c_plit_word(s: str, i: int, pdepth: list) -> bool:
    """In a literal-parentheses stage: does a word (possibly starting with `(` / `)`) start at i?"""
    c = s[i]
    if c in " \t\r\f\v\n&|<>":
        return False
    if c == ")":
        return pdepth[0] > 0 or pdepth[1]  # at depth 0 inside a block, `)` closes the block
    if c == "^":
        return not (s.startswith("^\n", i) or s.startswith("^\r\n", i))
    return not (c in "0123456789" and s[i + 1 : i + 2] in ("<", ">"))


def _c_word(ctx: _Ctx, i: int, pdepth: "list | None" = None) -> tuple[str, int]:
    s, n = ctx.s, ctx.n
    parts: list[str] = []
    while i < n:
        m = _C_PLAIN.match(s, i)
        if m:
            parts.append(m.group())
            i = m.end()
            continue
        c = s[i]
        if pdepth is not None and c == "(":
            pdepth[0] += 1
            parts.append(c)
            i += 1
        elif pdepth is not None and c == ")" and (pdepth[0] > 0 or pdepth[1]):
            pdepth[0] = max(0, pdepth[0] - 1)
            parts.append(c)
            i += 1
        elif c == '"':
            j = s.find('"', i + 1)
            if j < 0 or s.find("\n", i + 1, j) >= 0:
                raise _Malformed("quote")  # cmd would quote to the end of the line; treat as malformed
            parts.append(s[i + 1 : j])
            i = j + 1
        elif c == "^":
            if s.startswith("^\n", i):
                i += 2
            elif s.startswith("^\r\n", i):
                i += 3
            elif i + 1 < n:
                parts.append(s[i + 1])
                i += 2
            else:
                i += 1
        else:
            break
    return "".join(parts), i


# --- malformed fallback --------------------------------------------------------------------------

_CRUDE_PIPES = re.compile(r"&&|\|\||[;&\n]")
_CRUDE_BREAK = {
    "bash": re.compile(r"\$\(|[`(){}]"),
    "powershell": re.compile(r"[$@]?\(|[(){}]"),
    "cmd": re.compile(r"[()]"),
}
_CRUDE_STRIP = {
    "bash": str.maketrans("", "", "'\"\\"),
    "powershell": str.maketrans("", "", "'\"`" + _SQ + _DQ),
    "cmd": str.maketrans("", "", '"^'),
}


def _crude(text: str, shell: str) -> list:
    """Whitespace split on the separators, quotes dropped; every stage is marked."""
    text = _CRUDE_BREAK[shell].sub("\n", text)
    table = _CRUDE_STRIP[shell]
    script = []
    for part in _CRUDE_PIPES.split(text):
        pipe = []
        for seg in part.split("|"):
            words = seg.translate(table).split()
            if words:
                st = _Stage()
                st.words, st.raw, st.bad = words, seg.strip(), True
                pipe.append(st)
        if pipe:
            script.append(pipe)
    return script


def _emit_crude(script: list, shell: str, out: list) -> None:
    """Turn a crude (malformed) parse into Cmds, with bounded pipe_to (linear, not quadratic)."""
    for pipe in script:
        acc = _EMPTY_PIPE
        consumers: list = [None] * len(pipe)
        for k in range(len(pipe) - 1, -1, -1):
            consumers[k] = acc
            acc = _prepend(pipe[k].words[0], acc)
        for k, st in enumerate(pipe):
            to = consumers[k]
            out.append(Cmd(st.words, MALFORMED + st.raw, shell, list(to), _EMPTY_ENV, truncated=to.truncated))


# --- unwrapping ----------------------------------------------------------------------------------

_POSIX_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "ksh93", "mksh", "ash", "fish", "yash",
                           "rbash", "csh", "tcsh", "xonsh", "nu", "elvish"})
_PS_EXES = frozenset({"powershell", "pwsh", "powershell_ise"})
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_BASH_KEYWORDS = frozenset({"!", "if", "then", "elif", "else", "do", "while", "until", "{", "}", "fi", "done", "esac"})
_PS_VAR = re.compile(r"\$[\w:]+")
_PS_ASSIGN_OPS = frozenset({"=", "+=", "-=", "*=", "/=", "%=", "??="})
_PS_ENV_JOINED = re.compile(r"\$env:(\w+)=(.*)", re.I | re.S)
_PS_VAR_JOINED = re.compile(r"\$[\w:]+(?:[+\-*/%]|\?\?)?=(.*)", re.S)
_DASHES = "-\u2013\u2014\u2015"
_WIN_TOKEN = re.compile(r'(?:\^"|\^|[^\s"^]+|"[^"]*"?)+')  # `^"` is a literal quote (cmd), not quoting
_WIN_QUOTE = re.compile(r'(\^")|"')
_CMD_SLASH_C = re.compile(r"/{1,2}[ckr]", re.I)

# name: (options that take a value, positional words before the command, NAME=VALUE allowed)
_SIMPLE: dict[str, tuple[frozenset, int, bool]] = {
    "sudo": (frozenset("-u -g -h -p -C -r -t -T -U -D -R --user --group --host --prompt --close-from --role "
                       "--type --command-timeout --other-user --chdir --chroot".split()), 0, True),
    "doas": (frozenset({"-u", "-C"}), 0, False),
    "pkexec": (frozenset({"--user"}), 0, False),
    "runuser": (frozenset({"-u", "-g", "-G", "--user", "--group", "--supp-group"}), 0, False),
    "time": (frozenset({"-f", "-o", "--format", "--output"}), 0, False),
    "nohup": (frozenset(), 0, False),
    "xargs": (frozenset("-I -L -n -P -s -E -d -a -J -R -S --max-args --max-procs --max-lines --max-chars "
                        "--delimiter --arg-file --eof --replace --process-slot-var".split()), 0, False),
    "exec": (frozenset({"-a"}), 0, False),
    "builtin": (frozenset(), 0, False),
    "nice": (frozenset({"-n", "--adjustment"}), 0, False),
    "timeout": (frozenset({"-s", "-k", "--signal", "--kill-after"}), 1, False),
    "stdbuf": (frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}), 0, False),
    "ionice": (frozenset({"-c", "-n", "-p", "-P", "-u", "--class", "--classdata", "--pid", "--pgid", "--uid"}), 0, False),
    "caffeinate": (frozenset({"-w", "-t"}), 0, False),
    "setsid": (frozenset(), 0, False),
    "busybox": (frozenset(), 0, False),
    "chronic": (frozenset(), 0, False),
    "unbuffer": (frozenset(), 0, False),
    "noglob": (frozenset(), 0, False),
    "nocorrect": (frozenset(), 0, False),
    "coproc": (frozenset(), 0, False),
    "firejail": (frozenset("--profile --net --name --whitelist --blacklist --noblacklist --nowhitelist "
                           "--read-only --read-write --dns --ip --hostname --env --include --join "
                           "--ignore --rlimit-as --timeout".split()), 0, False),
    "chroot": (frozenset({"--userspec", "--groups"}), 1, False),
    "nsenter": (frozenset({"-t", "--target", "-S", "--setuid", "-G", "--setgid"}), 0, False),
    "systemd-run": (frozenset("-u --unit -p --property --slice --description -M --machine --uid --gid "
                              "--setenv -E --working-directory --service-type --nice --on-active "
                              "--on-calendar".split()), 0, False),
}  # fmt: skip

# powershell.exe / pwsh parameters in match order: (full name, shortest accepted prefix, kind)
_PS_EXE_PARAMS = [
    ("executionpolicy", "ex", "val"), ("ep", "ep", "val"),
    ("encodedarguments", "encodeda", "val"), ("ea", "ea", "val"),
    ("encodedcommand", "e", "enc"), ("ec", "ec", "enc"),
    ("commandwithargs", "commandw", "cmd"), ("cwa", "cwa", "cmd"),
    ("command", "c", "cmd"),
    ("configurationfile", "configurationf", "val"), ("configurationname", "config", "val"),
    ("custompipename", "cus", "val"),
    ("file", "f", "file"),
    ("inputformat", "in", "val"), ("if", "if", "val"),
    ("interactive", "i", "sw"),
    ("outputformat", "o", "val"), ("of", "of", "val"),
    ("psconsolefile", "psc", "val"),
    ("settingsfile", "settings", "val"),
    ("version", "v", "val"),
    ("windowstyle", "w", "val"),
    ("workingdirectory", "wo", "val"), ("wd", "wd", "val"),
]  # fmt: skip

_SP_PARAMS = [
    "filepath", "argumentlist", "verb", "workingdirectory", "windowstyle", "redirectstandardoutput",
    "redirectstandarderror", "redirectstandardinput", "credential", "environment", "wait",
    "nonewwindow", "passthru", "loaduserprofile", "usenewenvironment",
]  # fmt: skip
_SP_ALIASES = {
    "path": "filepath", "pspath": "filepath", "lp": "filepath", "args": "argumentlist",
    "rso": "redirectstandardoutput", "rse": "redirectstandarderror", "rsi": "redirectstandardinput",
    "lup": "loaduserprofile", "nnw": "nonewwindow",
}  # fmt: skip
_WRAPPERS = frozenset(_SIMPLE) | _POSIX_SHELLS | _PS_EXES | frozenset(
    "cmd %comspec% start-process saps start iex invoke-expression env call eval script flock parallel "
    "sem sg command find watch su runas wsl".split()
)
_SP_SWITCHES = frozenset({"wait", "nonewwindow", "passthru", "loaduserprofile", "usenewenvironment", "?"})

# Programs that run what is piped or passed to them: every stdin/script reader this parser
# unwraps (all POSIX shells, PowerShell, cmd, iex, parallel, privilege wrappers that start a shell)
# plus common interpreters. Never dropped from pipe_to by the cap (names are _norm'ed).
_EXEC_SINKS = _POSIX_SHELLS | _PS_EXES | frozenset(
    "cmd cmd.exe %comspec% iex invoke-expression parallel sem sudo doas su sg runuser pkexec busybox "
    "wsl eval source . xargs tee python python3 python2 py node nodejs deno bun perl ruby php lua "
    "awk gawk mawk osascript wscript cscript tclsh wish ipython rscript jshell".split()
)
# versioned or family interpreters that also run their input: python3.12.1, pypy3.10, bash-5.2,
# perl5.36, php8.2, lua5.4, nodejs18, zsh-5.9, …
_SINK_RE = re.compile(
    r"(?:python|pypy|ruby|perl|php|lua|luajit|node|nodejs|bash|zsh|ksh|mksh|dash|fish|tcsh|csh|pwsh|tclsh"
    r"|deno|bun)[-_]?\d+(?:\.\d+)*$|pypy$",
    re.I,
)
_MAX_PIPE_TO = 32  # non-sink consumers kept per Cmd (sinks are kept too, up to the total cap)


def _is_sink(name: str) -> bool:
    return name in _EXEC_SINKS or _SINK_RE.match(name) is not None
_CMD_PROG = re.compile(r"(?:.*[\\/])?(cmd(?:\.exe)?|%comspec%)((?:/.*)?)$", re.I | re.S)
# a chain of real cmd switches glued to the program name: /s /q /d /a /u /x /y /c /k /r, and the
# only switches that take a value, /t:fc /e:on /v:on /f:off (a value never contains `/` or blanks)
_CMD_SWITCHES = re.compile(r"(?:/(?:[tevf](?::[^/\s]+)?|[aqudsxyckr]))+$", re.I)
# cmd only: the first word's raw text `cmd/c"…"`, `cmd/s/c^"…`, `cmd/c^ …` (a quote or caret glued to
# the switch chain): cmd.exe ends its program name at `/`, so the rest is the child's command line
_CMD_RAW_GLUED = re.compile(
    r"@*(?:[^\s\"^]*[\\/])?(?:cmd(?:\.exe)?|%comspec%)"
    r"((?:/(?:[tevf](?::[^/\s\"^]+)?|[aqudsxy]))*/[ckr][\"^].*)$",
    re.I | re.S,
)


def _norm(word: str) -> str:
    """Program name for wrapper matching: last path part, lower case, no .exe/.cmd/.bat/.com."""
    w = word.lower()
    if "/" in w or "\\" in w:
        w = w.replace("\\", "/").rsplit("/", 1)[-1]
    if "." in w:
        for ext in (".exe", ".cmd", ".bat", ".com"):
            if w.endswith(ext) and len(w) > len(ext):
                return w[: -len(ext)]
    return w


def _shell_name(shell: str) -> str:
    name = _norm(str(shell or "bash"))
    if name in _PS_EXES or name == "ps":
        return "powershell"
    if name == "cmd":
        return "cmd"
    return "bash"


def _win_split(text: str) -> list[str]:
    return [_WIN_QUOTE.sub(lambda m: m.group(1) or "", t) if '"' in t else t for t in _WIN_TOKEN.findall(text)]


def _join_cmd(parts: list[str]) -> str:
    """argv words back into a Windows command line (as MSYS / PowerShell would pass them)."""
    return " ".join(f'"{p}"' if (p == "" or any(ch.isspace() for ch in p)) and '"' not in p else p for p in parts)


def _is_cmd_prog(word: str) -> bool:
    """True for cmd / cmd.exe / %COMSPEC% (with path), optionally with a glued switch chain.

    A glued tail counts only when it is entirely a chain of real cmd switches (`cmd/q`, `cmd/d/q`,
    `cmd.exe/v:on`, `cmd/s/c`): with /c, /k or /r cmd runs the rest of the line, without one it
    reads commands from stdin. Anything else is a folder named `cmd` (cmd/sh, /tmp/cmd/sudo,
    cmd/c:x/sudo, cmd/kitty), so the normal name-based unwrapping applies. In bash a glued chain
    such as `cmd/q` is also a relative path; the wrapper Cmd is kept as written, so both readings
    are emitted (a chain only names one-letter programs, never a wrapper).
    """
    lw = word.lower()
    if "cmd" not in lw and "comspec" not in lw:
        return False
    m = _CMD_PROG.match(word)
    if m is None:
        return False
    tail = m.group(2)
    return not tail or _CMD_SWITCHES.match(tail) is not None


# any shell, quotes already removed: `cmd.exe/c"pip install x"` from PowerShell is the one word
# `cmd.exe/cpip install x`; a blank inside a program name can only come from quoting, so read the rest
# as cmd's command line too (a conservative extra reading; the word itself is kept as written)
_CMD_WORD_GLUED = re.compile(
    r"@*(?:\S*[\\/])?(?:cmd(?:\.exe)?|%comspec%)((?:/(?:[tevf](?::[^/\s]+)?|[aqudsxy]))*/[ckr]\S*\s.*)$",
    re.I | re.S,
)


def _cmd_word_glued(word: str):
    if not (" " in word or "\t" in word):
        return None
    lw = word.lower()
    return _CMD_WORD_GLUED.match(word) if ("cmd" in lw or "comspec" in lw) else None


def _cmd_raw_glued(raw: str) -> bool:
    lw = raw.lower()
    return ("cmd" in lw or "comspec" in lw) and _CMD_RAW_GLUED.match(raw) is not None


def _unwrap(argv: list[str], extra, shell: str):
    """What `argv` runs, if it is a wrapper.

    None, ("argv", inner_argv, env, extra), ("script", text, shell, env), ("stdin", shell) or ("bad",).
    `extra` is the PowerShell comma joins (Start-Process / the `&` call operator) or, for cmd, the
    list of each argv word's raw source text (quotes kept), for `start` titles and `cmd /c`.
    """
    raws = extra if shell == "cmd" else None
    if shell == "powershell" and argv[0] in ("&", "."):
        return ("argv", argv[1:], {}, {j - 1 for j in extra if j > 1} if extra else None)
    if shell == "bash" and argv[0].startswith("=") and len(argv[0]) > 1:  # zsh =npm equals expansion
        return ("argv", [argv[0][1:]] + argv[1:], {}, None)
    if _is_cmd_prog(argv[0]):  # cmd, cmd.exe, %COMSPEC%, also glued: cmd/c, cmd.exe/s/c
        return _unwrap_cmd(argv, raws)
    if raws is not None and len(raws) == len(argv) and _cmd_raw_glued(raws[0]):  # cmd/c"pip install x"
        m = _CMD_RAW_GLUED.match(raws[0])
        return _cmd_tail(" ".join([m.group(1)] + raws[1:]), True)
    m = _cmd_word_glued(argv[0])
    if m is not None:
        return _cmd_tail(" ".join([m.group(1)] + argv[1:]), shell == "cmd")
    name = _norm(argv[0])
    if name not in _WRAPPERS:
        return None
    spec = _SIMPLE.get(name)
    if spec is not None:
        return _unwrap_simple(argv, spec)
    if name in _POSIX_SHELLS:
        return _unwrap_sh(argv)
    if name in _PS_EXES:
        return _unwrap_ps(argv)
    if name in ("start-process", "saps") or (name == "start" and shell == "powershell"):
        return _unwrap_start_process(argv, extra)
    if name in ("iex", "invoke-expression"):
        args = argv[1:]
        if args and args[0].lower() in ("-command", "-c", "-co", "-com", "-comm", "-comma", "-comman"):
            args = args[1:]
        return ("script", " ".join(args), "powershell", {}) if args else ("stdin", "powershell")
    if name == "env":
        return _unwrap_env(argv)
    if name == "start":
        return _unwrap_cmd_start(argv, raws)
    if name == "call" and shell == "cmd":
        return ("argv", argv[1:], {}, raws[1:] if raws else None) if len(argv) > 1 else None
    if name in ("eval", "script"):
        return _unwrap_eval_script(argv, name)
    if name == "flock":
        return _unwrap_flock(argv)
    if name == "parallel":
        return _unwrap_parallel(argv)
    if name == "sem":
        return _unwrap_parallel(argv, is_sem=True)
    if name == "sg":
        return _unwrap_sg(argv)
    if name == "command":
        rest = argv[1:]
        while rest and rest[0] in ("-p", "--"):
            rest = rest[1:]
        if not rest or rest[0].startswith("-"):  # -v / -V look a name up, they don't run it
            return None
        return ("argv", rest, {}, None)
    if name == "find":
        for k, w in enumerate(argv):
            if w in ("-exec", "-execdir", "-ok", "-okdir"):
                rest = argv[k + 1 :]
                end = next((j for j, x in enumerate(rest) if x in (";", "+")), len(rest))
                return ("argv", rest[:end], {}, None) if end else None
        return None
    if name == "watch":
        return _unwrap_watch(argv)
    if name == "su":
        return _unwrap_su(argv)
    if name == "runas":
        rest = [w for w in argv[1:] if not w.startswith("/")]
        return ("script", rest[0], "cmd", {}) if rest else None
    if name == "wsl":
        return _unwrap_wsl(argv)
    return None


def _unwrap_simple(argv: list[str], spec: tuple[frozenset, int, bool]):
    vflags, npos, allow_assign = spec
    i, n = 1, len(argv)
    env: dict[str, str] = {}
    while i < n:
        w = argv[i]
        if w == "--":
            i += 1
            break
        if w.startswith("--"):
            i += 1 if ("=" in w or w not in vflags) else 2
            continue
        if w.startswith("-") and len(w) > 1:
            if w in vflags:
                i += 2
                continue
            step = 1
            for k in range(1, len(w)):  # clustered short options: -Eu bob, -n1, -I{}
                if "-" + w[k] in vflags:
                    step = 2 if k == len(w) - 1 else 1
                    break
            i += step
            continue
        m = _ASSIGN.match(w) if allow_assign else None
        if m:
            env[w[: m.end() - 1]] = w[m.end() :]
            i += 1
            continue
        break
    rest = argv[i + npos :]
    return ("argv", rest, env, None) if rest else None


def _unwrap_env(argv: list[str]):
    i, n = 1, len(argv)
    split_string = None
    while i < n:
        w = argv[i]
        if w == "--":
            i += 1
            break
        if w in ("-u", "--unset", "-C", "--chdir", "-P"):
            i += 2
            continue
        if w in ("-S", "--split-string"):
            split_string = argv[i + 1] if i + 1 < n else ""
            i += 2
            continue
        if w.startswith("--split-string="):
            split_string = w.split("=", 1)[1]
            i += 1
            continue
        if w.startswith("-S"):
            split_string = w[2:]
            i += 1
            continue
        if w.startswith("-"):
            i += 1
            continue
        break
    env: dict[str, str] = {}
    while i < n:
        m = _ASSIGN.match(argv[i])
        if not m:
            break
        env[argv[i][: m.end() - 1]] = argv[i][m.end() :]
        i += 1
    rest = argv[i:]
    if split_string is not None:
        return ("script", " ".join([split_string] + rest), "bash", env)
    return ("argv", rest, env, None) if rest else None


def _unwrap_eval_script(argv: list[str], name: str):
    if name == "eval":
        return ("script", " ".join(argv[1:]), "bash", {}) if len(argv) > 1 else None
    i, n = 1, len(argv)  # script -c "<cmd>" [logfile]
    while i < n:
        w = argv[i]
        if w in ("-c", "--command"):
            return ("script", argv[i + 1], "bash", {}) if i + 1 < n else None
        if w.startswith("--command="):
            return ("script", w.split("=", 1)[1], "bash", {})
        i += 1
    return None


def _unwrap_flock(argv: list[str]):
    i, n = 1, len(argv)
    while i < n:  # flock's own options, before the lock file
        w = argv[i]
        if w in ("-c", "--command"):
            return ("script", argv[i + 1], "bash", {}) if i + 1 < n else None
        if w.startswith("--command="):
            return ("script", w.split("=", 1)[1], "bash", {})
        if w in ("-w", "--timeout", "-E", "--conflict-exit-code"):
            i += 2
            continue
        if w.startswith("-") and len(w) > 1:
            i += 1
            continue
        break
    j = i + 1  # after the lock file / dir / fd: `-c "<cmd>"` or the command itself
    if j < n and argv[j] in ("-c", "--command"):
        return ("script", argv[j + 1], "bash", {}) if j + 1 < n else None
    if j < n and argv[j].startswith("--command="):
        return ("script", argv[j].split("=", 1)[1], "bash", {})
    rest = argv[j:]
    return ("argv", rest, {}, None) if rest else None


_PARALLEL_SEP = {":::", "::::", ":::+", "::::+"}
# GNU parallel / sem options that take a separate value (long and short forms)
_PARALLEL_VFLAGS = frozenset(
    "-j --jobs -n --max-args -N --max-replace-args -P -S --sshlogin --slf --sshloginfile "
    "--results --res --joblog --jl -a --arg-file --arg-sep --arg-file-sep --colsep -C --delimiter -d "
    "--timeout --retries --delay --halt --halt-on-error --env --tmpdir --wd --workdir --memfree "
    "--load --nice --block -b --recstart --recend --return --transfer --cleanup --basefile "
    "--controlmaster --rpl --parens --pipe-part --semaphore-timeout --semaphore-name --id "
    "--retry-failed --shellquote --termseq --tagstring --ssh --sshdelay --filter-hosts "
    "--sql --sqlmaster --sqlworker --sqlandworker --basefile --bf --block-timeout --bt "
    "--compress-program --decompress-program --ctagstring --filter --group-by --header --limit "
    "--memsuspend --minversion --process-slot-var --profile -J --semaphorename --st --shard "
    "--template --tmpl --transferfile --tf --trc --trim --extensionreplace --er --basenamereplace "
    "--bnr --dirnamereplace --dnr --basenameextensionreplace --bner --seqreplace --slotreplace "
    "--max-chars".split()
)
# GNU parallel short options: those that take a value (separate or glued: `-j 4`, `-j4`, `-I{}`) and
# plain switches; `-e`, `-i`, `-l` take an optional value only when glued, so alone they are switches
_PARALLEL_SHORT_V = frozenset("abCdEIjJLnNPSsU")
_PARALLEL_SHORT_SW = frozenset("0ghkmMopqrtuvVXxT")
# options whose value is optional: `-l1`, `--eof=x` are exact; `-l 1` / `--eof x` are ambiguous
# (the man page says the value must be glued, but a user may not know), so both readings are kept
_PARALLEL_SHORT_OPT = frozenset("eil")
_PARALLEL_LONG_OPT = frozenset({"--eof", "--replace", "--max-lines"})
_PARALLEL_MAX_AMBIGUOUS = 2  # ambiguous options read both ways (2**n readings); later ones are switches


def _parallel_both(argv: list[str], i: int, short: bool, is_sem: bool, amb: int):
    """`argv[i]` ends with an optional-value option followed by a plain word: read it both ways."""
    w, nxt = argv[i], argv[i + 1]
    head = w[:-1] if short else ""
    keep = [head] if head not in ("", "-") else []
    as_switch = argv[:i] + keep + argv[i + 1 :]
    replace = (w[-1] == "i") if short else (w == "--replace")
    as_value = argv[:i] + keep + (["-I", nxt] if replace else []) + argv[i + 2 :]
    texts = []
    stdin = False
    for variant in (as_value, as_switch):
        r = _unwrap_parallel(variant, is_sem, amb + 1)
        if r is not None and r[0] == "script":
            texts.append(r[1])
        elif r is not None and r[0] == "stdin":
            stdin = True
    if texts:
        return ("script", "\n".join(texts), "bash", {}, True)
    return ("stdin", "bash") if stdin else None


def _unwrap_parallel(argv: list[str], is_sem: bool = False, amb: int = 0):
    sep = _PARALLEL_SEP
    i, n = 1, len(argv)
    tmpl: list[str] = []
    repl = "{}"
    started = truncated = False
    while i < n and argv[i] not in sep:
        w = argv[i]
        if not started and w.startswith("-") and len(w) > 1:
            nxt = argv[i + 1] if i + 1 < n else None
            if w in ("--arg-sep", "--arg-file-sep") and nxt is not None:
                sep = sep | {nxt}  # a custom argument separator
                i += 2
                continue
            if w.startswith(("--arg-sep=", "--arg-file-sep=")):
                sep = sep | {w.split("=", 1)[1]}
                i += 1
                continue
            if w.startswith("--replace="):
                repl = w.split("=", 1)[1] or "{}"
                i += 1
                continue
            if w in _PARALLEL_VFLAGS:
                i += 2
                continue
            if w in _PARALLEL_LONG_OPT and nxt is not None and not nxt.startswith("-") and nxt not in sep:
                if amb < _PARALLEL_MAX_AMBIGUOUS:
                    return _parallel_both(argv, i, False, is_sem, amb)
                truncated = True
            if "=" in w or w in _PARALLEL_LONG_OPT:
                i += 1
                continue
            if w.startswith("--"):
                # an unknown long option that may swallow the next word: read it both ways, flagged
                if nxt is not None and not nxt.startswith("-") and nxt not in sep:
                    if amb < _PARALLEL_MAX_AMBIGUOUS:
                        return _parallel_both(argv, i, False, is_sem, amb)
                    truncated = True
                i += 1
                continue
            step = 1
            for k in range(1, len(w)):  # a cluster of short options: -k, -kj4, -j 4, -I{}, -I ,,
                ch = w[k]
                if ch in _PARALLEL_SHORT_V:
                    value = w[k + 1 :]
                    if not value:
                        value, step = (nxt if nxt is not None else ""), 2
                    if ch == "I":
                        repl = value or "{}"
                    break
                if ch in _PARALLEL_SHORT_OPT:
                    if k < len(w) - 1:  # glued value: -l1, -ieof, -i,,
                        if ch == "i":
                            repl = w[k + 1 :]
                        break
                    if nxt is not None and not nxt.startswith("-") and nxt not in sep:
                        if amb < _PARALLEL_MAX_AMBIGUOUS:
                            return _parallel_both(argv, i, True, is_sem, amb)
                        truncated = True
                    break
                if ch not in _PARALLEL_SHORT_SW:
                    # unknown: it may take the next word as its value; read it both ways, flagged
                    if k == len(w) - 1 and nxt is not None and not nxt.startswith("-") and nxt not in sep:
                        if amb < _PARALLEL_MAX_AMBIGUOUS:
                            return _parallel_both(argv, i, True, is_sem, amb)
                        truncated = True
                    break
            i += step
            continue
        started = True
        tmpl.append(w)
        i += 1
    args: list[str] = []
    seps = from_file = False
    while i < n:
        w = argv[i]
        if w in sep:
            seps, from_file = True, w.startswith("::::")
        elif not from_file:
            args.append(w)
        i += 1
    template = " ".join(tmpl)
    if not args:
        if template:
            return ("script", template, "bash", {}, truncated)  # arguments from stdin / a file
        if seps:
            return None
        return ("stdin", "bash")  # no template: parallel runs stdin lines as commands
    if not template:
        # `parallel ::: 'npm i x' 'pip install y'`: each argument is its own command. The text is
        # the arguments themselves (no amplification), so no job or byte cap applies.
        return ("script", "\n".join(args), "bash", {}, truncated)
    jobs: list[str] = []
    total = 0
    capped = len(args) > _MAX_PARALLEL_JOBS
    for a in args[:_MAX_PARALLEL_JOBS]:
        if repl in template:
            job = template.replace(repl, shlex.quote(a))
        else:
            job = template + " " + shlex.quote(a)
        if total + len(job) > _MAX_PARALLEL_BYTES:
            capped = True
            break
        jobs.append(job)
        total += len(job) + 1
    if capped or not jobs:
        # fall back to the template with every argument appended, once, flagged as approximate
        tail = " ".join([template] + [shlex.quote(a) for a in args])
        return ("script", tail[:_MAX_PARALLEL_BYTES], "bash", {}, True)
    return ("script", "\n".join(jobs), "bash", {}, truncated)


def _unwrap_sg(argv: list[str]):
    r = _unwrap_su(argv)  # sg <group> -c "<cmd>"
    if r is not None:
        return r
    rest = argv[1:]
    if rest and rest[0] == "-":
        rest = rest[1:]
    return ("script", " ".join(rest[1:]), "bash", {}) if len(rest) > 1 else None  # sg <group> "<cmd>"


def _unwrap_sh(argv: list[str]):
    i, n = 1, len(argv)
    dash_c = False
    while i < n:
        w = argv[i]
        if w == "--":
            i += 1
            break
        if w in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
            i += 2
            continue
        if w.startswith("--"):
            i += 1
            continue
        if len(w) > 1 and w[0] in "-+":
            if w[0] == "-" and "c" in w[1:]:
                dash_c = True
            i += 2 if w[-1] in "oO" else 1  # -euo pipefail
            continue
        break
    if dash_c:
        return ("script", argv[i], "bash", {}) if i < n else None
    if i >= n or argv[i] == "-":
        return ("stdin", "bash")
    return None  # a script file


def _ps_exe_param(word: str) -> str | None:
    if len(word) < 2 or word[0] not in _DASHES + "/":
        return None
    key = word.lstrip(_DASHES + "/").split(":", 1)[0].lower()
    if not re.fullmatch(r"[a-z?]+", key):
        return None
    for name, shortest, kind in _PS_EXE_PARAMS:
        if key == shortest or (name.startswith(key) and len(key) >= len(shortest)):
            return kind
    return "sw"


def _decode_ps(b64: str) -> str:
    t = re.sub(r"\s+", "", b64)
    t += "=" * (-len(t) % 4)
    return base64.b64decode(t, validate=True).decode("utf-16-le")


def _unwrap_ps(argv: list[str]):
    i, n = 1, len(argv)
    while i < n:
        w = argv[i]
        if w == "-":
            return ("stdin", "powershell")
        kind = _ps_exe_param(w)
        if kind is None:  # positional: Windows PowerShell runs it as -Command
            return ("script", " ".join(argv[i:]), "powershell", {})
        if kind == "cmd":
            rest = argv[i + 1 :]
            if not rest or rest == ["-"]:
                return ("stdin", "powershell")
            return ("script", " ".join(rest), "powershell", {})
        if kind == "enc":
            try:
                return ("script", _decode_ps(argv[i + 1]), "powershell", {})
            except Exception:
                return ("bad",)
        if kind == "file":
            return None
        i += 2 if kind == "val" else 1
    return ("stdin", "powershell")


def _unwrap_cmd(argv: list[str], raws: "list[str] | None" = None):
    """cmd / cmd.exe / %COMSPEC% with switches (glued `cmd/s/c` or spaced) and then /c, /k or /r."""
    m = _CMD_PROG.match(argv[0])
    tail = m.group(2) if m else ""  # switches glued to the program name, e.g. "/s/c"
    outer_cmd = raws is not None and len(raws) == len(argv)
    if outer_cmd:
        rest = " ".join(raws[1:])  # source text, quotes and carets kept
    else:
        rest = _join_cmd(argv[1:])
    return _cmd_tail(tail + " " + rest if rest else tail, outer_cmd)


def _decaret(text: str) -> str:
    """Remove one caret-escape level outside double quotes (cmd strips one `^` per `cmd /c`)."""
    if "^" not in text:
        return text
    out: list = []
    i, n, inq = 0, len(text), False
    while i < n:
        c = text[i]
        if c == '"':
            inq = not inq
            out.append(c)
            i += 1
        elif c == "^" and not inq:
            if i + 1 < n:
                out.append(text[i + 1])
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _outer_quotes(text: str) -> str:
    """cmd's outer-quote rule for `cmd /c "…"`: drop the first and the last quote."""
    if text.startswith('"'):
        last = text.rfind('"')
        return text[1:last] + text[last + 1 :] if last > 0 else text[1:]
    return text


def _cmd_tail(tail: str, outer_cmd: bool = True):
    """What `cmd <tail>` runs. `outer_cmd`: the line was written in cmd, so it still has the outer
    cmd's caret layer (`^&`, `^"`), which that cmd removes before the child applies its outer-quote
    rule. From bash or PowerShell there is no such layer: the child parses the text as it is (`^"`
    is a literal quote that opens no quoting); the caret-stripped text is added as a second,
    conservative reading (two orders) when it differs."""
    pos, n = 0, len(tail)
    while True:
        while pos < n and tail[pos] in " \t":
            pos += 1
        if pos >= n:
            return ("stdin", "cmd")
        if tail[pos] != "/":
            return None  # cmd with an argument but no /c: nothing to run
        if pos + 1 < n and tail[pos + 1] in "cCkKrR":
            text = tail[pos + 2 :].lstrip(" \t")
            stripped = _outer_quotes(_decaret(text))  # caret layer first, then the outer quotes
            if outer_cmd:
                return ("script", stripped, "cmd", {}) if stripped.strip() else None
            text = _outer_quotes(text)
            if not text.strip():
                return ("script", stripped, "cmd", {}) if stripped.strip() else None
            alts = tuple(
                dict.fromkeys(a for a in (stripped, _decaret(text)) if a != text and a.strip())
            )  # conservative extra readings: caret-stripped before / after the outer-quote rule
            return ("script", text, "cmd", {}, False, alts)
        pos += 1  # another switch (/s /q /d /v:on …), glued or spaced
        while pos < n and tail[pos] not in " \t/":
            pos += 1


def _unwrap_cmd_start(argv: list[str], raws: "list[str] | None"):
    i, n = 1, len(argv)
    titled = False
    have_raws = raws is not None and len(raws) == len(argv)
    while i < n:
        w = argv[i]
        lw = w.lower()
        if lw in ("/d", "/affinity", "/node"):
            i += 2
            continue
        if w.startswith("/") and len(w) > 1:
            i += 1
            continue
        # cmd's `start` reads a first argument that begins with `"` as the window title
        if not titled and have_raws and raws[i].startswith('"'):
            rawtok = raws[i]
            q2 = rawtok.find('"', 1)
            after = rawtok[q2 + 1 :] if q2 > 0 else ""
            if after.strip():  # `start "t"npm …`: a program is glued to the title -> reparse it
                tail = " ".join([after] + raws[i + 1 :])
                return ("script", tail, "cmd", {})
            titled = True  # a pure `"title"`
            i += 1
            continue
        if not titled and not have_raws and (w == "" or any(ch.isspace() for ch in w)):
            titled = True
            i += 1
            continue
        break
    rest = argv[i:]
    return ("argv", rest, {}, raws[i:] if have_raws else None) if rest else None


def _sp_param(word: str) -> str | None:
    if len(word) < 2 or word[0] not in _DASHES:
        return None
    key = word[1:].split(":", 1)[0].lower()
    if key in _SP_ALIASES:
        return _SP_ALIASES[key]
    hits = [p for p in _SP_PARAMS if p.startswith(key)]
    return hits[0] if hits else "?"


def _ps_items(text: str) -> list[str]:
    try:
        script, _ = _p_list(_Ctx(text), 0, None, 0)
    except _Malformed:
        return _win_split(text)
    return [w for pipe in script for st in pipe for w in st.words]


def _unwrap_start_process(argv: list[str], joins: set | None):
    groups: list[list[str]] = []
    for k, w in enumerate(argv):
        if joins and k in joins and groups:
            groups[-1].append(w)
        else:
            groups.append([w])
    file_path = None
    arg_list = None
    positional = []
    k = 1
    while k < len(groups):
        g = groups[k]
        param = _sp_param(g[0]) if len(g) == 1 else None
        if param is None:
            positional.append(g)
            k += 1
            continue
        if param in _SP_SWITCHES:
            k += 1
            continue
        if ":" in g[0]:
            value, k = [g[0].split(":", 1)[1]], k + 1
        else:
            value, k = (groups[k + 1] if k + 1 < len(groups) else []), k + 2
        if param == "filepath":
            file_path = value[0] if value else None
        elif param == "argumentlist":
            arg_list = value
    if file_path is None and positional:
        file_path, positional = positional[0][0], positional[1:]
    if arg_list is None and positional:
        arg_list = positional[0]
    if not file_path:
        return None
    items: list[str] = []
    for v in arg_list or []:
        m = re.fullmatch(r"@?\((.*)\)", v, re.S)
        items.extend(_ps_items(m.group(1)) if m else [v])
    return ("argv", [file_path] + _win_split(" ".join(items)), {}, None)


def _unwrap_watch(argv: list[str]):
    i, n = 1, len(argv)
    direct = False
    while i < n:
        w = argv[i]
        if w in ("-x", "--exec"):
            direct = True
            i += 1
        elif w in ("-n", "--interval", "-q", "--equexit"):
            i += 2
        elif w.startswith("-") and len(w) > 1:
            i += 1
        else:
            break
    rest = argv[i:]
    if not rest:
        return None
    return ("argv", rest, {}, None) if direct else ("script", " ".join(rest), "bash", {})


def _unwrap_su(argv: list[str]):
    i, n = 1, len(argv)
    while i < n:
        w = argv[i]
        if w in ("-c", "--command"):
            return ("script", argv[i + 1], "bash", {}) if i + 1 < n else None
        if w.startswith("--command="):
            return ("script", w.split("=", 1)[1], "bash", {})
        i += 2 if w in ("-s", "--shell", "-g", "--group", "-G", "--supp-group", "-w") else 1
    return None


def _unwrap_wsl(argv: list[str]):
    i, n = 1, len(argv)
    while i < n:
        w = argv[i]
        lw = w.lower()
        if lw in ("-e", "--exec", "--"):
            rest = argv[i + 1 :]
            return ("argv", rest, {}, None) if rest else None
        if lw in ("-d", "--distribution", "-u", "--user", "--cd", "--shell-type", "--distribution-id"):
            i += 2
            continue
        if w.startswith("-"):  # wsl --install, --update, … are wsl's own commands
            return None
        break
    rest = argv[i:]
    return ("argv", rest, {}, None) if rest else None


def _cmd_if(argv: list[str]) -> list[str]:
    i, n = 1, len(argv)
    if i < n and argv[i].lower() == "/i":
        i += 1
    if i < n and argv[i].lower() == "not":
        i += 1
    if i < n and argv[i].lower() in ("exist", "defined", "errorlevel", "cmdextversion"):
        i += 2
    elif i < n and "==" in argv[i]:
        i += 2 if argv[i].endswith("==") else 1
    elif i + 1 < n and (argv[i + 1].lower() in ("equ", "neq", "lss", "leq", "gtr", "geq", "==")):
        i += 3
    elif i + 1 < n and argv[i + 1].startswith("=="):
        i += 2
    else:
        i += 1
    return argv[i:]


def _cmd_strip(argv: list[str]) -> list[str]:
    for _ in range(16):
        if not argv:
            break
        w = argv[0]
        if w.startswith("@"):
            w = w.lstrip("@")
            argv = ([w] if w else []) + argv[1:]
            continue
        lw = w.lower()
        if lw in ("do", "else"):
            argv = argv[1:]
        elif lw == "if":
            argv = _cmd_if(argv)
        elif lw == "for":
            return []
        else:
            break
    return argv


# --- emitting Cmds -------------------------------------------------------------------------------


class _PipeTo(list):
    """A bounded, immutable-by-convention consumer list (nearest first) shared between stages.

    `keys` are the _norm'ed names (dedupe key), `others` counts non-sinks, `truncated` records a
    dropped consumer. Prepending a name already present returns the same object, so a flood of one
    program under many spellings (`/p0/tee|/p1/tee|…`) costs O(1) per stage.
    """

    __slots__ = ("keys", "others", "truncated")


def _new_pipe(items: list, keys: list, others: int, truncated: bool) -> _PipeTo:
    p = _PipeTo(items)
    p.keys, p.others, p.truncated = keys, others, truncated
    return p


_EMPTY_PIPE = _new_pipe([], [], 0, False)
_NO_FEED = (None,)


def _prepend(h: str, acc: _PipeTo) -> _PipeTo:
    k = "cmd" if _is_cmd_prog(h) else _norm(h)  # cmd/q, cmd.exe/v:on are cmd (a sink)
    if k in acc.keys:
        return acc
    items = [h] + acc
    keys = [k] + acc.keys
    others = acc.others + (0 if _is_sink(k) else 1)
    truncated = acc.truncated
    if others > _MAX_PIPE_TO:  # drop the farthest non-sink (keep the nearest consumers)
        for j in range(len(keys) - 1, 0, -1):
            if not _is_sink(keys[j]):
                del items[j], keys[j]
                others -= 1
                truncated = True
                break
    if len(items) > _MAX_PIPE_TOTAL:  # hard total cap: drop the farthest entry (a sink by now)
        if not _is_sink(keys[-1]):
            others -= 1
        del items[-1], keys[-1]
        truncated = True
    return _new_pipe(items, keys, others, truncated)


def _as_pipe(names) -> _PipeTo:
    if isinstance(names, _PipeTo):
        return names
    acc = _EMPTY_PIPE
    for h in reversed(names):
        acc = _prepend(h, acc)
    return acc


def _bound_consumers(heads: list, acc) -> _PipeTo:
    """`heads` (nearest first) in front of the consumer list `acc`, deduped and capped."""
    acc = _as_pipe(acc)
    for h in reversed(heads):
        acc = _prepend(h, acc)
    return acc


_WIN_ABS = re.compile(r"[A-Za-z]:[\\/]|\\\\")
_CD_NAMES = {
    "bash": ("cd", "pushd", "popd"),
    "powershell": ("cd", "sl", "set-location", "chdir", "pushd", "push-location", "popd", "pop-location"),
    "cmd": ("cd", "chdir", "pushd", "popd"),
}
_POPD = frozenset({"popd", "pop-location"})


_CD_UNKNOWN = object()  # a cd whose destination we cannot resolve (home, `cd -`): cwd becomes None


def _cd_target(argv: list, shell: str):
    """A cd's destination, _CD_UNKNOWN for `cd`/`cd -`/home/`popd`/a variable, or None if not a cd."""
    if not argv:
        return None
    name = _norm(argv[0])
    if name not in _CD_NAMES[shell]:
        return None
    if name in _POPD:
        return _CD_UNKNOWN  # back to a directory pushed earlier (maybe before this input)
    rest = argv[1:]
    if shell == "cmd":
        rest = [a for a in rest if a.lower() != "/d"]
    elif shell == "bash":
        rest = [a for a in rest if not (a.startswith("-") and len(a) > 1)]
    rest = [a for a in rest if a and not (shell == "powershell" and a.startswith("-"))]
    if not rest or rest[0] == "-":
        return _CD_UNKNOWN  # `cd` alone (home) or `cd -` (previous dir): destination unknown
    if any(ch in rest[0] for ch in "$`%"):
        return _CD_UNKNOWN  # `cd $OLDPWD`, `cd %X%`: only known at run time
    return rest[0]


def _apply_cd(cwd, target: str, shell: str):
    if target.startswith("/") or target.startswith("~") or _WIN_ABS.match(target):
        return target  # absolute: replaces the current directory
    if cwd is None:
        return target  # relative to the (unknown) start: keep it as written, best effort
    sep = "\\" if shell == "cmd" and "\\" in cwd else "/"
    return cwd.rstrip("/\\") + sep + target


def _parse(text, shell, depth, env, pipe_to, out, state, cwd, trunc=False) -> None:
    """Parse and emit one script level; `env` is the (frozen) environment it starts with."""
    if state.stop:
        return
    try:
        ctx = _Ctx(text, state)
        if shell == "powershell":
            script, _ = _p_list(ctx, 0, None, 0)
        elif shell == "cmd":
            script, _ = _c_list(ctx, 0, None, 0)
        else:
            script, _ = _b_list(ctx, 0, None, 0)
    except Exception:
        script = _crude(text, shell)
    _emit(script, shell, depth, _EnvBox(env), pipe_to, out, state, cwd, trunc)


def _reparse(text, shell, depth, env, pipe_to, out, state, cwd, trunc=False) -> None:
    if depth + 1 > MAX_DEPTH:
        words = text.translate(_CRUDE_STRIP[shell]).split()
        if words:
            to = _as_pipe(pipe_to)
            out.append(Cmd(words, DEPTH_LIMIT + text.strip(), shell, list(to), env, cwd, to.truncated or trunc))
        return
    _parse(text, shell, depth + 1, env, pipe_to, out, state, cwd, trunc)


_BASH_SPECIAL_HEADS = _BASH_KEYWORDS | {"export", "declare", "typeset"}
_CMD_SPECIAL_HEADS = frozenset({"do", "else", "if", "for", "set"})


def _analyze(st: _Stage, shell: str, state: "_State | None" = None):
    """(steps, tail, heads, persist) for one stage: the unwrap chain and env it sets for later."""
    words = st.words
    if words and st.group is None:  # fast path: an ordinary command, nothing to strip or unwrap
        w0 = words[0]
        if shell == "bash":
            plain = "=" not in w0 and w0 not in _BASH_SPECIAL_HEADS
        elif shell == "powershell":
            plain = w0[:1] != "$" and w0 != "&" and w0 != "."
        else:
            plain = w0[:1] != "@" and w0.lower() not in _CMD_SPECIAL_HEADS
            if plain and st.raws and _cmd_raw_glued(st.raws[0]):
                plain = False
        if plain and _norm(w0) not in _WRAPPERS and not _is_cmd_prog(w0) and not _cmd_word_glued(w0):
            return [(words, {})], None, [w0], None
    argv = list(words)
    assigns: dict[str, str] = {}
    persist = None
    if shell == "bash":
        while argv:
            w = argv[0]
            m = _ASSIGN.match(w)
            if w in _BASH_KEYWORDS:
                argv.pop(0)
            elif m:
                assigns[w[: m.end() - 1]] = w[m.end() :]
                argv.pop(0)
            else:
                break
        if not argv and assigns:
            persist = assigns
        elif argv and argv[0] in ("export", "declare", "typeset"):
            persist = {}
            for w in argv[1:]:
                m = _ASSIGN.match(w)
                if m:
                    persist[w[: m.end() - 1]] = w[m.end() :]
    elif shell == "powershell" and argv:
        w0 = argv[0]
        if _PS_VAR.fullmatch(w0) and len(argv) > 1 and argv[1] in _PS_ASSIGN_OPS:
            if w0.lower().startswith("$env:") and len(argv) <= 3:
                persist = {w0[5:]: argv[2] if len(argv) > 2 else ""}
                argv = []
            else:
                argv = argv[2:]  # $x = <pipeline> runs the pipeline
        else:
            m = _PS_ENV_JOINED.fullmatch(w0)
            if m and len(argv) == 1:
                persist = {m.group(1): m.group(2)}
                argv = []
            else:
                m = _PS_VAR_JOINED.fullmatch(w0)
                if m:
                    argv = ([m.group(1)] if m.group(1) else []) + argv[1:]
    elif shell == "cmd":
        argv = _cmd_strip(argv)
        if len(argv) > 1 and argv[0].lower() == "set" and not argv[1].startswith("/"):
            text = " ".join(argv[1:])
            if "=" in text:
                k, v = text.split("=", 1)
                persist = {k.strip(): v.strip()}

    # `extra` carries PowerShell comma joins (Start-Process / the call operator) or, for cmd, each
    # argv word's raw source text (quotes kept) for `start "title" prog` and `cmd /c "…"`.
    # Stripping may drop leading words (if/do/else) or an `@` from the first word, so align on the
    # words after the first.
    extra = None
    off = len(st.words) - len(argv)
    aligned = argv and off >= 0 and st.words[off + 1 :] == argv[1:]
    if aligned and shell == "powershell" and st.joins:
        extra = {j - off for j in st.joins if j > off}
    elif aligned and shell == "cmd" and st.raws is not None and len(st.raws) == len(st.words):
        extra = st.raws[off:]
    steps = []
    tail = None
    for _ in range(_MAX_UNWRAP):
        if not argv:
            break
        steps.append((argv, assigns))
        r = _unwrap(argv, extra, shell)
        if r is None:
            break
        if r[0] != "argv":
            tail = r
            break
        argv, extra = r[1], r[3]
        if state is not None:  # each step is a new argv (and a Cmd): its words are work
            state.work(len(argv))
            if state.stop:
                break
        if r[2]:
            assigns = {**assigns, **r[2]}
    heads = [a[0] for a, _ in steps]
    if st.group is not None:
        for pipe in st.group:
            heads.extend(_analyze(pipe[0], shell, state)[2])
    return steps, tail, heads, persist


def _echo_text(st: _Stage, steps: list) -> str | None:
    """Text a stage writes to stdout, when it is a literal (echo, printf, Write-Output, 'string')."""
    if st.literal and len(st.words) == 1:
        return st.words[0]
    if not steps:
        return None
    argv = steps[-1][0]
    name = _norm(argv[0])
    args = argv[1:]
    if name == "echo":
        while args and re.fullmatch(r"-[neE]+", args[0]):
            args = args[1:]
    elif name not in ("printf", "write-output", "write"):
        return None
    return " ".join(args).replace("\\n", "\n").replace("\\t", "\t")


def _set_env(box: _EnvBox, persist: dict, state: _State) -> None:
    """`export A=1`, `A=1`, `$env:A=…`, `set A=1`: a new env state for the commands after it."""
    cur = box.cur
    new = None  # copied on the first real change only
    for k, v in persist.items():
        env = cur if new is None else new
        if k not in env and len(env) >= _MAX_ENV:
            state.overflow = True
        elif env.get(k, _NOTHING) != v:
            if new is None:
                new = dict(cur)
            new[k] = v
    if new is not None:  # copy on write: Cmds already emitted keep the old state
        box.cur = _FrozenEnv(new)
        state.work(len(new))


_NOTHING = object()


def _merge_env(state: _State, base: _FrozenEnv, extra: dict) -> _FrozenEnv:
    """`base` with `extra` assignments on top, interned: equal states share one mapping."""
    if not extra:
        return base
    hit = state.env_keys.get(id(extra))
    if hit is None or hit[0] is not extra:
        hit = (extra, tuple(sorted(extra.items())))
        state.env_keys[id(extra)] = hit
        state.work(len(extra))
    mk = (id(base), hit[1])
    got = state.envs.get(mk)
    if got is not None and got[0] is base:
        return got[1]
    merged = _FrozenEnv({**base, **extra})
    state.envs[mk] = (base, merged)  # keeps `base` alive, so its id stays unique
    state.work(len(merged))
    return merged


def _emit(script, shell, depth, box, inherit, out, state, cwd=None, trunc=False) -> None:
    prev_op = None
    for pipeline in script:
        if state.late():
            return
        infos = [_analyze(st, shell, state) for st in pipeline]
        last = len(pipeline) - 1
        base = inherit if inherit.__class__ is _PipeTo else _as_pipe(inherit)
        if last:
            later = [base] * (last + 1)  # pipe_to of each stage (bounded, nearest consumer first)
            later_feed: list = [None] * (last + 1)  # nearest later stage running its stdin as a script
            acc, feed = base, None
            for k in range(last, -1, -1):
                later[k], later_feed[k] = acc, feed
                info = infos[k]
                for h in reversed(info[2]):
                    acc = _prepend(h, acc)
                if info[1] and info[1][0] == "stdin":
                    feed = info[1][1]
        else:
            later, later_feed = (base,), _NO_FEED
        for k, st in enumerate(pipeline):
            if k and not k & 255 and state.late():  # long pipelines check the clock as they go
                return
            steps, tail, heads, persist = infos[k]
            pipe_to = later[k]
            if st.group is not None:
                _emit(st.group, shell, depth, box, pipe_to, out, state, cwd, trunc)
            marked = st.bad or state.overflow
            for idx, (argv, assigns) in enumerate(steps):
                raw = st.raw
                if marked or (tail and tail[0] == "bad" and idx == len(steps) - 1):
                    raw = MALFORMED + raw
                env_ = _merge_env(state, box.cur, assigns) if assigns else box.cur
                out.append(Cmd(argv, raw, shell, list(pipe_to), env_, cwd, pipe_to.truncated or trunc))
            sub_inherit = _bound_consumers(heads, pipe_to) if st.subs else None
            for sub in st.subs:  # substitutions run before the command and feed it
                _emit(sub, shell, depth, box, sub_inherit, out, state, cwd, trunc)
            if tail is not None or later_feed[k]:
                inner_env = _merge_env(state, box.cur, steps[-1][1]) if steps else box.cur
                if tail and tail[0] == "script":
                    t_trunc = trunc or (len(tail) > 4 and tail[4])
                    for text in (tail[1],) + (tail[5] if len(tail) > 5 else ()):
                        _reparse(text, tail[2], depth, _merge_env(state, inner_env, tail[3]), pipe_to, out, state, cwd, t_trunc)
                target = tail[1] if tail and tail[0] == "stdin" else None
                feeds = target or later_feed[k]
                if feeds:
                    texts = list(st.stdin)
                    if not target:
                        echoed = _echo_text(st, steps)
                        if echoed:
                            texts.append(echoed)
                    for text in texts:
                        _reparse(text, feeds, depth, inner_env, pipe_to, out, state, cwd, trunc)
            if persist:
                _set_env(box, persist, state)
        op = pipeline[-1].op
        ran_after, prev_op = prev_op, op  # the operator before this pipeline (`a || cd /b`)
        if last == 0 and infos[0][0]:  # a lone `cd`/`pushd`/… moves the cwd for later commands
            target = _cd_target(infos[0][0][-1][0], shell)
            if target is None or (op == "&" and shell != "cmd"):
                pass  # not a cd, or a background job (its own process): the cwd is unchanged
            elif target is _CD_UNKNOWN or op == "||" or ran_after == "||":
                cwd = None  # unknown, or a conditional cd (`cd /b || true`): it may not have run
            else:
                cwd = _apply_cd(cwd, target, shell)


def _dedup(cmds: list[Cmd]) -> list[Cmd]:
    """One Cmd per (argv, shell, raw, env state, cwd); re-parses of the same text merge their pipe_to.

    Env states are interned mappings (see _merge_env), so the key uses the mapping's identity
    instead of sorting its items for every Cmd.
    """
    seen: dict = {}
    out = []
    for c in cmds:
        key = (tuple(c.argv), c.shell, c.raw, id(c.env), c.cwd)
        prev = seen.get(key)
        if prev is None:
            seen[key] = c
            out.append(c)
            continue
        prev.truncated = prev.truncated or c.truncated
        for p in c.pipe_to:
            if p not in prev.pipe_to:
                if len(prev.pipe_to) < _MAX_PIPE_TOTAL:
                    prev.pipe_to.append(p)
                else:
                    prev.truncated = True
    return out
