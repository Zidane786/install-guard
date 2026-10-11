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
                 `$env:A = '1'`, `set A=1` in the same line. Read only.

Splitting: `&&`, `||`, `;`, `|`, `&`, newlines, `$( … )`, backticks, `( … )` and `{ …; }` groups,
`<( … )`; PowerShell `;`, `|`, `&&`, `||`, `$( … )`, `( … )`, `{ … }` script blocks; cmd `&`,
`&&`, `||`, `|`, `( … )`. cmd does not split on `;`. Comments (`#`, `<# #>`, `REM`, `::`) and
heredoc / here-string bodies are dropped, except a body fed to a shell (`bash <<EOF`,
`cat <<EOF | sh`, `bash <<< '…'`, `echo '…' | bash`, `'…' | iex`), which is parsed as a script,
and `$( … )` inside an unquoted heredoc, which runs.

Unwrapping: sudo, doas, env, time, nohup, xargs, exec, command, nice, timeout, stdbuf, ionice,
caffeinate, setsid, busybox, runuser, pkexec, find -exec, watch, su -c, eval, bash/sh/zsh/… -c,
cmd /c, call, start, runas, wsl, powershell/pwsh -Command / -EncodedCommand (base64 UTF-16LE,
decoded) / positional command, Start-Process (-FilePath, -ArgumentList), Invoke-Expression/iex,
and the PowerShell call operators `&` and `.`. The wrapper's Cmd is kept and each unwrapped
command follows it as its own Cmd (order: source order, a wrapper before what it runs). Wrappers
that re-parse a string (-c, /c, -Command, iex, eval, stdin scripts) nest at most MAX_DEPTH deep;
text beyond that is returned whitespace-split with `raw` starting with DEPTH_LIMIT.

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
from dataclasses import dataclass, field

MAX_DEPTH = 5
MALFORMED = "[malformed] "
DEPTH_LIMIT = "[depth-limit] "

_MAX_NEST = 100  # syntactic nesting ($( … ) inside $( … ) …) before a script counts as malformed
_MAX_ENV = 64  # tracked environment variables before the line counts as malformed
_MAX_UNWRAP = 64  # argv-level unwraps of one command (sudo sudo sudo …)


@dataclass
class Cmd:
    argv: list[str]
    raw: str
    shell: str
    pipe_to: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


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
        try:
            _parse(line, sh, 0, {}, [], out, _State())
        except Exception:  # last resort: never raise into a hook
            out = []
            for pipe in _crude(line, sh):
                names = [st.words[0] for st in pipe]
                for k, st in enumerate(pipe):
                    out.append(Cmd(st.words, MALFORMED + st.raw, sh, names[k + 1 :], {}))
        return _dedup(out)
    finally:
        if collecting:
            gc.enable()


# --- shared structures ---------------------------------------------------------------------------


class _Malformed(Exception):
    pass


class _State:
    __slots__ = ("overflow",)

    def __init__(self) -> None:
        self.overflow = False  # more env variables than _MAX_ENV: later Cmds are marked


class _Ctx:
    __slots__ = ("s", "n", "heredocs")

    def __init__(self, s: str) -> None:
        self.s = s
        self.n = len(s)
        self.heredocs: list = []


class _Stage:
    """One simple command (or group) while tokenizing."""

    __slots__ = ("words", "joins", "start", "end", "subs", "stdin", "group", "literal", "bad", "raw")

    def __init__(self) -> None:
        self.words: list[str] = []
        self.joins: set[int] | None = None  # PowerShell: indexes of words joined to the one before by `,`
        self.start = -1
        self.end = -1
        self.subs: list = []  # nested scripts: $( … ), backticks, <( … ), PS ( … ) and { … }
        self.stdin: list[str] = []  # heredoc and here-string bodies
        self.group = None  # ( … ) / { …; } group: a script
        self.literal = False  # PowerShell: a word was one quoted string (a lone one is a string literal)
        self.bad = False
        self.raw = ""

    def empty(self) -> bool:
        return not (self.words or self.subs or self.stdin or self.group is not None)


class _Builder:
    __slots__ = ("s", "script", "pipe", "stage", "comma")

    def __init__(self, s: str) -> None:
        self.s = s
        self.script: list = []
        self.pipe: list = []
        self.stage = _Stage()
        self.comma = False

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
        self.stage = _Stage()
        self.comma = False

    def end_pipe(self) -> None:
        self.end_stage()
        if self.pipe:
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


