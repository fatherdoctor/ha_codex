"""Bridge Codex app-server turns to explicit, single-action user approvals.

The worker owns the process and authentication. This adapter owns JSON-RPC
correlation and live requests; only redacted display records leave it. Approval
responses never install command rules or grant access for a session.
"""

from __future__ import annotations

import copy
import fnmatch
import json
import posixpath
import re
import shlex
import threading
import time
import uuid
from typing import Any, Callable, TextIO


APPROVAL_GONE = "This permission request is no longer waiting for an answer."
DISPLAY_LIMIT = 32000
REQUEST_LIMIT = 50
COMMAND_APPROVAL = "item/commandExecution/requestApproval"
# The app's permission policy, its house rules and Codex's own configuration: a change there is never allowed here.
PROTECTED_PREFIXES = ("/usr/share/ha_codex", "/data/codex-home/AGENTS.md", "/data/codex-home/config.toml",
                      "/data/options.json", "/data/worker_api_token", "/data/schedules.sqlite3",
                      "/data/schedules.sqlite3-journal", "/data/schedules.sqlite3-wal", "/data/schedules.sqlite3-shm",
                      "/data/interaction-diagnostics.json", "/data/interaction-diagnostics.key",
                      "/data/house-rules", "/data/interaction-diagnostics.settings.json", "/data/codex-home/house-rules")


def touches_protected_path(path: str) -> bool:
    """Return whether a file change path is, or lies inside, a protected policy location."""
    normalized = posixpath.normpath(posixpath.join("/config", str(path)))
    if normalized.startswith('/data/interaction-diagnostics.json.corrupt-'):
        return True
    return any(normalized == prefix or normalized.startswith(prefix + "/") for prefix in PROTECTED_PREFIXES)


# The same places as a command spells them: quotes, backslashes and doubled slashes are removed first, and the
# names are matched loosely (a glob such as /usr/share/ha_c* still names the image folder). Best effort on the
# text, like the file-path check: it cannot see a path a command builds at run time.
PROTECTED_COMMAND_RES = (
    re.compile(r"codex-home/+house-rules(?:\b|[*?\[])", re.IGNORECASE),
    re.compile(r"/data/+interaction-diagnostics(?:\b|[*?\[])", re.IGNORECASE),
    re.compile(r"/share/+ha_", re.IGNORECASE),
    re.compile(r"ha_codex/+(policy|agents|house)", re.IGNORECASE),
    re.compile(r"codex-home/+[^\s/]*[*?\[]", re.IGNORECASE),
    re.compile(r"codex-home.*(agents\.md|config\.toml)|(agents\.md|config\.toml).*codex-home", re.IGNORECASE | re.DOTALL),
    re.compile(r"/data/+options\.json|worker_api_token|schedules\.sqlite3(?:-(?:journal|wal|shm))?|/data/+sched[^/\s]*[*?\[]", re.IGNORECASE),
)


# Characters after which a ``#`` starts a word. An escaped character is never one of them.
WORD_BREAKS = frozenset(" \t\r\n;&|()<>")


def _strip_comments(text: str) -> str:
    """Remove shell comments: from a ``#`` that starts a word outside quotes to the end of its line.

    A ``#`` inside a word, such as ``a#b``, is part of that word, as it is for the shell.
    """
    out: list[str] = []
    quote = ""
    i = 0
    while i < len(text):
        char = text[i]
        if quote == "'":
            if char == "'":
                quote = ""
        elif char == "\\":
            # A backslash keeps the next character, except inside single quotes.
            out.append(text[i:i + 2])
            i += 2
            continue
        elif quote == '"':
            if char == '"':
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "#" and (not out or out[-1] in WORD_BREAKS):
            end = text.find("\n", i)
            i = len(text) if end == -1 else end
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _split_words(text: str, punctuation: str) -> list[str]:
    """Split text the way the shell does, with comments already removed; raise ValueError if unreadable."""
    lexer = shlex.shlex(text, posix=True, punctuation_chars=punctuation)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _shell_words(text: str, punctuation: str) -> list[str]:
    """Split command text into words and shell punctuation, without running it.

    Raise ValueError when the text cannot be read with its quoting intact, such as an
    unfinished quote from an apostrophe in a heredoc body or a quoted script that spans
    lines in a way the splitter cannot follow. The caller treats that as touching a
    protected place: a command allowed once runs outside the sandbox, so a command this
    check cannot read is never offered Allow once. gate_rules.py reads such text again
    line by line instead, because its Supervisor rule has a text floor behind it.
    """
    return _split_words(_strip_comments(text), punctuation)


