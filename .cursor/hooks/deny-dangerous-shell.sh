#!/bin/sh
# Cursor beforeShellExecution hook: fail-closed deny list (v2.4).
#
# hooks.json only routes a command here when its "matcher" regex sees:
#   - launchctl, sudo, reboot, shutdown, halt, pmset, launchagents or
#     launchdaemons (any case), also with \ '' "" ${...} $@ $* between letters
#   - a script run in command position: ./x, ../x, dir/x, ~/x, $HOME/x,
#     anything ending .sh/.bash/.zsh/.command, sh/bash/zsh/dash/ksh, source, .
#     (a quoted path such as "./dir with space/x.sh" counts too)
#   - the named launchd scripts (deploy/macos/install.sh, *-tv-helper.sh)
#   - a GIT_*, LESS*, PAGER, MANPAGER, EDITOR, VISUAL or SSH_ASKPASS= or +=
#     assignment, also after a quote, backtick or backslash (these can make a
#     read-only tool run a program), which then gets at least ask
#   - git (also \git, g''it, /path/git) with -c, --config-env, --upload-pack,
#     --receive-pack or --exec anywhere in its arguments, also across a
#     backslash-newline continuation
#   - eval
# Every other command (ls, git, grep, npm, ...) never reaches this script and
# keeps Cursor's normal approval flow.
#
# Decisions (this script never auto-approves anything):
#   deny - launchctl, sudo, reboot, shutdown, halt or pmset as a command
#          (also after ; && || | and env/command/exec/nohup/xargs wrappers),
#          or any of those words or LaunchAgents/LaunchDaemons in the
#          arguments of a command that is not text-only, inside $( ),
#          backticks, <( ), sh -c or eval, in a command that pipes into a
#          shell or xargs, or as a redirect target, or in a leading VAR=
#          or env VAR= assignment (GIT_EXTERNAL_DIFF, GIT_PAGER, LESSOPEN,
#          ... can run their value). Checked on the raw command
#          and on normalized copies (${IFS}/$IFS -> space; quotes and
#          backslashes removed; ${...}, $@, $* removed), so launch""ctl,
#          l\aunchctl and su''do are caught
#   ask  - the word only appears as text in the arguments of a read-only
#          tool: git commit/log/grep/diff/show/blame, grep, rg, ag, cat,
#          head, tail, less, more, wc, sed -n (no -i), ls (no LaunchAgents/
#          LaunchDaemons path), echo/printf with no redirect
#   deny - a read-only tool given a LaunchAgents/LaunchDaemons path (sed w,
#          git --output=... can write there); only grep/rg/ag search
#          patterns (pattern argument, -e/--regexp) and git log --grep/-S/-G
#          and git grep patterns stay ask. sed scripts with w/W/e/r/R
#          commands or a w/e s-flag, and sed -f, are not read-only
#   deny - running a named launchd script (deploy/macos/install.sh or
#          build-*-tv-helper.sh) by any route; only read-only tools such as
#          cat/grep/git/ls on them get ask
#   deny - running a script file whose text mentions launchctl, sudo,
#          reboot, shutdown, pmset, LaunchAgents or LaunchDaemons (any case).
#          Relative paths resolve against "cd" earlier in the command, then
#          the input "cwd", "workspace_roots", $CURSOR_PROJECT_DIR, and the
#          hook's own working directory (the project root)
#   deny - a program-running variable (GIT_EXTERNAL_DIFF, GIT_PAGER, PAGER,
#          MANPAGER, EDITOR, VISUAL, LESSOPEN, LESSCLOSE, GIT_SSH,
#          GIT_SSH_COMMAND, GIT_ASKPASS, GIT_EDITOR, GIT_SEQUENCE_EDITOR,
#          GIT_PROXY_COMMAND, SSH_ASKPASS) set as VAR=, env VAR= or
#          export VAR= to a command whose script file is a named launchd
#          script or whose text has a blocked word (scanned like a run
#          script, including sh/bash <file>, sh -c '...', source and cd in
#          the value); a LESSOPEN "|" and an alias/helper "!" are skipped
#   deny - git -c / --config-env setting a command key (core.pager, pager.*,
#          core.editor, sequence.editor, diff.external, diff.*.command/
#          textconv, difftool/mergetool.*.cmd, merge.*.driver, core.sshCommand,
#          core.askpass, core.fsmonitor, core.gitProxy, credential*.helper,
#          alias.*, filter.*.clean/smudge/process, gpg*.program,
#          interactive.diffFilter, hook.*.command) to a command with a blocked
#          word or a script that has one; core.hooksPath=<dir> scans every
#          file in that directory. --config-env reads the variable from a
#          VAR= set earlier in the same command, else ask. Any -c value with
#          a blocked word still denies (v2.2)
#   ask  - the script could not be found or read, or the script is clean, or
#          the matcher hit only inside another word (e.g. "asphalt")
#   deny - fail closed: empty or invalid JSON, missing or non-string
#          "command", or no usable python3
#
# Known gaps (not caught, or only get ask):
#   - scripts called by a scanned script are not scanned (one level only),
#     and words spelled with quotes/variables inside a script file are not
#     normalized
#   - make, npm/yarn scripts, python/node/perl/ruby files, xargs, find -exec
#     and compiled binaries are not inspected
#   - an absolute path without a script extension (/some/dir/tool) does not
#     reach the hook; paths under /usr, /bin, /sbin, /opt/homebrew, /System
#     are never scanned
#   - piping into a shell (curl ... | sh) with no blocked word only gets ask
#   - read-only tools that can still run code through config are trusted:
#     a LESSOPEN/GIT_PAGER exported in an earlier command or set in a profile,
#     a git alias or a pager/diff driver in .git/config or ~/.gitconfig
#   - a git -c command key whose value is a plain program name (less, vim,
#     ssh) only gets ask; files that program reads are not inspected
#   - a LaunchAgents/LaunchDaemons path built at run time (cd ~/Library &&
#     sed -n "w Launch$X/x") or written to by a pattern-only option is missed
#   - a word glued to a short option (git grep -Olaunchctl) is not seen
#   - $'\x..' quoting, command substitution that builds a word, aliases and
#     variables holding a word (C=launchctl; $C) are not decoded
#   - "ask" runs automatically when an agent uses --force or yolo mode, so
#     only "deny" is real protection
#
# Input (stdin):  {"command": "...", "cwd": "...", "workspace_roots": [...], ...}
# Output (stdout): {"permission": "deny"|"ask", "user_message": ..., "agent_message": ...}
# To remove: delete .cursor/hooks.json and .cursor/hooks/ (or revert the commit).