def _subs_in_text(text: str, subs: list, level: int, shell: str) -> None:
    """Command substitutions inside an expandable heredoc / here-string body."""
    pat = _B_SUBST if shell == "bash" else _P_SUBST
    if not pat.search(text):
        return
    ctx = _Ctx(text)
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
    b = _Builder(s)
    while i < n:
        m = _B_TOK.match(s, i)
        if m:
            g = m.lastindex
            if g == 4:
                op = m.group(4)
                if op[0] == "|" and op != "||":
                    b.end_stage()
                else:
                    b.end_pipe()
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
    if closer is not None:
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
            _subs_in_text(body, stage.subs, level, "bash")
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
                    _subs_in_text(s[i + 3 : m.end() - 2], subs, level, "bash")
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
                _subs_in_text(s[i + 2 : m.end() - 1], subs, level, "bash")  # ${x:-$(cmd)} runs cmd
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
    sub, _ = _b_list(_Ctx("".join(out)), 0, None, level + 1)
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
    b = _Builder(s)
    while i < n:
        m = _P_TOK.match(s, i)
        if m and m.group(1) != "--%":
            g = m.lastindex
            if g == 4:
                if m.group(4) == "|":
                    b.end_stage()
                else:
                    b.end_pipe()
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
                b.end_pipe()
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
                        _, i, _ = _p_word(ctx, i, b.stage.subs, level)
                b.span(a, i)
                continue
        a = i
        w, i, lit = _p_word(ctx, i, b.stage.subs, level)
        if i == a:  # defensive: never loop without progress
            w, i, lit = s[i], i + 1, False
        if w == "--%" and s[a:i] == "--%":  # stop-parsing token: the rest of the line is literal
            j = s.find("\n", i)
            j = n if j < 0 else j
            for t in _win_split(s[i:j]):
                b.add_word(t, i, j)
            i = j
            continue
        b.add_word(w, a, i, lit)
    if closer is not None:
        raise _Malformed("unclosed " + closer)
    return b.finish(), i


def _p_word(ctx: _Ctx, i: int, subs: list, level: int) -> tuple[str, int, bool]:
    """One PowerShell argument; the flag says it was exactly one quoted string."""
    s, n = ctx.s, ctx.n
    parts: list[str] = []
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
                subs.append(sub)
                v = s[a:i]
            elif nx and nx in _SQ + _DQ and _P_HERE_START.match(s, i + 2):
                v, i = _p_here(ctx, i, subs, level)
                quoted += 1
            else:
                v, i = "@", i + 1
        elif c in "({":
            a = i
            sub, i = _p_list(ctx, i + 1, ")" if c == "(" else "}", level + 1)
            subs.append(sub)
            v = s[a:i]
        else:
            break
        parts.append(v)
        pieces += 1
    return "".join(parts), i, pieces == 1 and quoted == 1


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
        _subs_in_text(body, subs, level, "powershell")
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
    b = _Builder(s)
    while i < n:
        m = _C_TOK.match(s, i)
        # a first word that may be REM, a :label or start with @ goes the slow way
        if m and (m.lastindex != 1 or b.stage.words or (m.group(1)[0] not in ":@" and m.group(1).lower() != "rem")):
            g = m.lastindex
            if g == 3:
                if m.group(3) == "|":
                    b.end_stage()
                else:
                    b.end_pipe()
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
            b.end_pipe()
            a = i
            sub, i = _c_list(ctx, i + 1, ")", level + 1)
            inner = s[a + 1 : i - 1].strip()
            if len(inner) > 1 and inner[0] == inner[-1] and inner[0] in "'`":  # for /f … in ('cmd')
                sub, _ = _c_list(_Ctx(inner[1:-1]), 0, None, level + 1)
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
    if closer is not None:
        raise _Malformed("unclosed " + closer)
    return b.finish(), i


def _c_word(ctx: _Ctx, i: int) -> tuple[str, int]:
    s, n = ctx.s, ctx.n
    parts: list[str] = []
    while i < n:
        m = _C_PLAIN.match(s, i)
        if m:
            parts.append(m.group())
            i = m.end()
            continue
        c = s[i]
        if c == '"':
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


# --- unwrapping ----------------------------------------------------------------------------------