def _protected_ancestor_operations(command: str, initial_cwd: str = '', depth: int = 0) -> bool:
    """Conservatively identify named requirements, root searches and writes into root or /etc.

    Command text that cannot be split with its quoting intact counts as protected.

    This checks command text only; it does not follow aliases, symlinks or computed paths.
    Rare forms such as bsdtar, cpio, 7z, remote rsync, find -O3 and computed Python paths can be missed.
    """
    if depth > 5:
        return False
    try:
        tokens = _shell_words(command.replace('\\\n', ' '), ';&|()\n<>')
    except ValueError:
        # Fail closed: text whose quoting cannot be read might hide a protected write.
        return True
    segments, segment = [], []
    for token in tokens:
        if token and all(ch in ';&|()\n<>' for ch in token):
            if segment:
                segments.append(segment); segment = []
        else:
            segment.append(token)
    if segment:
        segments.append(segment)
    cwd = initial_cwd

    def target_path(word: str) -> str:
        path = posixpath.normpath(word if word.startswith('/') else posixpath.join(cwd, word))
        return '/' + path.lstrip('/') if path.startswith('//') else path

    def protected_destination(word: str) -> bool:
        path = target_path(word).lower()
        parts = path.split('/')
        return path == '/' or (len(parts) > 1 and parts[0] == '' and fnmatch.fnmatchcase('etc', parts[1]))

    for segment in segments:
        for word in segment:
            lower = word.lower()
            base = posixpath.basename(lower)
            readable = re.sub(r'\[([a-z0-9.])\]', r'\1', lower)
            readable = re.sub(r'\\(.)', r'\1', readable)
            if ('requirements.toml' in readable or
                    (any(ch in base for ch in '*?[') and base not in ('*', '?')
                     and fnmatch.fnmatchcase('requirements.toml', base))):
                return True
        index = 0
        while index < len(segment):
            word = segment[index]
            if re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', word):
                index += 1
                continue
            wrapper = posixpath.basename(word).lower()
            if wrapper not in ('sudo', 'env', 'command', 'nohup', 'timeout', 'nice', 'ionice',
                               'doas', 'setsid', 'stdbuf', 'xargs', 'time', 'exec', 'unbuffer'):
                break
            index += 1
            while index < len(segment) and segment[index].startswith('-'):
                flag = segment[index]
                index += 1
                if wrapper == 'env' and (flag.startswith('--chdir=') or flag.startswith('-C') and len(flag) > 2):
                    path = flag.split('=', 1)[1] if flag.startswith('--chdir=') else flag[2:]
                    cwd = target_path(path) if path.startswith('/') or cwd else ''
                elif ((wrapper in ('sudo', 'doas') and flag in ('-u', '--user', '-g', '--group'))
                      or (wrapper == 'env' and flag in ('-u', '--unset', '-C', '--chdir'))
                      or (wrapper == 'timeout' and flag in ('-s', '--signal', '-k', '--kill-after'))
                      or (wrapper == 'nice' and flag in ('-n', '--adjustment'))
                      or (wrapper == 'time' and flag in ('-f', '--format', '-o', '--output'))
                      or (wrapper == 'exec' and flag == '-a')
                      or (wrapper == 'ionice' and flag in ('-c', '--class', '-n', '--classdata', '-p', '-P'))
                      or (wrapper == 'stdbuf' and flag in ('-i', '-o', '-e', '--input', '--output', '--error'))
                      or (wrapper == 'xargs' and flag in ('-I', '-n', '-L', '-P', '-s', '-a', '-E', '-d',
                                                        '--replace', '--max-args', '--max-lines', '--max-procs',
                                                        '--max-chars', '--arg-file', '--eof', '--delimiter'))) and index < len(segment):
                    value = segment[index]; index += 1
                    if wrapper == 'env' and flag in ('-C', '--chdir'):
                        cwd = target_path(value) if value.startswith('/') or cwd else ''
            if wrapper == 'timeout' and index < len(segment):
                index += 1  # duration
        if index >= len(segment):
            continue
        program = posixpath.basename(segment[index]).lower()
        args = segment[index + 1:]
        if program in ('sh', 'bash', 'zsh'):
            for i, flag in enumerate(args):
                if flag.startswith('-') and 'c' in flag[1:]:
                    script = i + 1
                    if script < len(args) and args[script] == '--':
                        script += 1
                    if script < len(args) and _protected_ancestor_operations(args[script], cwd, depth + 1):
                        return True
                    break
        if program in ('cd', 'pushd'):
            paths = [word for word in args if not word.startswith('-')]
            if paths:
                candidate = paths[0]
                cwd = target_path(candidate) if candidate.startswith('/') or cwd else ''
            continue
        if program == 'find':
            starts = []
            for word in args:
                if word in ('-H', '-L', '-P', '--') and not starts:
                    continue
                if word.startswith('-'):
                    break
                starts.append(word)
            for start in starts or ('.',):
                if cwd.lower() in ('/', '/etc') and not start.startswith('/'):
                    return True
                path = target_path(start).lower()
                if path in ('/', '/etc') or fnmatch.fnmatchcase('/etc', path):
                    return True
            for i, word in enumerate(args):
                if word in ('-exec', '-execdir'):
                    end = next((j for j in range(i + 1, len(args)) if args[j] == '+'), len(args))
                    if _protected_ancestor_operations(shlex.join(args[i + 1:end]), cwd, depth + 1):
                        return True
        tar_extract = program == 'tar' and any(
            word in ('--extract', '--get') or
            (word.startswith('-') and not word.startswith('--') and 'x' in word[1:]) or
            (i == 0 and not word.startswith('-') and 'x' in word)
            for i, word in enumerate(args))
        if cwd.lower() in ('/', '/etc') and (program in ('cp', 'mv', 'install', 'rsync', 'ln', 'unzip') or tar_extract):
            return True
        if program in ('cp', 'mv', 'install', 'rsync', 'ln'):
            explicit = ''
            for i, word in enumerate(args):
                if word in ('-t', '--target-directory') and i + 1 < len(args):
                    explicit = args[i + 1]
                elif word.startswith('--target-directory='):
                    explicit = word.split('=', 1)[1]
                elif word.startswith('-t') and len(word) > 2:
                    explicit = word[2:]
                elif word.startswith('-') and not word.startswith('--') and 't' in word[1:]:
                    rest = word[word.index('t') + 1:]
                    explicit = rest or (args[i + 1] if i + 1 < len(args) else '')
            operands = [word for word in args if not word.startswith('-')]
            destination = explicit or (operands[-1] if operands else '')
            if destination and protected_destination(destination):
                return True
        if tar_extract or program == 'unzip':
            directory = ''
            for i, word in enumerate(args):
                if word in ('-C', '--directory', '-d') and i + 1 < len(args):
                    directory = args[i + 1]
                elif word.startswith('--directory='):
                    directory = word.split('=', 1)[1]
                elif len(word) > 2 and word[:2] in ('-C', '-d'):
                    directory = word[2:]
            if directory and protected_destination(directory):
                return True
            if not directory and cwd and protected_destination('.'):
                return True
    return False


