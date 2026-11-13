"""Synthetic account metadata, never an account or a model call."""
import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from unittest.mock import Mock
from account_access import AccountAccess, CHECK_INTERVAL_SECONDS, PINNED_REFRESH_MESSAGES


class AccountAccessTests(unittest.TestCase):
    def setUp(self):
        self.signature = 'synthetic-auth-generation'
        self.now = 1.0
        self.login = {'status_ok': True, 'has_auth_file': True, 'server_check': 'unchecked'}
        self.read = Mock(return_value={'rateLimits': {'planType': 'free', 'primary': {'usedPercent': 100}},
                                       'email': 'SYNTHETIC_ACCOUNT_VALUE'})
        self.record = Mock(return_value=True)
        self.check = AccountAccess(lambda: self.login, self.read, lambda: self.signature,
                                   self.record, clock=lambda: self.now)

    def test_plan_and_exhausted_usage_never_raise_a_repair_or_prove_access(self):
        self.check.refresh()
        result = self.check.snapshot(self.login)
        self.assertEqual(result['plan_type'], 'free')
        self.assertEqual(result['state'], 'unconfirmed')
        self.assertFalse(result['needs_sign_in'])
        self.assertNotIn('SYNTHETIC_ACCOUNT_VALUE', str(result))

    def test_daily_check_and_new_signin_bypass_the_daily_cache(self):
        self.assertTrue(self.check.refresh())
        self.now += CHECK_INTERVAL_SECONDS - 1
        self.assertFalse(self.check.refresh())
        self.now += 1
        self.assertTrue(self.check.refresh())
        self.signature = 'new-synthetic-generation'
        self.assertTrue(self.check.refresh())
        self.assertEqual(self.read.call_count, 3)

    def test_busy_check_defers_without_consuming_the_day(self):
        self.check.busy = lambda: True
        self.assertFalse(self.check.refresh())
        self.check.busy = lambda: False
        self.assertTrue(self.check.refresh())

    def test_only_pinned_refresh_failures_record_expired_signin(self):
        for code in ('refresh_token_expired', 'refresh_token_reused', 'refresh_token_invalidated'):
            with self.subTest(code=code):
                self.read.side_effect = RuntimeError(code)
                self.record.reset_mock()
                self.check.refresh(force=True)
                self.record.assert_called_once_with('rejected', self.signature)

    def test_pinned_user_facing_refresh_messages_are_recognized(self):
        for message in PINNED_REFRESH_MESSAGES:
            with self.subTest(message=message):
                self.read.side_effect = RuntimeError('upstream: ' + message)
                self.record.reset_mock()
                self.check.refresh(force=True)
                self.record.assert_called_once_with('rejected', self.signature)

    def test_generic_errors_do_not_invent_an_access_or_signin_refusal(self):
        for error in ('401 Unauthorized', '403 Forbidden', '429 exhausted', 'network timeout'):
            with self.subTest(error=error):
                self.read.side_effect = RuntimeError(error)
                self.record.reset_mock()
                self.check.refresh(force=True)
                self.record.assert_not_called()
                self.assertEqual(self.check.snapshot(self.login)['state'], 'unconfirmed')

    def test_metadata_from_a_previous_signin_is_discarded(self):
        def changed():
            self.signature = 'new-synthetic-generation'
            return {'rateLimits': {'planType': 'pro'}}
        self.read.side_effect = changed
        self.check.refresh()
        self.assertIsNone(self.check.snapshot(self.login)['plan_type'])

    def test_unknown_plan_text_and_unknown_schema_remain_unavailable(self):
        for result in ({'rateLimits': {'planType': 'SYNTHETIC_ACCOUNT_VALUE'}}, {'account': {'planType': 'pro'}}, {}):
            with self.subTest(result=result):
                self.read.return_value = result
                self.check.refresh(force=True)
                self.assertIsNone(self.check.snapshot(self.login)['plan_type'])

    def test_signedout_expired_unavailable_and_good_signin_are_distinct(self):
        for login, required in (({'status_ok': False, 'has_auth_file': False}, True),
                                ({'status_ok': False, 'has_auth_file': True, 'server_check': 'rejected'}, True),
                                ({'status_ok': False, 'has_auth_file': True}, None),
                                ({'status_ok': True, 'has_auth_file': True}, False)):
            with self.subTest(login=login):
                self.assertIs(self.check.snapshot(login)['needs_sign_in'], required)

    def test_successful_turn_confirms_only_the_matching_signin(self):
        self.check.successful_turn('old-synthetic-generation')
        self.record.assert_not_called()
        self.check.successful_turn(self.signature)
        self.assertEqual(self.check.snapshot(self.login)['state'], 'confirmed_by_turn')
        self.signature = 'new-synthetic-generation'
        self.assertEqual(self.check.snapshot(self.login)['state'], 'unconfirmed')


