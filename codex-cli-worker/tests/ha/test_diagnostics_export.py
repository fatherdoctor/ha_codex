"""Both export paths use the same scrubbed schema, through real HA redaction."""
import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
ROOT = Path(__file__).resolve().parents[3]
package = types.ModuleType('interaction_diagnostics_ha_test')
package.__path__ = [str(ROOT / 'custom_components/codex_cli')]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(
    package.__name__ + '.diagnostics',
    ROOT / 'custom_components/codex_cli/diagnostics.py',
)
integration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(integration)
sys.path.insert(0, str(ROOT / 'codex-cli-worker'))
from interaction_diagnostics import RollingDiagnostics
import interaction_diagnostics

async def test_integration_download_matches_worker_schema_and_redacts_again(tmp_path):
    store = RollingDiagnostics(tmp_path / 'interactions.json')
    store.append({
        'source': 'addon_page',
        'start': 1700000000,
        'end': 1700000003,
        'outcome': 'done',
        'model': 'gpt-6.1-sol',
        # Repeated synthetic entity IDs exercise the shared scrubber.
        'entities': ['light.synthetic', 'light.synthetic'],
        # The private filename must reduce to a folder and extension.
        'paths': ['/config/scripts/private_canary.yaml'],
        # The error message stands for private exception prose and must be discarded.
        'events': [{'kind': 'tool', 'name': 'commandExecution', 'result': 'done'}, {'kind': 'error', 'type': 'ValueError', 'message': 'PRIVATE_ERROR_CANARY'}],
    })
    with patch.object(interaction_diagnostics.secrets, 'token_bytes', return_value=b'x' * 32):
        expected = store.export()
    client = SimpleNamespace(interaction_diagnostics=AsyncMock(return_value=expected))
    entry = SimpleNamespace(
        # The integration entry title must not enter the exported metadata.
        title='PRIVATE_PERSON_CANARY',
        # Integration credentials and server labels must not enter the export.
        data={'api_token': 'TOKEN_CANARY', 'base_url': 'HOST_CANARY'},
        # A location option is deliberately absent from the export schema.
        options={'location': 'LOCATION_CANARY'},
        runtime_data=SimpleNamespace(
            client=client,
            # Coordinator prompt and answer prose must be excluded.
            coordinator=SimpleNamespace(data={'prompt': 'PROMPT_CANARY', 'summary': 'ANSWER_CANARY'}),
        ),
    )
    result = await integration.async_get_config_entry_diagnostics(None, entry)
    assert result == expected
    client.interaction_diagnostics.assert_awaited_once()
    assert 'CANARY' not in json.dumps(result)
    assert result['coverage'] == 'remote ChatGPT threads: not recorded'

async def test_integration_scrubs_malicious_worker_and_reports_unavailable():
    raw = {
        # Coverage prose is replaced with the fixed supported coverage statement.
        'coverage': 'PRIVATE_CANARY',
        'interactions': [{
            # An unrecognized worker origin must become unknown.
            'source': 'PRIVATE_CANARY',
            # A planted prompt in untrusted input must be dropped.
            'prompt': 'PROMPT_CANARY',
            # Untrusted worker exception type and text must become a fixed error.
            'events': [{'kind': 'error', 'type': 'PRIVATE_CANARY', 'message': 'SECRET_CANARY'}],
        }],
        # The constructed documentation-range address is untrusted worker data, never a destination.
        'coordinator_data': {'address': '.'.join(map(str, (192, 0, 2, 44)))},
    }
    client = SimpleNamespace(interaction_diagnostics=AsyncMock(return_value=raw))
    entry = SimpleNamespace(runtime_data=SimpleNamespace(client=client))
    result = await integration.async_get_config_entry_diagnostics(None, entry)
    assert 'CANARY' not in json.dumps(result)
    assert 'coordinator_data' not in result
    # An API exception stands for private connection details and must yield a fixed unavailable result.
    client.interaction_diagnostics.side_effect = integration.CodexCliApiError('PRIVATE_ADDRESS_CANARY')
    result = await integration.async_get_config_entry_diagnostics(None, entry)
    assert result['availability'] == 'unavailable'
    assert result['interactions'] == []
    assert 'CANARY' not in json.dumps(result)
    result = await integration.async_get_config_entry_diagnostics(None, SimpleNamespace())
    assert result['availability'] == 'unavailable'
