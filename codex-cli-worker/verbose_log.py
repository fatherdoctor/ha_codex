"""Private, opt-in FIFO of worker-visible text; never an anonymized export source."""
from collections import deque
from datetime import datetime, timezone
from itertools import chain
from collections.abc import Iterable
import io
import json
import logging
import os
import re
from pathlib import Path
import stat
import tempfile
import threading

MAX_BYTES = 2 * 1024 * 1024
DEFAULTS = {'enabled': False, 'lines': 500}
TRUNCATED = ' [line truncated]\n'


def atomic(path, raw):
    """Replace a private file atomically and sync the new contents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.verbose-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def private_read(path, maximum, tail=False):
    """Reject links and special files before reading; never block on a planted FIFO."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError('Not a regular file')
        if info.st_size > maximum:
            if not tail:
                raise ValueError('File exceeds its limit')
            stream.seek(-maximum, os.SEEK_END)
            stream.readline()  # Do not interpret a partial first line as a complete record.
        return stream.read(maximum + 1)


class VerboseLog:
    """Bounded private text storage with explicit owner controls and own-secret masking."""
    def __init__(self, path, secrets=lambda: (), max_bytes=MAX_BYTES):
        self.path = Path(path)
        self.settings_path = self.path.with_suffix('.settings.json')
        self.secrets = secrets
        self.max_bytes = max_bytes
        self.lock = threading.RLock()
        self.known = set()
        self.settings = dict(DEFAULTS)
        self.loaded = False
        self.available = True
        self.epoch = 0
        self.local = threading.local()
        self.rows = deque()
        self.bytes = 0
        self.settings_note = ''
        self.pending_note = False

    def _load(self):
        if self.loaded:
            return
        try:
            value = json.loads(private_read(self.settings_path, 1024))
            self.validate(value)
            self.settings = value
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            self.settings = dict(DEFAULTS)
            self.settings_note = 'Verbose log settings were unreadable; defaults restored.'
            self.pending_note = True
        self.rows.clear()
        self.bytes = 0
        try:
            raw = private_read(self.path, self.max_bytes, tail=True)
        except FileNotFoundError:
            raw = b''
        # Reuse capture's physical newline boundaries; Unicode separators are content.
        for line in io.StringIO(raw.decode('utf-8', errors='replace'), newline=None):
            self._add(line.rstrip('\r\n') + '\n')
        # Re-establish caps and 0600 mode before accepting any new record after restart.
        if raw:
            self._save()
        if self.path.parent.exists():
            for leftover in self.path.parent.glob('.verbose-*.tmp'):
                leftover.unlink(missing_ok=True)
        self.loaded = True

    @staticmethod
    def validate(value):
        if (not isinstance(value, dict) or set(value) != set(DEFAULTS)
                or type(value['enabled']) is not bool or type(value['lines']) is not int
                or not 100 <= value['lines'] <= 2000):
            raise ValueError('Choose on/off and 100 to 2000 lines.')

    def _add(self, line):
        raw = line.encode('utf-8', errors='backslashreplace')
        if len(raw) > self.max_bytes:
            room = self.max_bytes - len(TRUNCATED.encode())
            raw = raw[:max(0, room)].decode('utf-8', errors='ignore').encode() + TRUNCATED.encode()
        self.rows.append(raw)
        self.bytes += len(raw)
        while len(self.rows) > self.settings['lines'] or self.bytes > self.max_bytes:
            self.bytes -= len(self.rows.popleft())
        return raw

    def _append(self, raw, previous_bytes):
        # Appends are already masked and bounded; a partial write is safe to read after restart.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size != previous_bytes:
                raise OSError('Verbose log changed outside its writer')
            os.fchmod(fd, 0o600)
            view = memoryview(raw)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise OSError('Verbose append did not complete')
                view = view[count:]
        finally:
            os.close(fd)

    def _compact(self):
        # Leave half the capacity free: repeated single-line captures remain linear writes.
        newest = len(self.rows[-1]) if self.rows else 0
        while len(self.rows) > 1 and (len(self.rows) > self.settings['lines'] // 2
                                     or self.bytes > max(self.max_bytes // 2, newest)):
            self.bytes -= len(self.rows.popleft())
        self._save()

    def _save(self):
        # Both the old destination and the new temporary file remain within the byte cap.
        atomic(self.path, b''.join(self.rows))

    def remember(self, values: Iterable[object]) -> None:
        """Retain exact own-secret values for masking, including credentials rotated away."""
        with self.lock:
            values = [v.strip() for v in values if isinstance(v, str) and v.strip()]
            if any('\n' in value or '\r' in value for value in values):
                raise ValueError('Own credential has an invalid line break')
            if any(len(json.dumps(v).encode()) > 1024 * 1024 for v in values):
                raise ValueError('Secret exceeds the masking boundary')
            self.known.update(values)

    def _mask(self, text, values):
        spellings = set()
        for value in values:
            spellings.add(value)
            for ascii_only in (True, False):
                spellings.add(json.dumps(value, ensure_ascii=ascii_only)[1:-1])
        for value in sorted(spellings, key=len, reverse=True):
            text = text.replace(value, '[redacted]')
        return text

    def capture(self, source: str, text: str, *, epoch: int | None = None) -> None:
        """Observe text without changing execution; an optional stream epoch rejects stale lines."""
        if getattr(self.local, 'active', False):
            return
        self.local.active = True
        try:
            self._capture(source, text, epoch)
        finally:
            self.local.active = False

    def _capture(self, source, text, observed_epoch=None):
        """An observation failure never changes execution or turns the owner's toggle off."""
        try:
            with self.lock:
                self._load()
                if not self.settings['enabled'] or (observed_epoch is not None and observed_epoch != self.epoch):
                    return
                epoch = self.epoch
            # Never acquire worker/capability locks while holding the storage lock.
            try:
                values = tuple(self.secrets())
                self.remember(values)
            except Exception:
                # Credential lookup did not change storage; do not reload/rewrite it.
                with self.lock:
                    self.available = False
                return
            with self.lock:
                # Clear and off/on cycles invalidate observations already in flight.
                if not self.settings['enabled'] or self.epoch != epoch:
                    return
                stamp = datetime.now(timezone.utc).isoformat()
                prefix = f'[{stamp}] {source}: '
                lines = iter(io.StringIO(str(text), newline=None))
                first = next(lines, None)
                second = next(lines, None)
                if second is not None:
                    # One bulk capture may contain more than the cap; write only its bounded tail.
                    for line in chain((first, second), lines):
                        self._add(prefix + self._mask(line.rstrip('\r\n'), self.known) + '\n')
                    self._save()
                elif first is not None:
                    before_bytes, before_lines = self.bytes, len(self.rows)
                    raw = self._add(prefix + self._mask(first.rstrip('\r\n'), self.known) + '\n')
                    if before_bytes + len(raw) <= self.max_bytes and before_lines < self.settings['lines']:
                        self._append(raw, before_bytes)
                    else:
                        self._compact()
                self.available = True
        except Exception:
            self.available = False
            self.loaded = False

    def status(self) -> dict:
        """Return nonsecret settings, retained sizes and availability."""
        with self.lock:
            try:
                self._load()
            except Exception:
                self.available = False
            result = {**self.settings, 'byte_cap': self.max_bytes, 'stored_lines': len(self.rows),
                      'stored_bytes': self.bytes, 'available': self.available, 'note': self.settings_note}
            note = self.settings_note if self.pending_note else ''
            self.pending_note = False
        # Root handlers can themselves capture text: never acquire their locks under the store lock.
        if note:
            logging.getLogger(__name__).warning(note)
        return result

    def configure(self, value: dict) -> dict:
        """Persist an explicit owner choice, trimming before publishing a smaller cap."""
        self.validate(value)
        with self.lock:
            self._load()
            # Shrink the log before publishing a lower limit, so even a crash
            # between the two atomic replacements cannot leave an over-limit file.
            while len(self.rows) > value['lines']:
                self.bytes -= len(self.rows.popleft())
            if self.rows:
                self._save()
            atomic(self.settings_path, json.dumps(value).encode())
            if self.settings['enabled'] != value['enabled']:
                self.epoch += 1
            self.settings = dict(value)
            self.settings_note = ''
            self.pending_note = False
            return self.status()

    def export(self) -> bytes:
        """Refresh own secrets and return the bounded masked log for a confirmed export."""
        # Refresh own secrets before exporting, including credentials rotated since capture.
        values = tuple(self.secrets())
        self.remember(values)
        with self.lock:
            self._load()
            lines = [self._mask(raw.decode(), self.known) for raw in self.rows]
            self.rows.clear()
            self.bytes = 0
            for line in lines:
                self._add(line)
            self._save()
            return b''.join(self.rows)

    def clear(self) -> None:
        """Remove retained text and invalidate in-flight observations without disabling capture."""
        with self.lock:
            try:
                self._load()
            except (OSError, ValueError):
                self.settings = dict(DEFAULTS)
                self.loaded = True
                self.settings_note = 'Verbose log state was unreadable; defaults restored.'
            self.path.unlink(missing_ok=True)
            self.epoch += 1
            self.rows.clear()
            self.bytes = 0
            self.available = True


class Tee:
    """Observe complete physical lines without publishing an unfinished secret fragment."""
    def __init__(self, original, store, source):
        self.original, self.store, self.source = original, store, source
        self.pending = ''
        self.truncated = False
        self.epoch = store.epoch
        self.lock = threading.RLock()

    def write(self, value):
        result = self.original.write(value)
        enabled = self.store.status()['enabled']
        ready = []
        with self.lock:
            if not enabled or self.epoch != self.store.epoch:
                self.pending = ''
                self.truncated = False
                self.epoch = self.store.epoch
            if not enabled:
                return result
            for part in value.splitlines(keepends=True):
                # Retain lookahead so masking cannot leave half a secret at truncation.
                room = self.store.max_bytes + 1024 * 1024 - len(self.pending)
                if room > 0:
                    self.pending += part[:room]
                if len(part) > room:
                    self.truncated = True
                if part.endswith(('\n', '\r')):
                    ready.append((self.epoch, self.pending.rstrip('\r\n') + (TRUNCATED if self.truncated else '\n')))
                    self.pending = ''
                    self.truncated = False
        # Secret snapshots and disk I/O must not hold a stream lock.
        for epoch, line in ready:
            self.observe(line, epoch)
        return result

    def finish(self):
        with self.lock:
            line = self.pending + (TRUNCATED if self.truncated else '\n') if self.pending else ''
            epoch = self.epoch
            self.pending = ''
            self.truncated = False
        if line:
            self.observe(line, epoch)

    def observe(self, line, epoch):
        # Keep the original stream untouched. Only the optional private copy drops polls.
        if self.source != 'stderr' or not poll_stderr(line):
            self.store.capture(self.source, line, epoch=epoch)

    def flush(self):
        self.original.flush()

    def __getattr__(self, name):
        return getattr(self.original, name)


class Handler(logging.Handler):
    """Optional record observer for callers that do not also capture their stderr."""
    def __init__(self, store):
        super().__init__()
        self.store = store
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

    def emit(self, record):
        try:
            if poll_access(record):
                return
            self.store.capture('logging', self.format(record))
        except Exception:
            pass


POLL_PATH = re.compile(r'/(?:status|auth/status|remote-control|queue|tasks(?:/[a-zA-Z0-9_-]+(?:/(?:activity|log))?)?)$')
ACCESS_TEMPLATE = re.compile(r'^\S+ - - \[[^\]\r\n]+\] "%s" %s %s\n?$')
REQUEST_LINE = re.compile(r'(?:GET|HEAD) ([^ "\t]+) HTTP/[0-9.]+')


def poll_access(record: logging.LogRecord) -> bool:
    """Exclude only successful werkzeug access records for known read-only polling routes."""
    if record.name != 'werkzeug' or record.levelno >= logging.WARNING:
        return False
    # Match Werkzeug's access template and arguments, not arbitrary messages mentioning a request.
    if (not isinstance(record.msg, str) or not ACCESS_TEMPLATE.fullmatch(record.msg)
            or not isinstance(record.args, tuple) or len(record.args) != 3
            or not isinstance(record.args[0], str)):
        return False
    request_line = re.sub(r'\x1b\[[0-9;]*m', '', record.args[0])
    match = REQUEST_LINE.fullmatch(request_line)
    code = str(record.args[1])
    return bool(match and code.isdecimal() and 200 <= int(code) < 400
                and POLL_PATH.fullmatch(match[1].split('?', 1)[0]))


# The default Werkzeug handler writes its rendered access line to stderr. Logging
# prefixes and ordinary prose do not match; warnings/errors and failed requests stay.
STDERR_ACCESS = re.compile(
    r'^\S+ - - \[[^\]\r\n]+\] "(?:GET|HEAD) ([^ "\t]+) HTTP/[0-9.]+" ([0-9]{3}) \S+[ \t]*\n?$'
)


def poll_stderr(line: str) -> bool:
    """Recognize successful polling access lines after the normal handler renders them."""
    plain = re.sub(r'\x1b\[[0-9;]*m', '', line)
    match = STDERR_ACCESS.fullmatch(plain)
    return bool(match and 200 <= int(match[2]) < 400
                and POLL_PATH.fullmatch(match[1].split('?', 1)[0]))


class CredentialCache:
    """Read each own credential file again only when its filesystem identity changes."""
    def __init__(self):
        self.lock = threading.RLock()
        self.entries = {}

    @staticmethod
    def signature(path):
        """Capture identity and change metadata without following a link."""
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode)

    def read(self, path: Path) -> bytes | None:
        """Return a verified snapshot; races or unreadable files fail closed, never use stale data."""
        with self.lock:
            before = self.signature(path)
            if before is None:
                self.entries.pop(path, None)
                return None
            if path in self.entries and self.entries[path][0] == before:
                return self.entries[path][1]
            raw = private_read(path, 1024 * 1024)
            if before != self.signature(path):
                raise OSError('Own credential changed during lookup')
            if path not in self.entries and len(self.entries) >= 3:
                self.entries.pop(next(iter(self.entries)))
            self.entries[path] = (before, raw)
            return raw