_POSIX_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "mksh", "ash", "fish", "yash", "rbash"})
_PS_EXES = frozenset({"powershell", "pwsh", "powershell_ise"})
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_BASH_KEYWORDS = frozenset({"!", "if", "then", "elif", "else", "do", "while", "until", "{", "}", "fi", "done", "esac"})
_PS_VAR = re.compile(r"\$[\w:]+")
_PS_ASSIGN_OPS = frozenset({"=", "+=", "-=", "*=", "/=", "%=", "??="})
_PS_ENV_JOINED = re.compile(r"\$env:(\w+)=(.*)", re.I | re.S)
_PS_VAR_JOINED = re.compile(r"\$[\w:]+(?:[+\-*/%]|\?\?)?=(.*)", re.S)
_DASHES = "-\u2013\u2014\u2015"
_WIN_TOKEN = re.compile(r'(?:[^\s"]+|"[^"]*"?)+')
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
    "cmd start-process saps start iex invoke-expression env call eval command find watch su runas wsl".split()
)
_SP_SWITCHES = frozenset({"wait", "nonewwindow", "passthru", "loaduserprofile", "usenewenvironment", "?"})


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
    return [t.replace('"', "") for t in _WIN_TOKEN.findall(text)]


def _join_cmd(parts: list[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    return " ".join(f'"{p}"' if (p == "" or any(ch.isspace() for ch in p)) and '"' not in p else p for p in parts)


def _unwrap(argv: list[str], joins: set | None, shell: str):
    """What `argv` runs, if it is a wrapper.

    None, ("argv", inner_argv, env, joins), ("script", text, shell, env), ("stdin", shell) or ("bad",).
    `joins` are PowerShell comma joins (argv indexes), needed for Start-Process -ArgumentList a,b.
    """
    if shell == "powershell" and argv[0] in ("&", "."):
        return ("argv", argv[1:], {}, {j - 1 for j in joins if j > 1} if joins else None)
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
    if name == "cmd":
        return _unwrap_cmd(argv)
    if name in ("start-process", "saps") or (name == "start" and shell == "powershell"):
        return _unwrap_start_process(argv, joins)
    if name in ("iex", "invoke-expression"):
        args = argv[1:]
        if args and args[0].lower() in ("-command", "-c", "-co", "-com", "-comm", "-comma", "-comman"):
            args = args[1:]
        return ("script", " ".join(args), "powershell", {}) if args else ("stdin", "powershell")
    if name == "env":
        return _unwrap_env(argv)
    if name == "start":
        return _unwrap_cmd_start(argv)
    if name == "call" and shell == "cmd":
        return ("argv", argv[1:], {}, None) if len(argv) > 1 else None
    if name == "eval":
        return ("script", " ".join(argv[1:]), "bash", {}) if len(argv) > 1 else None
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


def _unwrap_cmd(argv: list[str]):
    i, n = 1, len(argv)
    while i < n:
        w = argv[i]
        m = _CMD_SLASH_C.match(w)
        if m:
            first = w[m.end() :]
            parts = ([first] if first else []) + argv[i + 1 :]
            return ("script", _join_cmd(parts), "cmd", {}) if parts else None
        if w.startswith("/"):
            i += 1
            continue
        return None
    return ("stdin", "cmd")


def _unwrap_cmd_start(argv: list[str]):
    i, n = 1, len(argv)
    titled = False
    while i < n:
        w = argv[i]
        lw = w.lower()
        if lw in ("/d", "/affinity", "/node"):
            i += 2
            continue
        if w.startswith("/") and len(w) > 1:
            i += 1
            continue
        if not titled and (w == "" or any(ch.isspace() for ch in w)):  # "title"
            titled = True
            i += 1
            continue
        break
    rest = argv[i:]
    return ("argv", rest, {}, None) if rest else None


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


def _parse(text: str, shell: str, depth: int, env: dict, pipe_to: list, out: list, state: _State) -> None:
    try:
        ctx = _Ctx(text)
        if shell == "powershell":
            script, _ = _p_list(ctx, 0, None, 0)
        elif shell == "cmd":
            script, _ = _c_list(ctx, 0, None, 0)
        else:
            script, _ = _b_list(ctx, 0, None, 0)
    except Exception:
        script = _crude(text, shell)
    _emit(script, shell, depth, env, pipe_to, out, state)


def _reparse(text: str, shell: str, depth: int, env: dict, pipe_to: list, out: list, state: _State) -> None:
    if depth + 1 > MAX_DEPTH:
        words = text.translate(_CRUDE_STRIP[shell]).split()
        if words:
            out.append(Cmd(words, DEPTH_LIMIT + text.strip(), shell, list(pipe_to), dict(env)))
        return
    _parse(text, shell, depth + 1, env, pipe_to, out, state)


_BASH_SPECIAL_HEADS = _BASH_KEYWORDS | {"export", "declare", "typeset"}
_CMD_SPECIAL_HEADS = frozenset({"do", "else", "if", "for", "set"})


def _analyze(st: _Stage, shell: str):
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
        if plain and _norm(w0) not in _WRAPPERS:
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

    joins = None
    if st.joins:
        off = len(st.words) - len(argv)
        if off >= 0 and st.words[off:] == argv:
            joins = {j - off for j in st.joins if j > off}
    steps = []
    tail = None
    for _ in range(_MAX_UNWRAP):
        if not argv:
            break
        steps.append((argv, assigns))
        r = _unwrap(argv, joins, shell)
        if r is None:
            break
        if r[0] != "argv":
            tail = r
            break
        argv, joins = r[1], r[3]
        if r[2]:
            assigns = {**assigns, **r[2]}
    heads = [a[0] for a, _ in steps]
    if st.group is not None:
        for pipe in st.group:
            heads.extend(_analyze(pipe[0], shell)[2])
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


def _set_env(env: dict, persist: dict, state: _State) -> None:
    for k, v in persist.items():
        if k in env or len(env) < _MAX_ENV:
            env[k] = v
        else:
            state.overflow = True


def _emit(script: list, shell: str, depth: int, env: dict, inherit: list, out: list, state: _State) -> None:
    for pipeline in script:
        infos = [_analyze(st, shell) for st in pipeline]
        last = len(pipeline) - 1
        later = [inherit] * (last + 1)  # pipe_to of each stage
        later_feed: list = [None] * (last + 1)  # nearest later stage that runs its stdin as a script
        if last:
            acc, feed = inherit, None
            for k in range(last, -1, -1):
                later[k], later_feed[k] = acc, feed
                acc = infos[k][2] + acc
                if infos[k][1] and infos[k][1][0] == "stdin":
                    feed = infos[k][1][1]
        for k, st in enumerate(pipeline):
            steps, tail, heads, persist = infos[k]
            pipe_to = later[k]
            if st.group is not None:
                _emit(st.group, shell, depth, env, pipe_to, out, state)
            marked = st.bad or state.overflow
            for idx, (argv, assigns) in enumerate(steps):
                raw = st.raw
                if marked or (tail and tail[0] == "bad" and idx == len(steps) - 1):
                    raw = MALFORMED + raw
                out.append(Cmd(argv, raw, shell, list(pipe_to), {**env, **assigns} if assigns else dict(env)))
            for sub in st.subs:  # substitutions run before the command and feed it
                _emit(sub, shell, depth, env, heads + pipe_to, out, state)
            if tail is None and not later_feed[k]:
                if persist:
                    _set_env(env, persist, state)
                continue
            inner_env = {**env, **steps[-1][1]} if steps else env
            if tail and tail[0] == "script":
                _reparse(tail[1], tail[2], depth, {**inner_env, **tail[3]}, pipe_to, out, state)
            target = tail[1] if tail and tail[0] == "stdin" else None
            feeds = target or later_feed[k]
            if feeds:
                texts = list(st.stdin)
                if not target:
                    echoed = _echo_text(st, steps)
                    if echoed:
                        texts.append(echoed)
                for text in texts:
                    _reparse(text, feeds, depth, dict(inner_env), pipe_to, out, state)
            if persist:
                _set_env(env, persist, state)


def _dedup(cmds: list[Cmd]) -> list[Cmd]:
    """One Cmd per (argv, shell, raw); re-parses of the same text merge their pipe_to and env."""
    seen: dict = {}
    out = []
    for c in cmds:
        key = (tuple(c.argv), c.shell, c.raw)
        prev = seen.get(key)
        if prev is None:
            seen[key] = c
            out.append(c)
            continue
        for p in c.pipe_to:
            if p not in prev.pipe_to:
                prev.pipe_to.append(p)
        for k, v in c.env.items():
            prev.env.setdefault(k, v)
    return out
