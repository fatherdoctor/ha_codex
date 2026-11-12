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
                    raise ValueError('Supervisor response is too large.')
            return json.loads(data)


def fetch_info(token):
    return get_json(INFO_URL, token, 65536)


class CoreAddress:
    def __init__(self):
        self.lock = threading.RLock()
        self.value = {'url': None, 'source': 'unavailable', 'error': 'Core address has not been discovered.'}

    def refresh(self, options, token, fetch=fetch_info):
        """Refresh on launch; an explicit override wins, never a guessed port."""
        discovered = None
        try:
            data = fetch(token)
            if not isinstance(data, dict) or data.get('result') != 'ok' or not isinstance(data.get('data'), dict):
                raise ValueError()
            port, ssl = data['data'].get('port'), data['data'].get('ssl')
            if type(port) is not int or not 1 <= port <= 65535 or type(ssl) is not bool:
                raise ValueError()
            discovered = f'{"https" if ssl else "http"}://homeassistant:{port}'
        except Exception:
            pass
        try:
            override = override_url(options.get('ha_url') or '')
            value = {'url': override or discovered, 'source': 'override' if override else 'discovery' if discovered else 'unavailable',
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
