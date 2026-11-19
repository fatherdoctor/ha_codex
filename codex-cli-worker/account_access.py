"""Passive sign-in checks; plan metadata never establishes Codex entitlement."""
from datetime import datetime, timezone
import re
import threading
import time

CHECK_INTERVAL_SECONDS = 86400
# rust-v0.166.0 protocol/src/account.rs; do not retain arbitrary account text.
PLAN_TYPES = frozenset(('free', 'go', 'plus', 'pro', 'prolite', 'promax', 'team', 'business',
                       'self_serve_business_prolite', 'self_serve_business_usage_based', 'ent26',
                       'enterprise_cbp_automation', 'enterprise_cbp_usage_based', 'enterprise',
                       'edu', 'edu_plus', 'edu_pro', 'sci', 'sci_plus', 'unknown'))
# login/src/auth/manager.rs classifies these refresh failures. HTTP status alone is ambiguous.
PINNED_REFRESH_MESSAGES = tuple(
    'Your access token could not be refreshed because your refresh token ' + reason
    + '. Please log out and sign in again.'
    for reason in ('has expired', 'was already used', 'was revoked')
)
EXPIRED_SIGN_IN = re.compile(r'\brefresh_token_(?:expired|reused|invalidated)\b', re.IGNORECASE)
REVOKED_SIGN_IN = re.compile(r'\btoken_revoked\b|invalidated oauth token', re.IGNORECASE)


def rejected_sign_in_error(error):
    """Recognize explicit Codex sign-in failures; an HTTP status alone is ambiguous."""
    text = str(error)
    return bool(EXPIRED_SIGN_IN.search(text) or REVOKED_SIGN_IN.search(text)
                or any(message in text for message in PINNED_REFRESH_MESSAGES))


class AccountAccess:
    """Bound metadata to the current sign-in without storing tokens, emails or raw errors."""
    def __init__(self, login, read, signature, record, busy=lambda: False, clock=time.monotonic):
        self.login, self.read, self.signature, self.record = login, read, signature, record
        self.busy, self.clock = busy, clock
        self.lock = threading.Lock()
        self.refreshing = False
        self.last_attempt = None
        self.last_signature = None
        self.plan_type = None
        self.checked_at = None
        self.confirmed_signature = None

    def refresh(self, force=False):
        """Read passive metadata at most daily, retrying a busy deferral on the next tick."""
        if self.busy():
            return False
        signature, now = self.signature(), self.clock()
        with self.lock:
            if self.refreshing:
                return False
            if (not force and self.last_attempt is not None and signature == self.last_signature
                    and now - self.last_attempt < CHECK_INTERVAL_SECONDS):
                return False
            self.refreshing = True
        plan = None
        try:
            login = self.login()
            if login.get('status_ok'):
                response = self.read()
                limits = response.get('rateLimits') if isinstance(response, dict) else None
                candidate = limits.get('planType') if isinstance(limits, dict) else None
                plan = candidate if isinstance(candidate, str) and candidate in PLAN_TYPES else None
        except Exception as error:
            # Never retain or publish the error: it may contain account details.
            if rejected_sign_in_error(error):
                self.record('rejected', signature)
        finally:
            with self.lock:
                if signature == self.signature():
                    self.last_signature = signature
                    self.last_attempt = now
                    self.plan_type = plan
                    self.checked_at = datetime.now(timezone.utc).isoformat()
                self.refreshing = False
        return True

    def observe_metadata(self, response, signature):
        """Reuse the existing passive usage read after sign-in; keep only the plan enum."""
        limits = response.get('rateLimits') if isinstance(response, dict) else None
        candidate = limits.get('planType') if isinstance(limits, dict) else None
        plan = candidate if isinstance(candidate, str) and candidate in PLAN_TYPES else None
        with self.lock:
            if signature is not None and signature == self.signature():
                self.last_signature = signature
                self.last_attempt = self.clock()
                self.plan_type = plan
                self.checked_at = datetime.now(timezone.utc).isoformat()

    def successful_turn(self, signature):
        """A real successful turn proves access for that same sign-in, not a later account."""
        if signature is not None and signature == self.signature() and self.record('ok', signature):
            with self.lock:
                self.confirmed_signature = signature

    def snapshot(self, login):
        """Expose a fixed message and optional plan label; no entitlement inference from plans."""
        signature = self.signature()
        with self.lock:
            current = signature is not None and signature == self.last_signature
            plan = self.plan_type if current else None
            checked = self.checked_at if current else None
            confirmed = signature is not None and signature == self.confirmed_signature
        if login.get('server_check') == 'rejected':
            state, needs = 'expired', True
            message = 'Your ChatGPT sign-in expired or was refused. Sign in again from Settings.'
        elif not login.get('status_ok') and login.get('has_auth_file') is False:
            state, needs = 'signed_out', True
            message = 'Sign in with a ChatGPT account that has Codex access.'
        elif login.get('status_ok') and confirmed:
            state, needs = 'confirmed_by_turn', False
            message = 'A successful turn confirmed Codex access with the current sign-in.'
        else:
            state = 'unconfirmed'
            needs = False if login.get('status_ok') else None
            message = 'Codex access cannot be confirmed by a passive read. Plan type is information only.'
        return {'state': state, 'needs_sign_in': needs, 'plan_type': plan,
                'checked_at': checked, 'message': message}
