"""Regression tests for wrapped writes and finds in approval requests."""
import io
import threading
import unittest

import approvals


class WrappedProtectedPaths(unittest.TestCase):
    def test_wrappers_hide_protected_writes_and_searches(self):
        commands = (
            'time cp x /etc/',
            'time -p cp x /etc/',
            '/usr/bin/time -f elapsed cp x /etc/',
            '/usr/bin/time --format elapsed cp x /etc/',
            '/usr/bin/time -o /tmp/timing cp x /etc/',
            '/usr/bin/time --output=/tmp/timing cp x /etc/',
            'exec cp x /etc/',
            'exec -a alias cp x /etc/',
            'exec -cl cp x /etc/',
            'exec -- cp x /etc/',
            'time -p exec -a alias cp x /etc/',
            'exec -a alias time -p find / -name x',
            'time -p find /etc -name x',
            'exec find / -name x',
            'unbuffer cp x /etc/',
            'unbuffer -p find / -name x',
            'time find /tmp -exec cp x /etc/ \\;',
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(approvals.command_names_protected_path(command))

    def test_request_surface_removes_allow_only_for_protected_actions(self):
        records = []
        session = approvals.AppServerSession(
            io.StringIO(), lock=threading.RLock(),
            thread_params={'sandbox': 'workspace-write', 'approvalPolicy': 'on-request', 'approvalsReviewer': 'user'},
            turn_params={'input': [{'type': 'text', 'text': 'Inspect'}]},
            on_event=lambda event: None, on_approvals=lambda items: records.extend(items[-1:]),
            on_final=lambda final: None, is_active=lambda: True, redact=lambda text: text,
        )
        session.start()
        session.receive({'id': 'worker-1', 'result': {}})
        session.receive({'id': 'worker-2', 'result': {'thread': {'id': 'thread'}, 'approvalPolicy': 'on-request', 'approvalsReviewer': 'user'}})
        session.receive({'id': 'worker-3', 'result': {'turn': {'id': 'turn'}}})
        for n, (command, expected) in enumerate((('time -p cp x /etc/', False),
                                                  ('exec -a alias find / -name x', False),
                                                  ('time -p cp x /tmp/', True))):
            session.receive({'id': n, 'method': approvals.COMMAND_APPROVAL, 'params': {
                'threadId': 'thread', 'turnId': 'turn', 'itemId': 'item',
                'command': command, 'cwd': '/config', 'reason': 'Review',
            }})
            with self.subTest(command=command):
                self.assertEqual(records[-1]['can_allow'], expected)

    def test_wrapped_nonprotected_commands_remain_eligible(self):
        commands = (
            'time -p cp /etc/hosts /tmp/copy',
            '/usr/bin/time -f elapsed find /tmp -name x',
            '/usr/bin/time -o /tmp/timing cp x /tmp/',
            'exec -a alias cp x /tmp/',
            'exec -cl find /tmp -name x',
            'time -p exec -a alias find /tmp -name x',
            'unbuffer -p cp x /tmp/',
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertFalse(approvals.command_names_protected_path(command))


if __name__ == '__main__':
    unittest.main()
