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