deny_raw() {
  printf '%s\n' '{"permission":"deny","user_message":"Blocked by the repo deny hook: '"$1"'","agent_message":"This shell command was blocked by .cursor/hooks/deny-dangerous-shell.sh ('"$1"'). launchd, sudo, reboot/shutdown/halt, pmset and LaunchAgents/LaunchDaemons changes need the owner to run them by hand. Do not retry or work around this."}'
  exit 0
}

PY=""
for cand in /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
  if [ -x "$cand" ]; then PY="$cand"; break; fi
done
[ -n "$PY" ] || deny_raw "python3 not available, failing closed"

INPUT=$(cat) || deny_raw "could not read hook input"
[ -n "$INPUT" ] || deny_raw "empty hook input"

HOOK_INPUT="$INPUT" "$PY" - <<'PYEOF' || deny_raw "hook parser failed"
import json, os, re, shlex, sys

def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.exit(0)

def deny(reason):
    out({
        "permission": "deny",
        "user_message": "Blocked by the repo deny hook: " + reason,
        "agent_message": (
            "This shell command was blocked by .cursor/hooks/deny-dangerous-shell.sh ("
            + reason + "). launchd, sudo, reboot/shutdown/halt, pmset and "
            "LaunchAgents/LaunchDaemons changes need the owner to run them by hand. "
            "Do not retry or work around this."
        ),
    })

def ask(why):
    out({
        "permission": "ask",
        "user_message": "The repo deny hook flagged this command (" + why + "). Approve only if it is safe.",
        "agent_message": (
            "This command reached the deny hook (" + why + "), so it needs the user's "
            "approval. Do not try to rephrase it to avoid the filter."
        ),
    })

raw = os.environ.get("HOOK_INPUT", "")
if not raw.strip():
    deny("empty hook input")
try:
    payload = json.loads(raw)
except Exception:
    deny("hook input is not valid JSON")
if not isinstance(payload, dict) or "command" not in payload:
    deny("hook input has no command field")
cmd = payload["command"]
if not isinstance(cmd, str):
    deny("command field is not a string")
# The shell drops backslash-newline before it splits words; do the same.
cmd = re.sub(r"\\\r?\n", "", cmd)