def command_names_protected_path(command: str) -> bool:
    """Return whether a command's text names a protected policy location by any plain spelling."""
    if _protected_ancestor_operations(str(command or '')):
        return True
    text = re.sub(r"[\'\"\\]", "", str(command or ""))
    text = re.sub(r"/{2,}", "/", text)
    if (any(pattern.search(text) for pattern in PROTECTED_COMMAND_RES)
            or re.search(r"/etc/codex/requirements\.toml(?![\w./-])", text, re.IGNORECASE)):
        return True
    # Match per path component: a shell wildcard cannot cross a slash.
    # Consider the complete command so cd, pushd, env -C and grouped commands
    # cannot hide a relative requirements name. No filesystem expansion occurs.
    target = "/etc/codex/requirements.toml"
    def matches_path(pattern: str, path: str) -> bool:
        expected, parts = path.lower().split('/'), posixpath.normpath(pattern).lower().split('/')
        return len(parts) == len(expected) and all(fnmatch.fnmatchcase(part, pat)
                                                    for pat, part in zip(parts, expected))

    def names_directory(pattern: str) -> bool:
        parts = posixpath.normpath(pattern).lower().split('/')
        expected = '/etc/codex'.split('/')
        return len(parts) >= len(expected) and all(fnmatch.fnmatchcase(part, pat)
                                                    for pat, part in zip(parts, expected))

    words = re.findall(r"[^\s<>;&|(){}]+(?:\{[^{}]*\}[^\s<>;&|(){}]*)?", text)
    expanded = []
    for word in words:
        brace = re.search(r"\{([^{}]+)\}", word)
        if brace:
            expanded.extend(word[:brace.start()] + choice + word[brace.end():]
                            for choice in brace[1].split(','))
        else:
            expanded.append(word)
    # Paths may be embedded in options such as --chdir=/etc/codex or -C/etc/codex.
    if any('/' in word and names_directory(word[word.index('/'):]) for word in expanded):
        return True
    # A find from an ancestor can select the managed requirements file.
    if (re.search(r"\bfind\s+/(?:etc)?(?:\s|/|$)", text, re.IGNORECASE)
            and any(fnmatch.fnmatchcase('requirements.toml', posixpath.basename(word).lower())
                    for word in expanded)):
        return True
    directory = ""
    for part in re.split(r"[;&|(){}\n]+", text):
        words = re.findall(r"[^\s<>]+", part)
        if not words:
            continue
        if words[0] in ("cd", "pushd"):
            paths = [word for word in words[1:] if not word.startswith('-')]
            if paths:
                path = paths[0]
                directory = posixpath.normpath(path if path.startswith("/") else posixpath.join(directory, path))
        for word in words:
            if word.startswith("/"):
                candidate = posixpath.normpath(word)
            elif directory:
                candidate = posixpath.normpath(posixpath.join(directory, word))
            else:
                continue
            if names_directory(candidate) or matches_path(candidate, target):
                return True
    if any(word.startswith('/') and matches_path(word, target) for word in expanded):
        return True
    return False