class AccountAccessWorkerTests(unittest.TestCase):
    def test_existing_usage_read_updates_safe_plan_metadata(self):
        from test_server import server
        from unittest.mock import patch
        metadata = {'rateLimits': {'planType': 'go', 'primary': {'windowDurationMins': 10080, 'usedPercent': 25}}}
        with (patch.object(server, 'active_task_id', return_value=None),
              patch.object(server, 'codex_binary_path', return_value='synthetic-codex'),
              patch.object(server, 'codex_login_status', return_value={'status_ok': True}),
              patch.object(server, 'read_codex_rate_limits', return_value=metadata),
              patch.object(server, 'record_sign_in_check'),
              patch.object(server.account_access, 'observe_metadata') as observed):
            result = server.fetch_codex_usage()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(observed.call_args.args[0], metadata)

    def test_successful_task_clears_only_its_own_recorded_signin(self):
        from test_server import server
        from unittest.mock import patch
        with (patch.dict(server.task_sign_in_signatures, {'synthetic-task': 'synthetic-generation'}),
              patch.object(server.account_access, 'successful_turn') as accepted):
            server.record_successful_sign_in('synthetic-task')
            accepted.assert_called_once_with('synthetic-generation')
            self.assertNotIn('synthetic-task', server.task_sign_in_signatures)


    def test_daily_expiry_repair_signal_clears_after_a_matching_successful_turn(self):
        from test_server import server
        from unittest.mock import patch
        login = {'has_auth_file': True, 'status_ok': True, 'message': 'Synthetic local login'}
        signature = (1, 2)
        reader = Mock(side_effect=RuntimeError(PINNED_REFRESH_MESSAGES[0]))
        with (patch.object(server, '_auth_signature', return_value=signature),
              patch.object(server, 'codex_local_login_status', side_effect=lambda: dict(login)),
              patch.dict(server.sign_in_check, {'result': '', 'auth_signature': None, 'checked_at': ''})):
            check = AccountAccess(lambda: dict(login), reader, server._auth_signature, server.record_sign_in_check)
            check.refresh()
            self.assertTrue(check.snapshot(server.codex_login_status())['needs_sign_in'])
            reader.side_effect = None
            reader.return_value = {'rateLimits': {'planType': 'free'}}
            check.refresh(force=True)
            self.assertTrue(check.snapshot(server.codex_login_status())['needs_sign_in'])
            check.successful_turn(signature)
            self.assertFalse(check.snapshot(server.codex_login_status())['needs_sign_in'])
            self.assertEqual(check.snapshot(server.codex_login_status())['state'], 'confirmed_by_turn')