IFS_RE = re.compile(r"\$\{IFS[^}]*\}|\$IFS")
WORDS = re.compile(
    r"(?<![A-Za-z0-9_-])(launchctl|sudo|reboot|shutdown|halt|pmset)(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
LAUNCHD_DIRS = re.compile(r"launch(agents|daemons)", re.IGNORECASE)
BLOCKED = {"launchctl", "sudo", "reboot", "shutdown", "halt", "pmset"}

# --- 2. Scripts the command runs ------------------------------------------
SCAN = re.compile(
    rb"launch(?:agents|daemons)|(?<![a-z0-9_])(?:launchctl|sudo|reboot|shutdown|pmset)(?![a-z0-9_])",
    re.IGNORECASE,
)
NAMED = re.compile(r"(?:^|/)(?:deploy/macos/install\.sh|build-[a-z0-9_.-]+-tv-helper\.sh)$", re.IGNORECASE)
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
PREFIXES = {"env", "command", "exec", "nohup", "time", "nice", "builtin", "then", "do",
            "else", "elif", "if", "while", "until", "!", "{", "}"}
READ_ONLY = {"cat", "less", "more", "head", "tail", "grep", "egrep", "fgrep", "rg", "git",
             "ls", "wc", "diff", "cmp", "file", "stat", "shellcheck", "bat", "sed"}
SCRIPT_EXT = (".sh", ".bash", ".zsh", ".command")
SYSTEM_DIRS = ("/usr/", "/bin/", "/sbin/", "/opt/homebrew/", "/System/")
SEPS = {";", "&&", "||", "|", "&", "(", ")", ";;", "|&", ";&"}

def tokenize(s):
    s = s.replace("`", " ; ").replace("\n", " ; ").replace("$(", " ( ")
    try:
        lx = shlex.shlex(s, posix=True, punctuation_chars=True)
        lx.whitespace_split = True
        lx.commenters = ""
        return list(lx)
    except ValueError:
        s = re.sub(r"[\"'\\]", "", s)
        return re.findall(r"&&|\|\||[;&|()<>]|[^\s;&|()<>]+", s)

def segments(tokens):
    seg = []
    for t in tokens:
        if t in SEPS:
            if seg:
                yield seg
            seg = []
        else:
            seg.append(t)
    if seg:
        yield seg

roots = []
c = payload.get("cwd")
if isinstance(c, str) and c:
    roots.append(c)
wr = payload.get("workspace_roots")
if isinstance(wr, list):
    roots += [r for r in wr if isinstance(r, str) and r]
if os.environ.get("CURSOR_PROJECT_DIR"):
    roots.append(os.environ["CURSOR_PROJECT_DIR"])
roots.append(os.getcwd())

home = os.environ.get("HOME", "")
def expand(p):
    p = re.sub(r"^\$\{?HOME\}?(?=/|$)", home, p) if home else p
    return os.path.expanduser(p)

def resolve(p, cd):
    """Return (display, absolute path or None)."""
    p = expand(p)
    if os.path.isabs(p):
        return p, p
    for base in ([cd] if cd else []) + roots:
        cand = os.path.normpath(os.path.join(expand(base), p))
        if os.path.exists(cand):
            return p, cand
    return p, None

def is_named(p, absp):
    for x in (os.path.normpath(p), absp or ""):
        if x and NAMED.search(x.replace("\\", "/")):
            return True
    return False

# --- 1. Blocked words in the command ---------------------------------------
# Normalized copies: ${IFS}/$IFS -> space; quotes and backslashes removed;
# ${...}, $@, $* removed. So launch""ctl, l\aunchctl and su''do are caught.
v_ifs = IFS_RE.sub(" ", cmd)
v_strip = re.sub(r"[\"'\\]", "", v_ifs)
v_noexp = re.sub(r"\$\{[^}]*\}|\$[@*]", "", re.sub(r"[\"'\\]", "", IFS_RE.sub("", cmd)))
variants = [cmd, v_ifs, v_strip, v_noexp]

def word_in(text):
    m = WORDS.search(text)
    if m:
        return "'" + m.group(1).lower() + "'"
    if LAUNCHD_DIRS.search(text):
        return "LaunchAgents/LaunchDaemons"
    return None

hit = None
for v in variants:
    hit = hit or word_in(v)

# A word hit is only text (ask) when every simple command that holds it is a
# read-only / text-only tool and the word sits in its arguments. Anything
# else denies: the word as a command (also after ; && || | and wrappers),
# inside $( ), backticks or <( ), with eval / sh -c / a pipe into a shell,
# or a redirect into LaunchAgents/LaunchDaemons.
TEXT_TOOLS = {"grep", "egrep", "fgrep", "rg", "ag", "cat", "head", "tail", "less",
              "more", "wc", "ls", "sed", "echo", "printf", "git"}
GIT_TEXT = {"commit", "log", "grep", "diff", "show", "blame"}
RUNNERS = SHELLS | {"eval", "source", ".", "xargs", "exec"}
WRAPPERS = PREFIXES | {"xargs"}

# A path that names a launchd directory as a path component.
LAUNCHD_PATH = re.compile(r"(?:^|[/=~])launch(?:agents|daemons)(?:/|$)", re.IGNORECASE)
GREP_VALUE_OPTS = {"-A", "-B", "-C", "-m", "-f", "--file", "--max-count", "--context",
                   "--after-context", "--before-context", "--exclude", "--include",
                   "--exclude-dir", "--label", "--max-depth"}
VALUE_OPTS = {
    "grep": GREP_VALUE_OPTS | {"-d", "-D", "--directories", "--devices"},
    "rg": GREP_VALUE_OPTS | {"-g", "-t", "-T", "-j", "-M", "-E", "-r", "--replace", "--glob",
                             "--iglob", "--type", "--type-not", "--threads", "--encoding",
                             "--sort", "--sortr", "--color", "--colors", "--max-columns"},
    "ag": GREP_VALUE_OPTS | {"-G", "--ignore", "--depth", "--file-search-regex"},
}
SED_CMD = re.compile(r"(?:^|[;\n{}!$0-9/,\s])\s*[wWerR](?:\s|$|;|})")
SED_SFLAG = re.compile(r"/[gpiImM0-9]*[we](?:\s|$|;|})")

def pattern_args(args, tool):
    """Indexes of search-pattern arguments (grep/rg/ag/git grep)."""
    idx, explicit, i, first = set(), False, 0, None
    while i < len(args):
        a = args[i]
        if a == "--":
            if first is None and i + 1 < len(args):
                first = i + 1
            break
        if a in ("-e", "--regexp") and i + 1 < len(args):
            idx.add(i + 1); explicit = True; i += 2; continue
        if a.startswith("--regexp=") or (a.startswith("-e") and len(a) > 2 and not a.startswith("--")):
            idx.add(i); explicit = True; i += 1; continue
        if a in ("-f", "--file") or a.startswith("--file="):
            explicit = True
        if a in VALUE_OPTS.get(tool, GREP_VALUE_OPTS):
            i += 2; continue
        if a.startswith("-") and a != "-":
            i += 1; continue
        if first is None:
            first = i
        i += 1
    if not explicit and first is not None:
        idx.add(first)
    return idx

def sed_ok(args):
    scripts, i, first = [], 0, None
    while i < len(args):
        a = args[i]
        if a in ("-f", "--file") or a.startswith("--file=") or re.match(r"^-[a-zA-Z]*f", a):
            return False
        if a in ("-e", "--expression") and i + 1 < len(args):
            scripts.append(args[i + 1]); i += 2; continue
        if a.startswith("--expression="):
            scripts.append(a.split("=", 1)[1]); i += 1; continue
        if a in ("-l", "--line-length"):
            i += 2; continue
        if a.startswith("-") and a != "-":
            i += 1; continue
        if first is None:
            first = a
        i += 1
    if not scripts and first is not None:
        scripts.append(first)
    return not any(SED_CMD.search(x) or SED_SFLAG.search(x) for x in scripts)

def strip_exp(t):
    return re.sub(r"\$\{[^}]*\}|\$[@*]", "", t)

def text_only(w, args):
    redirect_to = [args[i + 1] for i, a in enumerate(args[:-1]) if a.startswith((">", "&>")) or a == ">|"]
    if any(LAUNCHD_DIRS.search(t) for t in redirect_to):
        return False
    if w not in TEXT_TOOLS:
        return False
    # A launchd directory path in the arguments: deny, except search patterns.
    safe = set()
    if w in ("grep", "egrep", "fgrep", "rg", "ag"):
        safe = pattern_args(args, {"egrep": "grep", "fgrep": "grep"}.get(w, w))
    elif w == "git":
        j = 0
        while j < len(args) and args[j].startswith("-"):
            j += 2 if args[j] in ("-C", "--git-dir", "--work-tree", "--namespace", "-c") else 1
        if j < len(args) and args[j] == "grep":
            safe = {j + 1 + k for k in pattern_args(args[j + 1:], "grep")}
        elif j < len(args) and args[j] == "log":
            for k in range(j + 1, len(args)):
                a = args[k]
                if a in ("--grep", "-S", "-G") and k + 1 < len(args):
                    safe.add(k + 1)
                elif a.startswith(("--grep=", "-S", "-G")):
                    safe.add(k)
    if any(LAUNCHD_PATH.search(a) for k, a in enumerate(args) if k not in safe):
        return False
    if w in ("echo", "printf"):
        return not any(a.startswith((">", "&>")) for a in args)
    if w == "ls":
        return not any(LAUNCHD_DIRS.search(a) for a in args)
    if w == "rg":
        return not any(a.startswith("--pre") for a in args)
    if w == "sed":
        quiet = any(a in ("-n", "--quiet", "--silent") or re.match(r"^-[a-zA-Z]*n[a-zA-Z]*$", a) for a in args)
        if not quiet or any(a.startswith(("-i", "--in-place")) or re.match(r"^-[a-zA-Z]*i", a) for a in args):
            return False
        return sed_ok(args)
    if w == "git":
        i = 0
        while i < len(args) and args[i].startswith("-"):
            if args[i] == "-c" or args[i].startswith("--config-env"):
                return False
            i += 2 if args[i] in ("-C", "--git-dir", "--work-tree", "--namespace") else 1
        if i >= len(args) or args[i] not in GIT_TEXT:
            return False
        if args[i] == "grep" and any(a.startswith(("-O", "--open-files-in-pager")) for a in args):
            return False
        return True
    return True

if hit:
    if "$(" in cmd or "`" in cmd or "<(" in cmd:
        deny("command uses " + hit + " inside a command substitution")
    for v in (cmd, IFS_RE.sub(" ", cmd), strip_exp(IFS_RE.sub("", cmd))):
        try:
            lx = shlex.shlex(v.replace("\n", " ; "), posix=True, punctuation_chars=True)
            lx.whitespace_split = True
            lx.commenters = ""
            toks = list(lx)
        except ValueError:
            deny("command uses " + hit + " and could not be parsed")
        segs = list(segments(toks))
        runs_code = False
        for seg in segs:
            i = 0
            while i < len(seg) and (strip_exp(seg[i]) in WRAPPERS or os.path.basename(seg[i]) in WRAPPERS
                                    or re.match(r"^[A-Za-z_][A-Za-z0-9_]*\+?=", seg[i])
                                    or (i > 0 and seg[i].startswith("-") and os.path.basename(seg[i - 1]) in ("env", "nice", "xargs"))):
                if os.path.basename(seg[i]) in ("xargs", "exec"):
                    runs_code = True
                if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\+?=", seg[i]) and word_in(seg[i]):
                    deny("command sets an environment variable to a value with " + word_in(seg[i]))
                i += 1
            if i >= len(seg):
                continue
            w = os.path.basename(strip_exp(seg[i])).lower()
            args = [strip_exp(a) for a in seg[i + 1:]]
            if w in BLOCKED:
                deny("command uses '" + w + "'")
            if w in RUNNERS:
                runs_code = True
            found = word_in(" ".join([w] + args))
            if found and not text_only(w, args):
                deny("command uses " + found)
        if runs_code:
            deny("command uses " + hit + " and runs code through a shell, eval or xargs")

unreadable = []
clean = []

def check_script(p, cd):
    shown, absp = resolve(p, cd)
    if is_named(shown, absp):
        deny("runs launchd script " + os.path.basename(shown))
    if absp is None or "$" in shown:
        unreadable.append(shown)
        return
    if absp.startswith(SYSTEM_DIRS):
        return
    try:
        with open(absp, "rb") as f:
            data = f.read(4 * 1024 * 1024)
    except Exception:
        unreadable.append(shown)
        return
    m = SCAN.search(data)
    if m:
        deny("script " + shown + " contains '" + m.group(0).decode("latin-1").lower() + "'")
    clean.append(shown)

# --- 3. Commands run through env vars, eval, sh -c and git config (v2.4) ---
# Backslash-newline is joined first. sh -c / bash -c / eval / env -S strings
# are scanned like a command. GIT_CONFIG_COUNT/KEY_n/VALUE_n and
# GIT_CONFIG_PARAMETERS count as git -c. include.path, includeIf.*.path and
# GIT_CONFIG_GLOBAL/SYSTEM/GIT_CONFIG files are parsed and their command keys
# checked. GIT_SSH, GIT_ASKPASS, SSH_ASKPASS and core.askpass are one literal
# path. git --upload-pack/--receive-pack/--exec (and clone -u) values are run.
RUN_VARS = {"GIT_EXTERNAL_DIFF", "GIT_PAGER", "PAGER", "MANPAGER", "EDITOR", "VISUAL",
            "LESSOPEN", "LESSCLOSE", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_ASKPASS",
            "GIT_EDITOR", "GIT_SEQUENCE_EDITOR", "GIT_PROXY_COMMAND", "SSH_ASKPASS"}
# Run as one program path, never split by a shell.
LITERAL_VARS = {"GIT_SSH", "GIT_ASKPASS", "SSH_ASKPASS"}
CONFIG_FILE_VARS = {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG"}
EXPORTS = {"export", "declare", "typeset", "readonly", "local"}
ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\+?=(.*)$", re.S)
CMD_KEY = re.compile(
    r"^(?:core\.(?:pager|editor|sshcommand|askpass|fsmonitor|gitproxy)|pager\..+|sequence\.editor"
    r"|diff\.external|diff\..+\.(?:command|textconv)|(?:diff|merge)tool\..+\.cmd|merge\..+\.driver"
    r"|credential(?:\..+)?\.helper|alias\..+|filter\..+\.(?:clean|smudge|process)"
    r"|gpg(?:\..+)?\.program|interactive\.difffilter|hook\..+\.command"
    r"|remote\..+\.(?:uploadpack|receivepack))$")
LITERAL_KEYS = {"core.askpass"}
INCLUDE_KEY = re.compile(r"^(?:include|includeif\..+)\.path$")
GIT_RUN_OPTS = {"--upload-pack", "--receive-pack", "--exec"}
# LESSOPEN "|" / "||" (optionally "-"), and the "!" of an alias or helper.
LEAD = re.compile(r"^\s*(?:\|\|?-?|!)\s*")
MAX_DEPTH = 8

def value_word(value):
    for v in (value, IFS_RE.sub(" ", value), re.sub(r"[\"'\\]", "", value), strip_exp(re.sub(r"[\"'\\]", "", IFS_RE.sub("", value)))):
        f = word_in(v)
        if f:
            return f
    return None

def check_value(value, cd, what, depth=0, literal=False):
    """A variable or git config value that git/less/ssh runs as a command."""
    f = value_word(value)
    if f:
        deny(what + " runs a command with " + f)
    if literal:
        if value.strip():
            check_script(value.strip(), cd)
        return
    scan_command(LEAD.sub("", value), cd, depth + 1, what)

def config_value(v):
    out, q, i = [], False, 0
    v = v.strip()
    while i < len(v):
        ch = v[i]
        if ch == "\\" and i + 1 < len(v):
            out.append({"n": "\n", "t": "\t", "b": "\b"}.get(v[i + 1], v[i + 1])); i += 2; continue
        if ch == '"':
            q = not q
        elif ch in "#;" and not q:
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out).strip()

def parse_config(text):
    lines, buf = [], ""
    for raw in text.splitlines():
        if raw.endswith("\\") and not raw.endswith("\\\\"):
            buf += raw[:-1]
            continue
        lines.append(buf + raw); buf = ""
    if buf:
        lines.append(buf)
    section, items = "", []
    for line in lines:
        s = line.strip()
        if not s or s[0] in "#;":
            continue
        m = re.match(r'^\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\](.*)$', s)
        if m:
            section = m.group(1).lower()
            if m.group(2) is not None:
                section += "." + re.sub(r"\\(.)", r"\1", m.group(2))
            s = m.group(3).strip()
            if not s or s[0] in "#;":
                continue
        k, sep, v = s.partition("=")
        key = k.strip().lower()
        if re.match(r"^[a-z][a-z0-9-]*$", key):
            items.append(((section + "." if section else "") + key, config_value(v) if sep else ""))
    return items

def check_config_file(path, cd, what, depth):
    if depth > MAX_DEPTH:
        unreadable.append("deeply nested git config " + path)
        return
    shown, absp = resolve(path, cd)
    if absp is None or "$" in shown:
        unreadable.append("git config file " + shown)
        return
    if not os.path.isfile(absp):
        return
    try:
        with open(absp, "rb") as f:
            text = f.read(1024 * 1024).decode("utf-8", "replace")
    except Exception:
        unreadable.append("git config file " + shown)
        return
    for key, val in parse_config(text):
        if INCLUDE_KEY.match(key) and not os.path.isabs(expand(val)):
            val = os.path.join(os.path.dirname(absp), val)
        check_config(key, val, cd, depth)

def check_config(key, val, cd, depth):
    what = "git config " + key
    f = value_word(val)
    if f:
        deny(what + " sets a value with " + f)
    if INCLUDE_KEY.match(key):
        check_config_file(val, cd, what, depth + 1)
    elif key == "core.hookspath":
        _, d = resolve(val, cd)
        if not d or not os.path.isdir(d):
            unreadable.append("git hooks directory " + val)
            return
        for name in sorted(os.listdir(d))[:200]:
            fp = os.path.join(d, name)
            if os.path.isfile(fp):
                check_script(fp, None)
    elif key in LITERAL_KEYS:
        check_value(val, cd, what, depth, literal=True)
    elif key.startswith("alias.") and not val.lstrip().startswith("!"):
        check_git(tokenize(val), cd, {}, depth + 1)
    elif CMD_KEY.match(key):
        check_value(val, cd, what, depth)

def env_assignments(assigns, cd, depth):
    """VAR=value pairs that make git, less or ssh run something."""
    env = {}
    for name, val in assigns:
        env[name] = val
        if name in RUN_VARS:
            check_value(val, cd, name, depth, literal=name in LITERAL_VARS)
        elif name in CONFIG_FILE_VARS:
            check_config_file(val, cd, name, depth)
        elif name == "GIT_CONFIG_PARAMETERS":
            try:
                items = shlex.split(val)
            except ValueError:
                unreadable.append("GIT_CONFIG_PARAMETERS value")
                items = []
            for it in items:
                k, sep, v = it.partition("=")
                if sep:
                    check_config(k.strip().lower(), v, cd, depth)
    for name in env:
        m = re.match(r"^GIT_CONFIG_KEY_(\d+)$", name)
        if m:
            check_config(env[name].strip().lower(), env.get("GIT_CONFIG_VALUE_" + m.group(1), ""), cd, depth)
    return env

def check_git(args, cd, env, depth=0):
    """git's global options (-C, -c, --config-env) and commands it runs (--upload-pack, ...)."""
    if depth > MAX_DEPTH:
        return
    i, gcd = 0, cd
    while i < len(args):
        a, kv = args[i], None
        if a == "-C" and i + 1 < len(args):
            _, d = resolve(args[i + 1], gcd)
            gcd = d if d and os.path.isdir(d) else gcd
            i += 2
            continue
        if a in ("-c", "--config-env") and i + 1 < len(args):
            kv, i = args[i + 1], i + 2
        elif a.startswith("--config-env="):
            a, kv, i = "--config-env", a[len("--config-env="):], i + 1
        elif a in ("--git-dir", "--work-tree", "--namespace", "--super-prefix", "--exec-path") and i + 1 < len(args):
            i += 2
            continue
        elif a.startswith("-"):
            i += 1
            continue
        else:
            break
        key, sep, val = kv.partition("=")
        key = key.strip().lower()
        if a == "--config-env":
            if val in env:
                val = env[val]
            else:
                unreadable.append("git --config-env value from $" + val)
                continue
        elif not sep:
            continue
        check_config(key, val, gcd, depth)
    sub, rest, k = (args[i] if i < len(args) else ""), args[i + 1:], 0
    while k < len(rest):
        name, eq, v = rest[k].partition("=")
        if rest[k] == "--":
            break
        if name in GIT_RUN_OPTS or (sub == "clone" and name == "-u"):
            if eq:
                check_value(v, gcd, "git " + name, depth)
            elif k + 1 < len(rest):
                check_value(rest[k + 1], gcd, "git " + name, depth)
                k += 1
        k += 1

def cmd_start(seg):
    """Index of the command word after wrappers, VAR= and env options; the VAR= pairs; env -S strings."""
    i, assigns, inline = 0, [], []
    while i < len(seg):
        t = seg[i]
        b = os.path.basename(t)
        m = ASSIGN.match(t)
        if m:
            assigns.append((m.group(1), m.group(2))); i += 1; continue
        if b == "env":
            i += 1
            while i < len(seg):
                a = seg[i]
                if a in ("-u", "--unset", "-C", "--chdir", "-P") and i + 1 < len(seg):
                    i += 2; continue
                if a in ("-S", "--split-string") and i + 1 < len(seg):
                    inline.append(seg[i + 1]); i += 2; continue
                if a.startswith("--split-string="):
                    inline.append(a.split("=", 1)[1]); i += 1; continue
                if a == "--":
                    i += 1; break
                if a.startswith("-"):
                    i += 1; continue
                break
            continue
        if b == "nice":
            i += 1
            if i < len(seg) and seg[i] == "-n":
                i += 2
            elif i < len(seg) and seg[i].startswith("-"):
                i += 1
            continue
        if t in PREFIXES or b in PREFIXES:
            i += 1
            while i < len(seg) and seg[i].startswith("-") and b in ("command", "exec", "time"):
                i += 2 if (b == "exec" and seg[i] == "-a") else 1
            continue
        break
    return i, assigns, inline

def scan_command(text, cd, depth=0, what=None):
    """Scripts, env vars and git config a command (or an inline command string) runs."""
    if depth > MAX_DEPTH:
        unreadable.append("deeply nested command")
        return
    for seg in segments(tokenize(text)):
        i, assigns, inline = cmd_start(seg)
        env = env_assignments(assigns, cd, depth)
        for s in inline:
            scan_command(s, cd, depth + 1, what)
        if i >= len(seg):
            continue
        w, args = seg[i], seg[i + 1:]
        base = os.path.basename(w)
        if base in EXPORTS:
            env_assignments([(m.group(1), m.group(2)) for m in (ASSIGN.match(a) for a in args) if m], cd, depth)
            continue
        if base == "git":
            check_git(args, cd, env, depth)
        # A named launchd script anywhere in a non-read-only command: deny.
        if base not in READ_ONLY or what:
            for a in args:
                if a not in ("<", ">", ">>") and is_named(a, resolve(a, cd)[1]):
                    deny((what + " runs" if what else "runs") + " launchd script " + os.path.basename(a))
        if base == "cd":
            _, absd = resolve(args[0] if args else home, cd)
            cd = absd if absd and os.path.isdir(absd) else cd
            continue
        if base == "eval":
            scan_command(" ".join(args), cd, depth + 1, what)
            continue
        if base in SHELLS:
            j, target, inline_c = 0, None, None
            while j < len(args):
                a = args[j]
                if a == "<" and j + 1 < len(args):
                    target = args[j + 1]
                    break
                if a.startswith(("-", "+")):
                    if a in ("-o", "+o", "-O", "+O"):
                        j += 2
                        continue
                    if "c" in a.lstrip("-+") and not a.startswith("--"):
                        inline_c = args[j + 1] if j + 1 < len(args) else ""
                        break
                    j += 1
                    continue
                target = a
                break
            if inline_c is not None:
                scan_command(inline_c, cd, depth + 1, what)
            elif target:
                check_script(target, cd)
            else:
                unreadable.append("script read from stdin")
            continue
        if base in ("source", "."):
            if args:
                check_script(args[0], cd)
            continue
        if w.lower().endswith(SCRIPT_EXT) or "/" in w:
            check_script(w, cd)

for v in (v_ifs, IFS_RE.sub("", cmd)):
    scan_command(v, None)

if unreadable:
    ask("could not read script " + unreadable[0])
if clean and not hit:
    ask("runs script " + clean[0] + "; no launchd/sudo/power words found in it")
if re.search(r"(?:^|[\s;&|(`{\"'\\])(?:GIT_[A-Za-z0-9_]*|LESS[A-Za-z0-9_]*|PAGER|MANPAGER|EDITOR|VISUAL|SSH_ASKPASS)\+?=", cmd):
    ask("it sets a GIT_*/LESS*/PAGER/EDITOR variable that can run a program")
if re.search(r"(?:^|[^A-Za-z0-9_-])g(?:\\|''|\"\")*i(?:\\|''|\"\")*t\b.*\s[\"']?(?:-c|--config-env|--upload-pack|--receive-pack|--exec)(?:[\"']?(?:[\s=]|$))", cmd):
    ask("it passes git a -c/--config-env/--upload-pack setting that can run a program")
if hit:
    ask("it mentions " + hit + " only as text in a read-only command")
ask("it contains a launchd/sudo/power word inside another word, or a script-run pattern")
PYEOF
