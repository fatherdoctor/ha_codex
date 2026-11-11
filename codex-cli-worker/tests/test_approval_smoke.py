"""The packaged CLI smoke must prove rejection or a restricted managed fallback."""
import json
import subprocess
import sys
from unittest.mock import patch
import unittest

from test_server import server

# Reuse the suite's isolated worker module rather than importing another worker.
with patch.dict(sys.modules, {"server": server}):
    from codex_approval_smoke import assert_restricted_full_access

REQUIREMENTS = '/etc/codex/requirements.toml'
APPROVAL = ('Configured value for `approval_policy` is disallowed by requirements; '
            'falling back to required value OnRequest. Details: invalid value '
            f'(set by {REQUIREMENTS})')
PROFILE = ('Configured value for `permission_profile` is disallowed by requirements; '
           'falling back to required value Managed { file_system: Restricted { entries: [] }, '
           f'network: Restricted }}. Details: invalid value (set by {REQUIREMENTS})')


def result(*messages, returncode=0, item_type='error'):
    output = '\n'.join(json.dumps({'type':'item.completed','item':{'type':item_type,'message':message}})
                       for message in messages)
    return subprocess.CompletedProcess(['codex'], returncode, output, '')


class ApprovalSmokeOutcomeTests(unittest.TestCase):
    def test_nonzero_requirements_refusal_is_accepted(self):
        self.assertEqual(assert_restricted_full_access(result('Rejected by '+REQUIREMENTS, returncode=1)), 'refused')

    def test_both_warnings_and_reported_restricted_profile_are_accepted(self):
        self.assertEqual(assert_restricted_full_access(result(APPROVAL, PROFILE)), 'restricted fallback')

    def test_success_without_fallback_is_rejected(self):
        with self.assertRaises(AssertionError):
            assert_restricted_full_access(result())

    def test_each_warning_and_its_requirements_source_are_required(self):
        for messages in [(APPROVAL,), (PROFILE,), (APPROVAL.replace(REQUIREMENTS,'other'),PROFILE),
                         (APPROVAL,PROFILE.replace(REQUIREMENTS,'other'))]:
            with self.subTest(messages=messages), self.assertRaises(AssertionError):
                assert_restricted_full_access(result(*messages))

    def test_warning_alone_without_restricted_profile_is_rejected(self):
        with self.assertRaises(AssertionError):
            assert_restricted_full_access(result(APPROVAL, PROFILE.replace('Managed { file_system: Restricted { entries: [] }, network: Restricted }','unknown')))

    def test_success_with_unrestricted_or_full_access_profile_is_rejected(self):
        for profile in ['DangerFullAccess', 'Managed { file_system: Unrestricted, network: Restricted }',
                        'Managed { file_system: Restricted, network: Unrestricted }',
                        'Managed { file_system: Restricted, network: Restricted, full_access: true }']:
            message = 'Configured value for `permission_profile` is disallowed by requirements; falling back to required value '+profile+'. Details: (set by '+REQUIREMENTS+')'
            with self.subTest(profile=profile), self.assertRaises(AssertionError):
                assert_restricted_full_access(result(APPROVAL,message))

    def test_model_text_cannot_supply_configuration_warnings(self):
        with self.assertRaises(AssertionError):
            assert_restricted_full_access(result(APPROVAL,PROFILE,item_type='agent_message'))

    def test_unrelated_failure_is_not_a_policy_refusal(self):
        with self.assertRaises(AssertionError):
            assert_restricted_full_access(result('model fixture unavailable', returncode=1))