FILE_APPROVAL = "item/fileChange/requestApproval"


_SERVICE_PATH = re.compile(r"/api/services/([a-z0-9_]+)/([a-z0-9_]+)")

_SERVICE_WORDS = {"turn_on": "turns on", "turn_off": "turns off", "toggle": "toggles"}

_RELOADS = {
    ("automation", "reload"): ("Reload the automations.", "Automations read their configuration again; none is run by this step."),
    ("script", "reload"): ("Reload the scripts.", "Scripts read their configuration again; none is run by this step."),
    ("scene", "reload"): ("Reload the scenes.", "Scenes read their configuration again; none is applied by this step."),
    ("homeassistant", "restart"): ("Restart Home Assistant.", "Home Assistant is unavailable for a minute or two."),
}

_FILE_EFFECTS = (
    (re.compile(r"(^|/)automations\.yaml$"), "Your automations change once automations are reloaded."),
    (re.compile(r"(^|/)scripts\.yaml$"), "Your scripts change once scripts are reloaded."),
    (re.compile(r"(^|/)scenes\.yaml$"), "Your scenes change once scenes are reloaded."),
    (re.compile(r"(^|/)\.storage/lovelace"), "A dashboard changes; open it to see the change."),
    (re.compile(r"(^|/)(configuration\.yaml|packages/.+\.ya?ml)$"),
     "Home Assistant's configuration changes; most parts take effect after a reload or a restart."),
    (re.compile(r"\.ya?ml$"), "A configuration file changes; it takes effect when Home Assistant next reads it."),
)

_KIND_WORDS = {"add": "Create", "update": "Change", "delete": "Delete"}

_ENTITY_ID = re.compile(r"(?<![\w.-])[a-z_][a-z0-9_]*\.[a-z0-9_]+\b")


def _entity_ids(text: str) -> list[str]:
    """Return the entity ids a command names, in order, without repeats."""
    return list(dict.fromkeys(_ENTITY_ID.findall(text)))

def _command_confirmation(command: str, cwd: str, network: str) -> dict[str, str]:
    """Plain words for a command: a Home Assistant action when one is named, else the command itself."""
    lower = (command or "").lower()
    call = _SERVICE_PATH.search(lower)
    if call:
        domain, service = call.groups()
        if (domain, service) in _RELOADS:
            what, effect = _RELOADS[(domain, service)]
            return {"what": what, "where": "This Home Assistant.", "effect": effect}
        targets = [entity for entity in _entity_ids(lower) if entity.split(".")[0] == domain] or _entity_ids(lower)
        where = ", ".join(targets) if targets else f"every {domain} the action reaches"
        verb = _SERVICE_WORDS.get(service, f"runs {domain}.{service} on")
        if domain == "scene" and service == "turn_on":
            verb = "applies"
        return {"what": f"Run the Home Assistant action {domain}.{service}.", "where": (where if targets else where.capitalize()) + ".",
                "effect": f"This {verb} {where}."}
    where = f"In {cwd}" + (f", reaching {network}" if network else "") + "." if cwd else "On this Home Assistant."
    return {"what": "Run one command outside the sandbox, shown below.", "where": where,
            "effect": "Only what that command does; it may read files and use the network."}

def _file_effect(paths: list[str]) -> str:
    """The plain effect of changing these files: the most specific one that applies to each, without repeats."""
    effects: list[str] = []
    for path in paths:
        effect = next((text for pattern, text in _FILE_EFFECTS if pattern.search(path)), "The file changes.")
        if effect not in effects:
            effects.append(effect)
    return " ".join(effects)

