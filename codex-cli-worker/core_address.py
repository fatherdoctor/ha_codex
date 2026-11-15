"""Resolve Core's direct address without exporting Supervisor credentials."""
from __future__ import annotations
import json
import threading
from urllib.parse import urlsplit
import requests

LEGACY_DEFAULTS = frozenset(('http://homeassistant:8123', 'http://supervisor/core', 'http://supervisor/core/api'))
INFO_URL = 'http://supervisor/core/info'


def override_url(value):
    if not isinstance(value, str):
        raise ValueError('The Core address override must be a URL.')
    value = value.strip().rstrip('/')
    if not value or value in LEGACY_DEFAULTS:
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError('The Core address override is not a valid HTTP(S) URL.') from None
    if (parts.scheme not in ('http', 'https') or not parts.hostname or parts.hostname == 'supervisor'
            or parts.username is not None or parts.password is not None or parts.query or parts.fragment
            or (port is not None and not 1 <= port <= 65535)):
        raise ValueError('Use an HTTP(S) Core address without credentials, query or fragment.')
    return value


class ResponseTooLarge(ValueError):
    pass


def get_json(url, token, maximum):
    """Read only an internal fixed URL selected by worker code, with bounded transport."""
    if not token:
        raise ValueError('Supervisor access is unavailable.')
    with requests.Session() as client:
        client.trust_env = False
        with client.get(url, headers={'Authorization': 'Bearer ' + token}, timeout=(3, 5),
                        allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise ValueError('Supervisor read is unavailable.')
            data = bytearray()
            for chunk in response.iter_content(4096):
                data.extend(chunk)
                if len(data) > maximum:
                    raise ResponseTooLarge(f'Response exceeds {maximum} bytes.')
            return json.loads(data)


def fetch_info(token):
    return get_json(INFO_URL, token, 65536)


def probe_core(port):
    """Probe two fixed internal Core endpoints without forwarding any credential."""
    if port not in (8123, 80):
        raise ValueError('Unsupported Core probe port.')
    try:
        with requests.Session() as client:
            client.trust_env = False
            with client.get(f'http://homeassistant:{port}/api/', headers={}, timeout=(1, 2),
                            allow_redirects=False, stream=True) as response:
                if response.status_code not in (200, 401):
                    return False
                data = bytearray()
                for chunk in response.iter_content(4096):
                    data.extend(chunk)
                    if len(data) > 65536:
                        return False
                if response.status_code == 401:
                    return bytes(data).strip() == b'401: Unauthorized'
                value = json.loads(data)
                return isinstance(value, dict) and value.get('message') == 'API running.'
    except Exception:
        return False


class CoreAddress:
    def __init__(self):
        self.lock = threading.RLock()
        self.value = {'url': None, 'source': 'unavailable', 'error': 'Core address has not been discovered.'}

    def refresh(self, options, token, fetch=fetch_info, *, probe=None, retry_wait=None):
        """Retry metadata per launch, then verify fixed fallback endpoints; never guess."""
        discovered = None
        source = 'unavailable'
        for attempt in range(2):
            try:
                data = fetch(token)
                if not isinstance(data, dict) or data.get('result') != 'ok' or not isinstance(data.get('data'), dict):
                    raise ValueError()
                port, ssl = data['data'].get('port'), data['data'].get('ssl')
                if type(port) is not int or not 1 <= port <= 65535 or type(ssl) is not bool:
                    raise ValueError()
                discovered = f'{"https" if ssl else "http"}://homeassistant:{port}'
                source = 'discovery'
                break
            except Exception:
                if attempt == 0 and retry_wait is not None:
                    retry_wait()
        if discovered is None and probe is not None:
            for port in (8123, 80):
                if probe(port):
                    discovered = f'http://homeassistant:{port}'
                    source = 'fallback'
                    break
        try:
            override = override_url(options.get('ha_url') or '')
            value = {'url': override or discovered, 'source': 'override' if override else source,
                     'error': '' if override or discovered else 'Core address discovery is unavailable. Check Supervisor connectivity or set an explicit Core address override.'}
        except ValueError as exc:
            value = {'url': None, 'source': 'unavailable', 'error': str(exc)}
        with self.lock:
            self.value = value
            return dict(value)

    def snapshot(self, options):
        """Honor an edited override immediately; only refresh can invent a new discovery."""
        try:
            override = override_url(options.get('ha_url') or '')
        except ValueError as exc:
            return {'url': None, 'source': 'unavailable', 'error': str(exc)}
        if override:
            return {'url': override, 'source': 'override', 'error': ''}
        with self.lock:
            # An old override is not a discovery result after the owner removes it.
            return dict(self.value) if self.value['source'] != 'override' else {
                'url': None, 'source': 'unavailable', 'error': 'Core discovery will refresh on the next launch.'}
