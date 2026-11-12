"""Relay filter precision, password-mode hiding and observable omissions."""
import json
import unittest

import test_core_address as fixtures
import test_relay_request_shapes as transport_fixtures
import relay_reads_mcp as mcp


class ReadFilterTests(unittest.TestCase):
    setUp = fixtures.RelayReadTests.setUp
    call = fixtures.RelayReadTests.call
    transport = transport_fixtures.RequestShapeTests.transport

    def test_ordinary_names_are_readable_and_listed(self):
        names = ('shopping_list', 'keyboard_backlight', 'alarm_bypass',
                 'washer_spin_speed', 'hotplate', 'postcode', 'monkey')
        self.fetch.return_value = [
            {'entity_id': 'light.' + name, 'state': 'off'} for name in names]
        result = self.call(operation='states', domain='light')
        self.assertEqual(result['total'], len(names))
        self.assertEqual({r['entity_id'] for r in result['entities']},
                         {'light.' + name for name in names})
        self.assertNotIn('notes', result)
        for name in names:
            with self.subTest(name=name):
                self.fetch.return_value = {'entity_id': 'light.' + name,
                                           'state': 'off', 'attributes': {}}
                self.assertTrue(self.call(operation='state', entity_id='light.' + name)['ok'])

    def test_credential_words_and_segments_remain_refused_before_fetch(self):
        words = ('pin', 'pincode', 'code', 'codes', 'passcode', 'otp', 'apikey',
                 'api_key', 'token', 'secret', 'password', 'credential', 'pw')
        for word in words:
            for name in (word, 'sample_' + word, word + '_sample', 'a_' + word + '_b'):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    self.call(operation='state', entity_id='input_text.' + name)
        self.fetch.assert_not_called()

    def test_hidden_rows_do_not_leak_names_or_values_in_listing_notes(self):
        self.fetch.return_value = [
            {'entity_id': 'light.sample', 'state': 'on'},
            {'entity_id': 'light.sample_passcode', 'state': 'synthetic-private-value'},
            {'entity_id': 'text.sample', 'state': 'synthetic-private-value',
             'attributes': {'mode': 'password'}}]
        result = self.call(operation='states')
        self.assertEqual(result['entities'], [{'entity_id': 'light.sample', 'state': 'on',
                                               'state_truncated': False}])
        self.assertIn('hidden', ' '.join(result['notes']).lower())
        self.assertNotIn('synthetic-private-value', json.dumps(result))
        self.assertNotIn('sample_passcode', json.dumps(result))
        self.assertNotIn('text.sample', json.dumps(result))

    def test_hidden_notes_and_page_clamp_coexist_and_pages_skip_hidden_rows(self):
        self.fetch.return_value = [
            {'entity_id': f'light.sample_{i:02d}', 'state': 'on'} for i in range(55)
        ] + [{'entity_id': 'light.sample_secret', 'state': 'synthetic-private-value'}]
        result = self.call(operation='states', domain='light', limit=100)
        self.assertEqual((result['total'], len(result['entities']), result['next_offset']), (55, 50, 50))
        self.assertTrue(any('hidden' in note.lower() for note in result['notes']))
        self.assertTrue(any('50' in note for note in result['notes']))
        last = self.call(operation='states', domain='light', offset=50, limit=100)
        self.assertEqual(len(last['entities']), 5)
        self.assertIsNone(last['next_offset'])
        self.assertTrue(last['notes'])

    def test_password_mode_both_text_domains_refused_in_state_and_listing(self):
        for domain in ('text', 'input_text'):
            for mode in ('password', 'PASSWORD'):
                row = {'entity_id': domain + '.sample', 'state': 'synthetic-private-value',
                       'attributes': {'mode': mode}}
                with self.subTest(domain=domain, mode=mode):
                    self.fetch.return_value = row
                    with self.assertRaisesRegex(ValueError, 'Password-mode'):
                        self.call(operation='state', entity_id=row['entity_id'])
                    self.fetch.return_value = [row]
                    result = self.call(operation='states', domain=domain)
                    self.assertEqual(result['entities'], [])
                    self.assertTrue(result['notes'])

    def test_plain_text_remains_readable_and_malformed_text_attributes_fail_closed(self):
        for domain in ('text', 'input_text'):
            for attrs in ({'mode': 'text'}, {}):
                self.fetch.return_value = {'entity_id': domain + '.sample', 'state': 'label',
                                          'attributes': attrs}
                self.assertTrue(self.call(operation='state', entity_id=domain + '.sample')['ok'])
            for attrs in (None, [], 'password'):
                row = {'entity_id': domain + '.sample', 'state': 'synthetic-private-value',
                       'attributes': attrs}
                self.fetch.return_value = [row]
                self.assertEqual(self.call(operation='states')['entities'], [])
                self.fetch.return_value = row
                with self.assertRaises(ValueError):
                    self.call(operation='state', entity_id=row['entity_id'])

    def test_notes_apply_only_to_requested_domain(self):
        self.fetch.return_value = [
            {'entity_id': 'light.sample', 'state': 'off'},
            {'entity_id': 'text.sample', 'state': 'synthetic-private-value',
             'attributes': {'mode': 'password'}}]
        result = self.call(operation='states', domain='light')
        self.assertEqual(result['total'], 1)
        self.assertNotIn('notes', result)

    def test_native_tool_reports_omissions_and_refuses_password_readback(self):
        self.fetch.return_value = [
            {'entity_id': 'light.keyboard_backlight', 'state': 'on'},
            {'entity_id': 'text.sample', 'state': 'synthetic-private-value',
             'attributes': {'mode': 'password'}}]
        with self.transport():
            response = mcp.respond({'method': 'tools/call', 'params': {
                'name': 'read', 'arguments': {'operation': 'states'}}})
            self.assertFalse(response['isError'])
            result = json.loads(response['content'][0]['text'])
            self.assertEqual(result['total'], 1)
            self.assertEqual(result['entities'][0]['entity_id'], 'light.keyboard_backlight')
            self.assertTrue(result['notes'])
            self.fetch.return_value = {'entity_id': 'text.sample', 'state': 'synthetic-private-value',
                                      'attributes': {'mode': 'password'}}
            result = mcp.read({'operation': 'state', 'entity_id': 'text.sample'})
            self.assertFalse(result['ok'])
            self.assertIn('Password-mode', result['error'])
            self.assertNotIn('synthetic-private-value', json.dumps(result))
