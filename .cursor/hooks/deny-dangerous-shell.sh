#!/bin/sh
# Cursor beforeShellExecution hook: fail-closed deny list (v2.2).
#
# hooks.json only routes a command here when its "matcher" regex sees:
#   - launchctl, sudo, reboot, shutdown, halt, pmset, launchagents or
#     launchdaemons (any case), also with \ '' "" ${...} $@ $* between letters
#   - a script run in command position: ./x, ../x, dir/x, ~/x, $HOME/x,
#     anything ending .sh/.bash/.zsh/.command, sh/bash/zsh/dash/ksh, source, .
#     (a quoted path such as "./dir with space/x.sh" counts too)
#   - the named launchd scripts (deploy/macos/install.sh, *-tv-helper.sh)
#   - a GIT_*, LESS*, PAGER, MANPAGER, EDITOR or VISUAL= assignment (these
#     can make a read-only tool run a program), which then gets at least ask
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
#     a LESSOPEN/GIT_PAGER exported earlier or set in a profile, a git alias
#     or a pager/diff driver in .git/config or ~/.gitconfig
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
                                    or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[i])
                                    or (i > 0 and seg[i].startswith("-") and os.path.basename(seg[i - 1]) in ("env", "nice", "xargs"))):
                if os.path.basename(seg[i]) in ("xargs", "exec"):
                    runs_code = True
                if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[i]) and word_in(seg[i]):
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

for v in (v_ifs, IFS_RE.sub("", cmd)):
    cd = None
    for seg in segments(tokenize(v)):
        i = 0
        while i < len(seg) and (seg[i] in PREFIXES or os.path.basename(seg[i]) in PREFIXES
                                or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[i])
                                or (i > 0 and seg[i].startswith("-") and os.path.basename(seg[i - 1]) in ("env", "nice"))):
            i += 1
        if i >= len(seg):
            continue
        w, args = seg[i], seg[i + 1:]
        base = os.path.basename(w)
        # A named launchd script anywhere in a non-read-only command: deny.
        if base not in READ_ONLY:
            for a in args:
                if a not in ("<", ">", ">>") and is_named(a, resolve(a, cd)[1]):
                    deny("runs launchd script " + os.path.basename(a))
        if base == "cd":
            target = args[0] if args else home
            _, absd = resolve(target, cd)
            cd = absd if absd and os.path.isdir(absd) else cd
            continue
        if base in SHELLS:
            j, target = 0, None
            while j < len(args):
                a = args[j]
                if a == "<" and j + 1 < len(args):
                    target = args[j + 1]
                    break
                if a.startswith(("-", "+")):
                    if a in ("-o", "+o", "-O", "+O"):
                        j += 1
                    elif "c" in a.lstrip("-+") and not a.startswith("--"):
                        break  # inline -c string: its words were checked above
                    j += 1
                    continue
                target = a
                break
            if target:
                check_script(target, cd)
            else:
                unreadable.append("script read from stdin or -c")
            continue
        if base in ("source", "."):
            if args:
                check_script(args[0], cd)
            continue
        if w.lower().endswith(SCRIPT_EXT) or "/" in w:
            check_script(w, cd)

if unreadable:
    ask("could not read script " + unreadable[0])
if clean and not hit:
    ask("runs script " + clean[0] + "; no launchd/sudo/power words found in it")
if re.search(r"(?:^|[\s;&|(])(?:GIT_[A-Za-z0-9_]*|LESS[A-Za-z0-9_]*|PAGER|MANPAGER|EDITOR|VISUAL)=", cmd):
    ask("it sets a GIT_*/LESS*/PAGER/EDITOR variable that can run a program")
if hit:
    ask("it mentions " + hit + " only as text in a read-only command")
ask("it contains a launchd/sudo/power word inside another word, or a script-run pattern")
PYEOF
