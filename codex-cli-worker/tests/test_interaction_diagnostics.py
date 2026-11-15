"""Synthetic golden export and bounded, anonymized rolling storage."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import interaction_diagnostics as diag

class DiagnosticsTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'rolling.json'
        self.store = diag.RollingDiagnostics(self.path)

    def sample(self):
        return {
            'source': 'addon_page',
            'start': 1700000000,
            'end': 1700000010,
            'duration_seconds': 10,
            'outcome': 'done',
            'addon_version': '0.1.81.2',
            'model': 'gpt-6.1-sol',
            # Repeated synthetic entity IDs test stable pseudonyms within one export.
            'entities': ['light.sample', 'light.sample'],
            # The private filename must reduce to a folder and extension.
            'paths': ['/config/scripts/private_canary.yaml'],
            # The error message stands for private exception prose and must be discarded.
            'events': [{'kind': 'tool', 'name': 'commandExecution', 'result': 'done', 'duration_seconds': 1}, {'kind': 'approval', 'decision': 'denied'}, {'kind': 'gate', 'rule': 'credential'}, {'kind': 'error', 'type': 'ValueError', 'message': 'PRIVATE_CANARY'}, {'kind': 'ha_read', 'operation': 'state', 'domain': 'light'}],
        }

    def test_golden_export_drops_all_unstructured_canaries(self):
        record = self.sample()
        record.update(
            # Private prompt prose is not part of diagnostic metadata.
            prompt='PROMPT_CANARY',
            # Private answer prose is not part of diagnostic metadata.
            answer='ANSWER_CANARY',
            # A credential value must never reach either store or export.
            token='TOKEN_CANARY',
            # A person identifier must be excluded from the diagnostic schema.
            person='PERSON_CANARY',
            # A location label must be excluded from diagnostic metadata.
            location='LOCATION_CANARY',
            # A server label must be excluded from diagnostic metadata.
            host='HOST_CANARY',
            # A contact field must be excluded from diagnostic metadata.
            email='EMAIL_CANARY',
            # A telephone field must be excluded from diagnostic metadata.
            phone='PHONE_CANARY',
            # A constructed documentation-range address is data only, never a network destination.
            address='.'.join(map(str, (192, 0, 2, 10))),
        )
        self.store.append(record)
        with patch.object(diag.secrets, 'token_bytes', return_value=b'x' * 32):
            export = self.store.export()
        raw = json.dumps(export)
        for value in ('PROMPT_CANARY', 'ANSWER_CANARY', 'TOKEN_CANARY', 'PERSON_CANARY', 'LOCATION_CANARY', 'HOST_CANARY', 'EMAIL_CANARY', 'PHONE_CANARY', 'PRIVATE_CANARY', 'private_canary', 'light.sample', record['address']):
            self.assertNotIn(value, raw)
            self.assertNotIn(value, self.path.read_text())
        result = export['interactions'][0]
        self.assertEqual(result['source'], 'addon_page')
        self.assertEqual(result['outcome'], 'done')
        self.assertEqual(result['start'], '2023-11-14T22:13:20+00:00')
        self.assertEqual(result['end'], '2023-11-14T22:13:30+00:00')
        self.assertEqual(result['events'][-2]['message'], 'Invalid operation.')
        self.assertEqual(result['paths'], [{'folder': 'scripts', 'extension': '.yaml'}])
        self.assertEqual(result['entities'][0], result['entities'][1])
        self.assertEqual(
            result['events'][-1],
            {'kind': 'ha_read', 'operation': 'state', 'domain': 'light', 'time': None},
        )

    def test_export_rekeys_the_same_entity_between_downloads(self):
        self.store.append(self.sample())
        self.assertNotEqual(
            self.store.export()['interactions'][0]['entities'][0]['id'],
            self.store.export()['interactions'][0]['entities'][0]['id'],
        )

    def test_last_ten_bounded_private_records_survive_restart(self):
        for i in range(29):
            self.store.append({**self.sample(), 'duration_seconds': i})
        rows = diag.RollingDiagnostics(self.path).export()['interactions']
        self.assertEqual([x['duration_seconds'] for x in rows], list(range(4, 29)))
        self.assertLessEqual(self.path.stat().st_size, diag.MAX_BYTES)
        self.assertEqual(self.path.stat().st_mode & 511, 384)

    def test_event_caps_and_untrusted_identifiers_are_allowlisted(self):
        record = self.sample()
        # Unrecognized source, model and release values must reduce to fixed enums.
        record.update(source='SOURCE_CANARY', model='MODEL_CANARY', addon_version='VERSION_CANARY')
        # Unrecognized tool names and results must not escape their allowlists.
        record['events'] = [{'kind': 'tool', 'name': 'TOOL_CANARY', 'result': 'RESULT_CANARY'}] * 1000
        self.store.append(record)
        out = self.store.export()['interactions'][0]
        self.assertEqual(len(out['events']), diag.MAX_EVENTS)
        self.assertNotIn('CANARY', json.dumps(out))
        self.assertEqual(out['events'][0]['name'], 'other')

    def test_export_revalidates_tampered_disk_and_nonfinite_numbers(self):
        self.path.write_text(json.dumps([{
            # A planted prompt in untrusted input must be dropped.
            'prompt': 'PROMPT_CANARY',
            # An unrecognized origin must become the fixed unknown value.
            'source': 'SOURCE_CANARY',
            # Private error-type and message strings must become a fixed error description.
            'events': [{'kind': 'error', 'type': 'PERSON_CANARY', 'message': 'PRIVATE_CANARY'}],
            'start': float('nan'),
        }]))
        out = self.store.export()
        self.assertNotIn('CANARY', json.dumps(out))
        self.assertIsNone(out['interactions'][0]['start'])

    def test_corrupt_oversized_and_symlink_storage_is_not_imported(self):
        self.path.write_text('not JSON')
        self.assertEqual(self.store.export()['interactions'], [])
        self.path.write_text('x' * (diag.MAX_BYTES + 1))
        self.assertEqual(self.store.export()['interactions'], [])
        self.path.unlink()
        other = self.path.with_name('other')
        other.write_text('[]')
        self.path.symlink_to(other)
        self.assertEqual(self.store.export()['interactions'], [])

    def test_append_revalidates_old_disk_rows_before_retaining_them(self):
        self.path.write_text(json.dumps([{
            # A private string in a timestamp field must be discarded.
            'start': 'PRIVATE_CANARY',
            # Old disk rows cannot retain private prompt prose.
            'prompt': 'PRIVATE_CANARY',
            # Old disk rows cannot retain arbitrary exception text.
            'events': [{'kind': 'error', 'message': 'PRIVATE_CANARY'}],
        }]))
        self.store.append(self.sample())
        self.assertNotIn('PRIVATE_CANARY', self.path.read_text())
        self.assertEqual(len(self.store.export()['interactions']), 2)

class RecorderTests(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = diag.RollingDiagnostics(Path(tmp.name) / 'interactions.json')
        self.time = 1700000000.0
        self.recorder = diag.InteractionRecorder(self.store, '0.1.81.2', '0.166.0', clock=lambda: self.time)

    def test_packaged_schema_copies_are_identical(self):
        root = Path(__file__).resolve().parents[2]
        self.assertEqual(
            (root / 'codex-cli-worker/diagnostics_scrub.py').read_bytes(),
            (root / 'custom_components/codex_cli/diagnostics_scrub.py').read_bytes(),
        )

    def test_full_golden_record_has_timing_outcomes_and_no_prose(self):
        # The internal task identifier stands for private thread metadata.
        self.recorder.begin('PRIVATE_TASK_CANARY', 'scheduled_run', 'gpt-6.1-sol')
        self.recorder.activity('PRIVATE_TASK_CANARY', {
            'type': 'item.started',
            # Private tool identifiers and command text must not enter the metadata store.
            'item': {'id': 'PRIVATE_TOOL_CANARY', 'type': 'command_execution', 'command': 'PRIVATE_COMMAND_CANARY'},
        })
        self.time += 3
        self.recorder.activity('PRIVATE_TASK_CANARY', {
            'type': 'item.completed',
            'item': {
                'id': 'PRIVATE_TOOL_CANARY',
                'type': 'command_execution',
                'status': 'completed',
                'exit_code': 0,
                # Raw command output must be discarded after metadata extraction.
                'aggregated_output': 'PRIVATE_OUTPUT_CANARY',
            },
        })
        self.recorder.approvals('PRIVATE_TASK_CANARY', [{
            # An approval request identifier must remain internal.
            'approval_id': 'PRIVATE_APPROVAL_CANARY',
            'status': 'pending',
            # The approval explanation is private prose, not an exported rule.
            'reason': 'PRIVATE_REASON_CANARY',
            'can_allow': True,
        }])
        self.time += 2
        self.recorder.approvals(
            'PRIVATE_TASK_CANARY',
            # A resolved approval still must not export its internal identifier.
            [{'approval_id': 'PRIVATE_APPROVAL_CANARY', 'status': 'denied', 'can_allow': True}],
        )
        self.recorder.event(
            'PRIVATE_TASK_CANARY',
            {'kind': 'ha_read', 'operation': 'history', 'domain': 'light'},
            # Raw entity identifiers must be replaced with keyed references.
            entities=['light.private_entity_canary', 'light.private_entity_canary'],
            # Only the folder category and suffix may survive this filename.
            paths=['/config/scripts/private_file_canary.yaml'],
        )
        # The exception message must be replaced with the fixed ValueError text.
        self.recorder.finish('PRIVATE_TASK_CANARY', 'failed', ValueError('PRIVATE_ERROR_CANARY'))
        export = self.store.export()
        raw = json.dumps(export)
        disk = self.store.path.read_text()
        self.assertNotIn('CANARY', raw)
        self.assertNotIn('CANARY', disk)
        self.assertNotIn('private_entity_canary', raw)
        self.assertNotIn('private_file_canary', raw)
        self.assertEqual(export['coverage'], 'remote ChatGPT threads: not recorded')
        row = export['interactions'][0]
        self.assertEqual(row['duration_seconds'], 5)
        self.assertEqual(row['cli_version'], '0.166.0')
        self.assertEqual(row['events'][0]['duration_seconds'], 3)
        self.assertEqual([x['decision'] for x in row['events'] if x['kind'] == 'approval'], ['asked', 'denied'])
        self.assertEqual(row['events'][-1]['message'], 'Invalid operation.')
        self.assertEqual(row['entities'][0], row['entities'][1])
        self.assertEqual(row['source'], 'scheduled_run')

    def test_running_upserts_do_not_duplicate_and_sort_by_start(self):
        self.recorder.begin('long', 'addon_page')
        for n in range(27):
            self.time += 1
            self.recorder.begin(str(n), 'lead_channel')
            self.recorder.finish(str(n), 'done')
        self.recorder.event('long', {'kind': 'error', 'type': 'RuntimeError'})
        self.recorder.finish('long', 'stopped')
        rows = self.store.export()['interactions']
        self.assertEqual(len(rows), 25)
        self.assertEqual(sum(x['source'] == 'lead_channel' for x in rows), 24)
        self.assertEqual(sum(x['source'] == 'addon_page' and x['outcome'] == 'stopped' for x in rows), 1)
        self.assertLessEqual(self.store.path.stat().st_size, diag.MAX_BYTES)

    def test_timeout_and_cancel_close_pending_tools_without_inventing_results(self):
        for outcome in ('stopped', 'timed_out'):
            self.recorder.begin(outcome, 'addon_page')
            self.recorder.activity(outcome, {'type': 'item.started', 'item': {'id': 'tool', 'type': 'file_change'}})
            self.time += 4
            self.recorder.finish(outcome, outcome)
        rows = self.store.export()['interactions']
        self.assertEqual([x['outcome'] for x in rows], ['stopped', 'timed_out'])
        self.assertEqual(rows[0]['events'][0]['name'], 'fileChange')
        self.assertEqual(rows[0]['events'][0]['result'], 'stopped')
        self.assertEqual(rows[1]['events'][0]['result'], 'unknown')

    def test_gate_classification_does_not_infer_hidden_rules(self):
        self.recorder.begin('task', 'addon_page')
        self.recorder.approvals(
            'task',
            # Untrusted approval prose cannot identify a hidden gate rule.
            [{'approval_id': 'one', 'status': 'pending', 'can_allow': False, 'note': 'PRIVATE_CANARY'}],
        )
        self.recorder.approvals('task', [{
            'approval_id': 'two',
            'status': 'pending',
            'can_allow': False,
            'note': "This request touches the app's permission policy or settings, which Codex may not edit. Deny it.",
        }])
        self.recorder.finish('task', 'done')
        gates = [x['rule'] for x in self.store.export()['interactions'][0]['events'] if x['kind'] == 'gate']
        self.assertEqual(gates, ['other', 'protected_policy'])

    def test_storage_failure_never_changes_execution(self):
        # A storage exception carries private text that must not affect task execution.
        with patch.object(self.store, 'append', side_effect=OSError('PRIVATE_CANARY')):
            self.recorder.begin('task', 'addon_page')
            self.recorder.event('task', {'kind': 'error'})
            self.recorder.finish('task', 'failed')
        self.assertEqual(self.recorder.active, {})
        self.assertEqual(self.store.export()['availability'], 'unavailable')

    def test_address_shaped_version_fields_are_not_exported(self):
        from diagnostics_scrub import sanitize_export
        address = '.'.join(map(str, (192, 0, 2, 44)))
        out = sanitize_export({'interactions': [{'addon_version': address, 'cli_version': address}]})
        self.assertNotIn(address, json.dumps(out))
        self.assertEqual(out['interactions'][0]['addon_version'], 'unknown')
        self.assertEqual(out['interactions'][0]['cli_version'], 'unknown')
        config = Path(__file__).resolve().parents[1] / 'config.yaml'
        import yaml
        self.assertEqual(
            (schema_value := yaml.safe_load(config.read_text())['version']),
            sanitize_export({'interactions': [{'addon_version': schema_value}]})['interactions'][0]['addon_version'],
        )

    def test_export_revalidation_rejects_nested_planted_values(self):
        from diagnostics_scrub import sanitize_export
        out = sanitize_export({
            # An arbitrary availability label must not bypass export validation.
            'availability': 'PRIVATE_CANARY',
            # Coverage prose is replaced with the fixed supported coverage statement.
            'coverage': 'PRIVATE_CANARY',
            'interactions': [{
                'source': 'remote_thread',
                # A malicious tool name and message must be revalidated.
                'events': [{'kind': 'tool', 'name': 'PRIVATE_CANARY', 'message': 'PRIVATE_CANARY'}],
                # Invalid entity-reference fields must not reach a download.
                'entities': [{'id': 'PRIVATE_CANARY', 'domain': 'PRIVATE_CANARY'}],
                # Invalid path categories must reduce to fixed values.
                'paths': [{'folder': 'PRIVATE_CANARY', 'extension': 'PRIVATE_CANARY'}],
            }],
        })
        self.assertNotIn('CANARY', json.dumps(out))
        self.assertEqual(out['interactions'][0]['source'], 'unknown')