def status_from_usage_failure(error, screen_error='Usage unavailable'):
    """Exercise the real status-triggered usage refresh with synthetic local sign-in."""
    from contextlib import ExitStack
    from unittest.mock import patch
    from test_server import server
    signature = (100, 200)
    with ExitStack() as stack:
        for name, value in (
            ('_auth_signature', lambda: signature),
            ('codex_local_login_status', lambda: {'status_ok': True, 'has_auth_file': True}),
            ('active_task_id', lambda: None), ('codex_binary_path', lambda: 'synthetic-codex'),
            ('api_token', lambda: 'synthetic-worker-auth'), ('read_options', lambda: {}),
            ('integration_status', lambda: {}), ('remote_control_summary', lambda: {}),
            ('auth_status_payload', lambda: {}), ('pause_payload', lambda: {})):
            stack.enter_context(patch.object(server, name, value))
        stack.enter_context(patch.object(server, 'tasks', {}))
        stack.enter_context(patch.object(server, 'message_queue', []))
        stack.enter_context(patch.dict(server.sign_in_check,
                                      {'result': '', 'auth_signature': None, 'checked_at': ''}, clear=True))
        stack.enter_context(patch.dict(server.usage_state, {'_refreshing': False}, clear=True))
        check = AccountAccess(server.codex_login_status, lambda: {}, server._auth_signature,
                              server.record_sign_in_check)
        stack.enter_context(patch.object(server, 'account_access', check))
        stack.enter_context(patch.object(server, 'read_codex_rate_limits', side_effect=RuntimeError(error)))
        stack.enter_context(patch.object(server, 'fetch_codex_usage_status',
                                        return_value={'status': 'error', 'error': screen_error, 'raw_excerpt': screen_error}))
        stack.enter_context(patch.object(server, 'refresh_usage_status_async',
                                        side_effect=lambda force=False: server._refresh_usage_worker(force=True)))
        response = server.app.test_client().get('/status', headers={'Authorization': 'Bearer synthetic-worker-auth'})
        assert response.status_code == 200
        return response.get_json()


class StatusUsageErrorTests(unittest.TestCase):
    def test_status_refresh_generic_http_errors_do_not_report_expired_signin(self):
        for error in ('401 Unauthorized', '403 Forbidden', '429 exhausted', 'network timeout'):
            with self.subTest(error=error):
                status = status_from_usage_failure(error)
                self.assertNotEqual(status['codex_login']['server_check'], 'rejected')
                self.assertEqual(status['account_access']['state'], 'unconfirmed')
                self.assertFalse(status['account_access']['needs_sign_in'])

    def test_status_refresh_preserves_explicit_codex_signin_rejections(self):
        for error in ('token_revoked', 'refresh_token_expired', 'refresh_token_reused',
                      'refresh_token_invalidated', *PINNED_REFRESH_MESSAGES):
            with self.subTest(error=error):
                status = status_from_usage_failure(error)
                self.assertEqual(status['codex_login']['server_check'], 'rejected')
                self.assertTrue(status['account_access']['needs_sign_in'])

    def test_complete_status_omits_account_and_token_from_usage_errors(self):
        import json
        for category in ('401 Unauthorized', 'network error', 'token_revoked'):
            with self.subTest(category=category):
                private_error = category + ' SYNTHETIC_PRIVATE_ACCOUNT SYNTHETIC_PRIVATE_TOKEN'
                status = status_from_usage_failure(private_error, screen_error=private_error)
                encoded = json.dumps(status)
                for marker in ('SYNTHETIC_PRIVATE_ACCOUNT', 'SYNTHETIC_PRIVATE_TOKEN'):
                    self.assertNotIn(marker, encoded)
                self.assertIn(status['codex_usage']['rate_limits_error_category'],
                              ('unavailable', 'sign_in_rejected'))

    def test_cached_usage_errors_are_bounded_before_the_next_refresh(self):
        import json
        from unittest.mock import patch
        from test_server import server
        private = 'SYNTHETIC_PRIVATE_ACCOUNT SYNTHETIC_PRIVATE_TOKEN'
        with patch.dict(server.usage_state, {'error': private, 'rate_limits_error': private,
                                            'rate_limits_error_category': private, 'raw_excerpt': private}, clear=True):
            payload = server.usage_status_payload()
        self.assertNotIn('SYNTHETIC_PRIVATE', json.dumps(payload))
        self.assertEqual(payload['rate_limits_error_category'], 'unavailable')
