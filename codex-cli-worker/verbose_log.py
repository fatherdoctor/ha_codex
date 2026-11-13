"""Private, opt-in FIFO of worker-visible text; never an anonymized export source."""
from collections import deque
from datetime import datetime, timezone
import io
import json
import logging
import os
from pathlib import Path
import stat
import tempfile
import threading

MAX_BYTES = 2 * 1024 * 1024
DEFAULTS = {'enabled': False, 'lines': 500}
TRUNCATED = ' [line truncated]\n'


def atomic(path, raw):
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

    def _load(self):
        if self.loaded:
            return
        try:
            value = json.loads(private_read(self.settings_path, 1024))
            self.validate(value)
            self.settings = value
        except FileNotFoundError:
            pass
        self.rows.clear()
        self.bytes = 0
        try:
            raw = private_read(self.path, self.max_bytes, tail=True)
        except FileNotFoundError:
            raw = b''
        for line in raw.decode('utf-8', errors='replace').splitlines():
            self._add(line + '\n')
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

    def _save(self):
        # Both the old destination and the new temporary file remain within the byte cap.
        atomic(self.path, b''.join(self.rows))

    def remember(self, values):
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

    def capture(self, source, text):
        if getattr(self.local, 'active', False):
            return
        self.local.active = True
        try:
            self._capture(source, text)
        finally:
            self.local.active = False

    def _capture(self, source, text):
        """An observation failure never changes execution or turns the owner's toggle off."""
        try:
            with self.lock:
                self._load()
                if not self.settings['enabled']:
                    return
            # Never acquire worker/capability locks while holding the storage lock.
            values = tuple(self.secrets())
            self.remember(values)
            with self.lock:
                if not self.settings['enabled']:
                    return
                stamp = datetime.now(timezone.utc).isoformat()
                prefix = f'[{stamp}] {source}: '
                for line in io.StringIO(str(text), newline=None):
                    self._add(prefix + self._mask(line.rstrip('\r\n'), self.known) + '\n')
                self._save()
                self.available = True
        except Exception:
            self.available = False

    def status(self):
        with self.lock:
            try:
                self._load()
            except Exception:
                self.available = False
            return {**self.settings, 'byte_cap': self.max_bytes, 'stored_lines': len(self.rows),
                    'stored_bytes': self.bytes, 'available': self.available}

    def configure(self, value):
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
            return self.status()

    def export(self):
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

    def clear(self):
        with self.lock:
            self._load()
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
                    ready.append(self.pending.rstrip('\r\n') + (TRUNCATED if self.truncated else '\n'))
                    self.pending = ''
                    self.truncated = False
        # Secret snapshots and disk I/O must not hold a stream lock.
        for line in ready:
            self.store.capture(self.source, line)
        return result

    def finish(self):
        with self.lock:
            line = self.pending + (TRUNCATED if self.truncated else '\n') if self.pending else ''
            self.pending = ''
            self.truncated = False
        if line and self.epoch == self.store.epoch:
            self.store.capture(self.source, line)

    def flush(self):
        self.original.flush()

    def __getattr__(self, name):
        return getattr(self.original, name)


class Handler(logging.Handler):
    def __init__(self, store):
        super().__init__()
        self.store = store
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))

    def emit(self, record):
        try:
            self.store.capture('logging', self.format(record))
        except Exception:
            pass
