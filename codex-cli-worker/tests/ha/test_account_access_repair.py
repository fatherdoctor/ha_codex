"""Sign-in Repairs use explicit state; account text and plan labels are not authority."""
from unittest.mock import AsyncMock
from homeassistant.helpers import issue_registry as ir
from test_integration_polling import integration


async def test_real_worker_status_http_failure_does_not_raise_but_expiry_does(hass):
    """Use actual status-triggered usage refresh output, not a hand-written access flag."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from test_account_access import status_from_usage_failure

    client = AsyncMock()
    coordinator = integration.CodexCliCoordinator(hass, client)
    registry = ir.async_get(hass)
    for error, expected in (('401 Unauthorized', False), ('403 Forbidden', False),
                            ('refresh_token_expired', True), ('token_revoked', True),
                            ('network timeout', False)):
        client.status.return_value = status_from_usage_failure(error)
        await coordinator._async_update_data()
        assert (registry.async_get_issue('codex_cli', 'codex_not_signed_in') is not None) is expected


async def test_signedout_and_expired_raise_then_good_signin_clears_without_account_data(hass):
    client = AsyncMock()
    coordinator = integration.CodexCliCoordinator(hass, client)
    registry = ir.async_get(hass)
    for state in ('signed_out', 'expired'):
        client.status.return_value = {
            'codex_login': {'status_ok': False},
            'account_access': {'state': state, 'needs_sign_in': True, 'plan_type': 'free',
                               'email': 'SYNTHETIC_PRIVATE_ACCOUNT'},
        }
        await coordinator._async_update_data()
        issue = registry.async_get_issue('codex_cli', 'codex_not_signed_in')
        assert issue is not None
        assert 'SYNTHETIC_PRIVATE_ACCOUNT' not in str(issue.translation_placeholders)
        client.status.return_value['account_access']['needs_sign_in'] = False
        client.status.return_value['codex_login']['status_ok'] = True
        await coordinator._async_update_data()
        assert registry.async_get_issue('codex_cli', 'codex_not_signed_in') is None


async def test_unknown_access_or_plan_never_creates_a_repair(hass):
    client = AsyncMock()
    coordinator = integration.CodexCliCoordinator(hass, client)
    for plan in ('free', 'go', 'plus', None):
        client.status.return_value = {'codex_login': {'status_ok': False},
                                      'account_access': {'state': 'unconfirmed', 'needs_sign_in': None,
                                                         'plan_type': plan}}
        await coordinator._async_update_data()
        assert ir.async_get(hass).async_get_issue('codex_cli', 'codex_not_signed_in') is None