def _changes_confirmation(changes: list[dict[str, Any]]) -> dict[str, str]:
    """Plain words for a file change: create, change or delete, which files, and what that does."""
    present = {str(change.get("kind") or "update") for change in changes}
    verbs = " and ".join(word.lower() for kind, word in _KIND_WORDS.items() if kind in present) or "change"
    paths = list(dict.fromkeys(str(change[key]) for change in changes for key in ("path", "move_path") if change.get(key)))
    count = len(changes)
    what = f"{verbs[0].upper() + verbs[1:]} {count} file{'s' if count != 1 else ''}."
    return {"what": what, "where": (", ".join(paths) + ".") if paths else "Not given.", "effect": _file_effect(paths)}


def confirmation(record: dict[str, Any]) -> dict[str, str]:
    """Describe the redacted action without making or suppressing its approval decision."""
    text = (_command_confirmation(record["command"], record["cwd"], record.get("network", ""))
            if record["kind"] == "command" else _changes_confirmation(record["changes"]))
    if not record["can_allow"]:
        text["effect"] = "This request cannot be allowed here. " + record["note"]
    return text


def require_supported_policy(params: dict[str, Any]) -> None:
    """Integration-owned requests must refuse full access instead of relying on Codex's fallback."""
    config = params.get("config") or {}
    policy = params.get("sandboxPolicy") or {}
    modes = [params.get("sandbox"), config.get("sandbox_mode") if isinstance(config, dict) else None,
             policy.get("type") if isinstance(policy, dict) else policy]
    if any(mode in ("danger-full-access", "dangerFullAccess") for mode in modes):
        raise ValueError("Full-access requests are refused by this integration; use workspace-write or read-only.")
    if params.get("approvalPolicy") == "never" or (isinstance(config, dict) and config.get("approval_policy") == "never"):
        raise ValueError("The never approval policy is refused by this integration; use on-request.")


class AppServerSession:
    """Run one app-server turn and correlate each user decision with its request.

    All state and writes use the worker's reentrant lock, including cancellation
    and the HTTP decision route. A browser cannot supply protocol request IDs,
    commands, permission scopes, or policy amendments.
    """

    def __init__(
        self, writer: TextIO, *, lock: Any, thread_params: dict[str, Any],
        turn_params: dict[str, Any], on_event: Callable[[dict[str, Any]], None],
        on_approvals: Callable[[list[dict[str, Any]]], None],
        on_final: Callable[[str], None], is_active: Callable[[], bool],
        redact: Callable[[str], str],
    ) -> None:
        """Bind a live transport to one worker exchange and its persistence callbacks."""
        self.writer = writer
        self.lock = lock
        self.thread_params = copy.deepcopy(thread_params)
        self.turn_params = copy.deepcopy(turn_params)
        self.on_event = on_event
        self.on_approvals = on_approvals
        self.on_final = on_final
        self.is_active = is_active
        self.redact = redact
        self.finished = threading.Event()
        # When Codex last sent anything, or the user last answered; the worker's stall watchdog reads it.
        self.last_activity = time.monotonic()
        self.error = ""
        self.returncode = 1
        self.thread_id = ""
        self.turn_id = ""
        self._closed = False
        self._turn_ended = False
        self._next_id = 0
        self._requests: dict[str, str] = {}
        self._pending: dict[str, Any] = {}
        self._pending_files: dict[str, str] = {}
        self._records: list[dict[str, Any]] = []
        self._file_changes: dict[str, list[dict[str, Any]]] = {}
        self._final_text = ""

    def _write(self, message: dict[str, Any]) -> None:
        """Write one complete JSON-RPC message while the shared lock is held."""
        self.writer.write(json.dumps(message) + "\n")
        self.writer.flush()

    def _request(self, method: str, params: dict[str, Any]) -> None:
        """Send a client request with a namespace distinct from server request IDs."""
        if method in {"thread/start", "thread/resume", "turn/start"}:
            require_supported_policy(params)
        self._next_id += 1
        request_id = f"worker-{self._next_id}"
        self._requests[request_id] = method
        self._write({"id": request_id, "method": method, "params": params})

    def start(self) -> None:
        """Initialize the protocol; thread and turn creation follow their replies."""
        with self.lock:
            if self._closed or not self.is_active():
                self.close()
                return
            require_supported_policy(self.thread_params)
            require_supported_policy(self.turn_params)
            self._request("initialize", {"clientInfo": {
                "name": "home_assistant_worker", "title": "Home Assistant", "version": "1",
            }})

    def read(self, handle: TextIO) -> None:
        """Read the JSONL transport until completion or EOF, failing closed on errors."""
        try:
            for line in iter(handle.readline, ""):
                self.receive(json.loads(line))
                if self.finished.is_set():
                    break
            if not self.finished.is_set():
                self.fail("Codex closed the permission connection before the exchange finished.")
        except Exception as exc:
            self.fail(f"Codex permission connection failed: {exc}")

    def fail(self, message: str) -> None:
        """Record a redacted transport error and invalidate every outstanding request."""
        with self.lock:
            self.error = self.redact(message)
            try:
                self.close()
            except OSError:
                # A failed audit write must not keep the transport alive.
                pass

    def close(self) -> None:
        """Expire all live requests and close stdin; saved records cannot grant access."""
        with self.lock:
            if self._closed:
                return
            self._closed = True
            if self.thread_id and self.turn_id and not self._turn_ended:
                try:
                    self._write({"id": "worker-stop", "method": "turn/interrupt", "params": {
                        "threadId": self.thread_id, "turnId": self.turn_id,
                    }})
                except (OSError, ValueError):
                    pass
            self._pending.clear()
            self._pending_files.clear()
            for record in self._records:
                if record["status"] == "pending":
                    record["status"] = "expired"
            try:
                if self._records:
                    self.on_approvals(copy.deepcopy(self._records))
            finally:
                self.finished.set()
                try:
                    self.writer.close()
                except (OSError, ValueError):
                    pass

    def _display(self, value: Any) -> tuple[str, bool]:
        """Redact display text and signal when review would hide part of an action."""
        raw = str(value or "")
        text = self.redact(raw)
        return text[:DISPLAY_LIMIT], text != raw or len(text) > DISPLAY_LIMIT

    def _publish_approval(self, request_id: Any, method: str, params: dict[str, Any]) -> None:
        """Expose only a request for the active thread and turn, with an opaque UI ID."""
        if params.get("threadId") != self.thread_id or params.get("turnId") != self.turn_id:
            raise ValueError("permission request does not belong to the active exchange")
        if len(self._records) >= REQUEST_LIMIT:
            self._write({"id": request_id, "result": {"decision": "decline"}})
            self.on_event({"type": "error", "message": "Too many permission requests in this exchange; request denied."})
            return
        if request_id in self._pending.values():
            raise ValueError("duplicate live permission request")
        reason, _ = self._display(params.get("reason"))
        clipped = False
        # Anything but a new command, such as input typed into a running one, is shown but never allowed.
        request_kind = params.get("kind", "command") if method == COMMAND_APPROVAL else ""
        record: dict[str, Any] = {
            "approval_id": uuid.uuid4().hex, "status": "pending", "reason": reason,
            "kind": "command" if method == COMMAND_APPROVAL else "file_change",
            # The counter's kind: a new command, input to a running one, another command request, or a file change.
            "prompt_kind": ("file_change" if method != COMMAND_APPROVAL else "command" if request_kind == "command"
                            else "command_input" if request_kind == "writeStdin" else "other"),
            "can_allow": True, "command": "", "cwd": "", "changes": [],
        }
        if method == COMMAND_APPROVAL:
            record["command"], truncated = self._display(params.get("command"))
            record["cwd"], folder_truncated = self._display(params.get("cwd"))
            clipped |= truncated or folder_truncated
            record["scope"] = (
                "Allow this command once outside the configured sandbox. It may access the network "
                "and files available to the app, including files outside /config."
            )
            if request_kind == "writeStdin":
                record["scope"] = "Send this input to a command that is already running outside the configured sandbox."
            record["can_allow"] = bool(record["command"] and record["cwd"]) and request_kind == "command"
            network = params.get("networkApprovalContext")
            if isinstance(network, dict):
                record["network"], _ = self._display(network.get("host"))
        else:
            record["changes"] = copy.deepcopy(self._file_changes.get(str(params.get("itemId")), []))
            record["scope"] = "Allow these file changes once, including any listed paths outside /config."
            record["can_allow"] = bool(record["changes"]) and all(
                change.get("path") and change.get("diff") and change.get("kind") in {"add", "update", "delete"}
                for change in record["changes"])
            clipped |= any(change.get("truncated") or change.get("redacted") for change in record["changes"])
        if method == COMMAND_APPROVAL:
            # A command allowed once runs outside the sandbox, so it is held to the same protected places.
            protected = command_names_protected_path(params.get("command") or "")
        else:
            protected = any(
                touches_protected_path(change.get("path") or "") or touches_protected_path(change.get("move_path") or "")
                for change in record["changes"])
        if clipped or protected:
            record["can_allow"] = False
        if protected:
            record["note"] = "This request touches the app's permission policy or settings, which Codex may not edit. Deny it."
        elif request_kind == "writeStdin":
            record["note"] = "Input to a running command cannot be allowed here. Deny it and ask for the whole action as one command."
        elif request_kind not in ("", "command"):
            record["note"] = "This kind of request cannot be allowed here. Deny it and ask for a single command or file change."
        elif not record["can_allow"]:
            record["note"] = "The complete action is missing, too large, or redacted. Deny it and request an action without hidden details."
        else:
            record["note"] = "Known credentials are hidden. This does not change your sandbox setting or approve future requests."
        record["confirmation"] = confirmation(record)
        self._records.append(record)
        self._pending[record["approval_id"]] = request_id
        if method == FILE_APPROVAL:
            self._pending_files[record["approval_id"]] = str(params.get("itemId"))
        # Persist the reviewable request before it can receive a decision.
        self.on_approvals(copy.deepcopy(self._records))

    def waiting_for_user(self) -> bool:
        """Return True while a permission request waits for the user, when silence from Codex is expected."""
        with self.lock:
            return bool(self._pending) and not self._closed

    def decide(self, approval_id: str, decision: str, decided_by: dict[str, str] | None = None) -> None:
        """Apply exactly one allow/deny response to a still-live request.

        Persistence precedes the write to Codex. If either fails, the connection
        is closed so a retry cannot accidentally authorize the action twice.
        """
        if decision not in {"allow", "deny"}:
            raise ValueError("decision must be allow or deny")
        with self.lock:
            if self._closed or not self.is_active() or approval_id not in self._pending:
                raise LookupError(APPROVAL_GONE)
            record = next(item for item in self._records if item["approval_id"] == approval_id)
            if decision == "allow" and not record["can_allow"]:
                # The note already says why this request can only be denied.
                raise ValueError(record["note"])
            request_id = self._pending.pop(approval_id)
            self._pending_files.pop(approval_id, None)
            # The stall clock starts again from the answer, not from when the request arrived.
            self.last_activity = time.monotonic()
            record["status"] = "allowed" if decision == "allow" else "denied"
            if decided_by:
                record["decided_by"] = dict(decided_by)
            try:
                self.on_approvals(copy.deepcopy(self._records))
                self._write({"id": request_id, "result": {"decision": "accept" if decision == "allow" else "decline"}})
            except (OSError, ValueError):
                record["status"] = "expired"
                self.fail("Could not deliver the permission decision. The exchange was stopped.")
                raise OSError(self.error) from None

    def receive(self, message: dict[str, Any]) -> None:
        """Dispatch a server message without treating model prose as permission."""
        with self.lock:
            self.last_activity = time.monotonic()
            if self._closed:
                return
            if not isinstance(message, dict):
                raise ValueError("invalid app-server message")
            method = message.get("method")
            params = message.get("params") or {}
            if "id" in message and method:
                if not self.is_active():
                    self.close()
                elif method in {COMMAND_APPROVAL, FILE_APPROVAL}:
                    self._publish_approval(message["id"], method, params)
                elif method == "item/permissions/requestApproval":
                    # Broad turn/session grants are deliberately unsupported.
                    self._write({"id": message["id"], "result": {"permissions": {}, "scope": "turn"}})
                    self.on_event({"type": "error", "message": "Broad permission grant denied. Request a single command or file change instead."})
                elif method == "mcpServer/elicitation/request":
                    self._write({"id": message["id"], "result": {"action": "decline"}})
                else:
                    self._write({"id": message["id"], "error": {"code": -32601, "message": "Unsupported worker request"}})
                return
            if "id" in message:
                self._reply(message)
                return
            if method == "serverRequest/resolved":
                changed = False
                for approval_id, request_id in list(self._pending.items()):
                    if request_id == params.get("requestId") and params.get("threadId") == self.thread_id:
                        self._pending.pop(approval_id)
                        self._pending_files.pop(approval_id, None)
                        next(item for item in self._records if item["approval_id"] == approval_id)["status"] = "expired"
                        changed = True
                if changed:
                    self.on_approvals(copy.deepcopy(self._records))
            elif method == "turn/started":
                if params.get("threadId") == self.thread_id:
                    self.turn_id = params["turn"]["id"]
                    self.on_event({"type": "turn.started"})
            elif params.get("threadId") == self.thread_id and params.get("turnId") == self.turn_id and method in {"item/started", "item/completed"}:
                self._item(method, params["item"])
            elif method == "item/fileChange/patchUpdated" and params.get("threadId") == self.thread_id and params.get("turnId") == self.turn_id:
                self._cache_file_changes(str(params["itemId"]), params.get("changes") or [])
            elif method == "turn/completed" and params.get("threadId") == self.thread_id:
                turn = params["turn"]
                if turn.get("id") != self.turn_id:
                    raise ValueError("completion does not belong to the active exchange")
                self._turn_ended = True
                if turn.get("status") == "completed" and self._final_text:
                    self.on_final(self._final_text)
                    self.returncode = 0
                else:
                    self.error = self.redact(str((turn.get("error") or {}).get("message") or "Codex finished without a final answer."))
                self.close()

    def _reply(self, message: dict[str, Any]) -> None:
        """Advance initialization only after the preceding request succeeded."""
        method = self._requests.pop(message["id"], None)
        if method is None:
            return
        if message.get("error"):
            raise ValueError(f"{method}: {message['error'].get('message', 'request failed')}")
        result = message.get("result") or {}
        if method == "initialize":
            self._write({"method": "initialized"})
            self._request("thread/resume" if self.thread_params.get("threadId") else "thread/start", self.thread_params)
        elif method in {"thread/start", "thread/resume"}:
            if result.get("approvalPolicy") != "on-request" or result.get("approvalsReviewer") != "user":
                raise ValueError("Codex did not enable user approval requests")
            require_supported_policy(result)
            self.thread_id = result["thread"]["id"]
            expected = self.thread_params.get("threadId")
            if expected and self.thread_id != expected:
                raise ValueError("Codex resumed a different conversation")
            self.on_event({"type": "thread.started", "thread_id": self.thread_id})
            self._request("turn/start", {**self.turn_params, "threadId": self.thread_id})
        elif method == "turn/start":
            self.turn_id = result["turn"]["id"]

    def _cache_file_changes(self, item_id: str, raw_changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Bound and redact complete file actions, refusing to replace a pending review."""
        changes = []
        remaining = DISPLAY_LIMIT
        for change in raw_changes[:REQUEST_LIMIT]:
            kind = change.get("kind") or {}
            entry: dict[str, Any] = {"kind": str(kind.get("type") or "unknown"), "truncated": False, "redacted": False}
            for key, raw in (("path", change.get("path")), ("move_path", kind.get("move_path")), ("diff", change.get("diff"))):
                raw = str(raw or "")
                text = self.redact(raw)
                entry["redacted"] |= text != raw
                entry["truncated"] |= len(text) > max(remaining, 0)
                entry[key] = text[:max(remaining, 0)]
                remaining -= len(text)
            changes.append(entry)
        if len(raw_changes) > REQUEST_LIMIT and changes:
            changes[-1]["truncated"] = True
        if item_id in self._pending_files.values() and self._file_changes.get(item_id) != changes:
            raise ValueError("File changes were updated while permission was pending. The exchange was stopped.")
        self._file_changes[item_id] = changes
        return changes

    def _item(self, method: str, item: dict[str, Any]) -> None:
        """Translate app-server items into the worker's existing activity format."""
        kind = item.get("type")
        mapped = {"agentMessage": "agent_message", "commandExecution": "command_execution",
                  "fileChange": "file_change", "mcpToolCall": "mcp_tool_call", "webSearch": "web_search"}
        if kind not in mapped and kind != "reasoning":
            # Images are collected from the rollout after completion. Do not
            # duplicate image bytes or user input in the activity log.
            return
        event_item = {**item, "type": mapped.get(kind, kind)}
        if item.get("status") == "inProgress":
            event_item["status"] = "in_progress"
        if kind == "commandExecution":
            event_item.update(aggregated_output=item.get("aggregatedOutput"), exit_code=item.get("exitCode"))
        elif kind == "fileChange":
            changes = self._cache_file_changes(str(item["id"]), item.get("changes") or [])
            if method == "item/completed":
                self._file_changes.pop(str(item["id"]), None)
            event_item["changes"] = [{"path": c["path"], "kind": c["kind"], "move_path": c["move_path"]} for c in changes]
        elif kind == "reasoning":
            event_item = {"id": item["id"], "type": "reasoning", "text": "\n".join(item.get("summary") or [])}
        elif kind == "agentMessage" and method == "item/completed":
            if item.get("phase") != "commentary":
                self._final_text = str(item.get("text") or "")
        self.on_event({"type": method.replace("/", "."), "item": event_item})
