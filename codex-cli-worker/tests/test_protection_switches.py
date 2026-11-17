"""Owner choices use synthetic files and requests; no live account or Core."""
import json
from pathlib import Path
import sys
import tempfile
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import approvals
import gate_rules
import policy
import protection_switches as switches
import remote_control


class ProtectionSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'protection-checks/settings.json'
        replacement = patch.object(switches, 'STATE_PATH', self.path)
        replacement.start()
        self.addCleanup(replacement.stop)

    def choose(self, **changes):
        switches.save({**switches.DEFAULTS, **changes})

    def test_private_signed_choice_survives_a_new_reader(self):
        self.assertEqual(switches.load(), switches.DEFAULTS)
        self.choose(shell_commands=False)
        self.assertFalse(switches.load(Path(str(self.path)))['shell_commands'])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.with_name('signing.key').stat().st_mode & 0o777, 0o600)

    def test_a_changed_signed_record_keeps_every_check_on(self):
        self.choose(shell_commands=False)
        record = json.loads(self.path.read_text())
        record['protections']['rest_commands'] = False
        self.path.write_text(json.dumps(record))
        self.assertEqual(switches.load(), switches.DEFAULTS)

    def test_missing_unreadable_unsigned_and_wrong_types_keep_all_on(self):
        for record in ({}, [], {'version': 1, 'protections': dict(switches.DEFAULTS)},
                       {'version': 1, 'protections': {**switches.DEFAULTS, 'full_access': 0}, 'signature': 'invalid'}):
            with self.subTest(record_type=type(record).__name__):
                self.path.parent.mkdir(exist_ok=True)
                self.path.write_text(json.dumps(record))
                self.assertEqual(switches.load(), switches.DEFAULTS)
        self.choose(shell_commands=False)
        with patch('protection_switches.os.open', side_effect=PermissionError()):
            self.assertEqual(switches.load(), switches.DEFAULTS)
        self.path.unlink()
        target = self.root / 'ordinary.json'
        target.write_text('{}')
        self.path.symlink_to(target)
        self.assertEqual(switches.load(), switches.DEFAULTS)

    def test_invalid_save_does_not_replace_the_current_choice(self):
        self.choose(shell_commands=False)
        for values in ({}, {**switches.DEFAULTS, 'unknown': True}, {**switches.DEFAULTS, 'full_access': 'off'}):
            with self.assertRaises(ValueError):
                switches.save(values)
            self.assertFalse(switches.load()['shell_commands'])

    def test_each_kind_changes_only_its_hook_and_watch_findings(self):
        samples = {
            'shell_commands': 'shell_command:\n  sample: echo sample\n',
            'rest_commands': 'rest_command:\n  sample:\n    url: https://example.invalid/\n',
            'command_line': 'command_line:\n  - sensor:\n      command: echo sample\n',
            'python_scripts': 'python_script:\n',
            'templated_actions': 'automation:\n  - action: "{{ sample }}"\n',
        }
        config = self.root / 'config'
        config.mkdir()
        for kind, text in samples.items():
            (config / (kind + '.yaml')).write_text(text)
        watch = remote_control.ConfigWatch(config, self.root / 'baseline.json')
        self.assertEqual(len(watch.scan()), 5)
        for disabled in samples:
            with self.subTest(disabled=disabled):
                self.choose(**{disabled: False})
                rules = gate_rules.load_rules()
                found = watch.scan()
                self.assertNotIn(disabled + '.yaml', found)
                self.assertEqual(len(found), 4)
                for kind, text in samples.items():
                    self.assertEqual(bool(gate_rules.find_refused(text, rules)), kind != disabled)
                    patch_text = '*** Begin Patch\n*** Add File: ' + str(config / 'new.yaml') + '\n'
                    patch_text += ''.join('+' + line + '\n' for line in text.splitlines()) + '*** End Patch\n'
                    request = {'tool_input': {'patch': patch_text}, 'cwd': str(config)}
                    with self.subTest(kind=kind), patch.object(gate_rules, 'CONFIG_ROOT', config):
                        self.assertEqual(bool(gate_rules.check_tool_call('PreToolUse', request, True, rules)), kind != disabled)

    def test_python_aliases_and_rest_platforms_share_their_kind(self):
        for key, names in [('python_scripts', ['python_script', 'pyscript']),
                           ('rest_commands', ['rest', 'scrape', 'rest_command'])]:
            self.choose(**{key: False})
            rules = gate_rules.load_rules()
            for name in names:
                self.assertEqual(gate_rules.find_refused(name + ':\n', rules), [])

    def test_credential_and_settings_checks_remain_independent(self):
        for credential_on, settings_on in [(True, True), (False, True), (True, False), (False, False)]:
            self.choose(credential_reads=credential_on, app_settings=settings_on)
            rules = gate_rules.load_rules()
            for command, expected in [('cat /config/secrets.yaml', credential_on),
                                      ('cat /data/options.json', settings_on),
                                      ('cat /data/protection-checks/settings.json', settings_on)]:
                self.assertEqual(bool(gate_rules.check_tool_call('PreToolUse', {'tool_input': {'command': command}}, True, rules)), expected)
            request = {'tool_input': {'path': '/data/protection-checks/signing.key'}}
            self.assertEqual(bool(gate_rules.check_tool_call('PreToolUse', request, False, rules)), settings_on)
            self.assertEqual(approvals.touches_protected_path('/data/worker_api_token'), credential_on)
            self.assertEqual(approvals.touches_protected_path('/data/options.json'), settings_on)
            self.assertEqual(approvals.command_names_protected_path('cat /data/worker_api_token'), credential_on)
            self.assertEqual(approvals.command_names_protected_path('cat /data/options.json'), settings_on)

    def test_review_paths_keep_their_own_group_after_normalization(self):
        # Review examples are classifier input only, never shell commands to run.
        samples = [
            ('cd /data && cat worker_api_token', 'credential_reads'),
            ('cat "/data/"worker_api_token', 'credential_reads'),
            ('cat /data/github/token>/data/options.json', 'app_settings'),
            ('cat /data/github/t>/usr/share/ha_codex/policy.json', 'app_settings'),
        ]
        for command, group in samples:
            with self.subTest(command=command):
                self.choose(**{**dict.fromkeys(switches.SELF, False), group: True})
                self.assertTrue(approvals.command_names_protected_path(command))

    def test_catalogue_groups_do_not_depend_on_explanatory_text(self):
        import tomllib
        template = (Path(__file__).resolve().parents[1] / 'gate/requirements.toml').read_text()
        changed = template.replace('credential access', 'private data').replace('Supervisor access', 'system actions')
        for kind in switches.SELF:
            values = {**switches.DEFAULTS, kind: False}
            original = tomllib.loads(switches.requirements(template, values))
            renamed = tomllib.loads(switches.requirements(changed, values))
            self.assertEqual([rule['pattern'] for rule in original['rules']['prefix_rules']],
                             [rule['pattern'] for rule in renamed['rules']['prefix_rules']])

    def test_every_shipped_rule_has_an_explicit_group(self):
        import tomllib
        template = Path(__file__).resolve().parents[1] / 'gate/requirements.toml'
        rules = tomllib.loads(template.read_text())['rules']['prefix_rules']
        identities = [switches.managed_rule_identity(rule) for rule in rules]
        self.assertEqual(len(identities), len(set(identities)))
        self.assertEqual(set(identities), set(switches.MANAGED_RULE_GROUPS))
        for rule in rules:
            group = switches.MANAGED_RULE_GROUPS[switches.managed_rule_identity(rule)]
            for disabled in switches.SELF:
                data = tomllib.loads(switches.requirements(template.read_text(),
                                    {**switches.DEFAULTS, disabled: False}))
                self.assertEqual(rule in data['rules']['prefix_rules'], disabled != group)

    def test_disabled_full_access_keeps_approval_identity_and_admin_boundary(self):
        self.choose(full_access=False)
        for change in ({'approvals_reviewer': 'auto_review'}, {'approvals_require_admin': False}):
            path = self.root / 'policy.json'
            path.write_text(json.dumps({**policy.STRICT_POLICY, **change}))
            with self.subTest(change=change), self.assertRaises(policy.PolicyError):
                policy.load_policy(path)

    def test_nonprivate_record_or_key_keeps_all_on(self):
        for name in ('settings.json', 'signing.key'):
            for mode in (0o640, 0o604):
                with self.subTest(name=name, mode=mode):
                    self.choose(shell_commands=False)
                    path = self.path.with_name(name)
                    path.chmod(mode)
                    self.assertEqual(switches.load(), switches.DEFAULTS)
                    path.chmod(0o600)

    def test_interrupted_key_creation_can_be_repaired_by_an_admin_save(self):
        self.path.parent.mkdir()
        key = self.path.with_name('signing.key')
        for size in (0, 16, 33):
            with self.subTest(size=size):
                key.write_bytes(b'x' * size)
                key.chmod(0o600)
                self.assertEqual(switches.load(), switches.DEFAULTS)
                self.choose(shell_commands=False)
                self.assertFalse(switches.load()['shell_commands'])
                self.assertEqual(len(key.read_bytes()), 32)

    def test_remaining_protections_notice_when_settings_can_be_changed(self):
        for changes in ({'app_settings': False}, {'full_access': False}):
            self.choose(**changes)
            self.assertIn('Remaining protections may no longer hold', switches.status()['notice'])

    def test_public_protection_claims_describe_owner_selected_full_access(self):
        root = Path(__file__).resolve().parents[2]
        claims = {
            'AUDITING.md': 'Full access is not available, whatever a client or an option asks for',
            'README.md': 'This fork has no full-access mode',
            'codex-cli-worker/README.md': 'This fork has no full-access mode',
            'codex-cli-worker/translations/en.yaml': 'There is no full-access mode in this fork',
            'SECURITY.md': None,
        }
        for name, obsolete in claims.items():
            with self.subTest(document=name):
                text = (root / name).read_text()
                if obsolete:
                    self.assertNotIn(obsolete, text)
                self.assertIn('Block full access', text)
                self.assertIn('container', text)

    def test_native_preparation_failure_keeps_installed_requirements(self):
        template = Path(__file__).resolve().parents[1] / 'gate/requirements.toml'
        output = self.root / 'requirements.toml'
        output.write_text(template.read_text())
        before = output.read_text()
        with patch.object(switches, 'TEMPLATE_PATH', template), patch.object(switches, 'REQUIREMENTS_PATH', output):
            with patch('protection_switches.os.replace', side_effect=OSError('synthetic write failure')):
                with self.assertRaises(OSError):
                    switches.prepare_native()
        self.assertEqual(output.read_text(), before)

    def test_building_arguments_does_not_write_native_requirements(self):
        from test_server import server
        with patch.object(switches, 'prepare_native') as prepare:
            args = server.build_app_server_args()
        self.assertIn('app-server', args)
        prepare.assert_not_called()

    def test_restriction_groups_do_not_depend_on_display_reason(self):
        self.choose(credential_reads=False)
        rules = gate_rules.load_rules()
        changed = tuple((group, pattern, 'updated explanation')
                        for group, pattern, _reason in gate_rules.POLICY_RESTRICTIONS)
        # The review's mixed-path example still names app settings.
        request = {'tool_input': {'command': 'cat /data/github/token>/data/options.json'}}
        with patch.object(gate_rules, 'POLICY_RESTRICTIONS', changed):
            self.assertTrue(gate_rules.check_tool_call('PreToolUse', request, True, rules))

    def test_managed_rule_loading_remains_on_for_each_enabled_group(self):
        self.choose(**dict.fromkeys(switches.SELF, False))
        self.assertFalse(remote_control.exec_rules_enabled())
        for kind in switches.SELF:
            self.choose(**{key: key == kind for key in switches.SELF})
            with self.subTest(kind=kind):
                self.assertTrue(remote_control.exec_rules_enabled())

    def test_full_access_choice_reaches_hook_policy_and_managed_requirements(self):
        template = (Path(__file__).resolve().parents[1] / 'gate/requirements.toml').read_text()
        for enabled in (True, False):
            self.choose(full_access=enabled)
            request = {'tool_input': {'command': 'codex --sandbox danger-full-access'}}
            self.assertEqual(bool(gate_rules.check_tool_call('PreToolUse', request, True, gate_rules.load_rules())), enabled)
            self.assertEqual(bool(policy.policy_problem({'codex_sandbox': 'danger-full-access'})), enabled)
            if enabled:
                with self.assertRaises(ValueError):
                    approvals.require_supported_policy({'sandbox': 'danger-full-access'})
            else:
                approvals.require_supported_policy({'sandbox': 'danger-full-access', 'approvalPolicy': 'never'})
            text = switches.requirements(template, switches.load())
            self.assertEqual('"danger-full-access"' in text.split('allowed_approval_policies')[0], not enabled)
            output = self.root / 'requirements.toml'
            output.write_text(text)
            self.assertEqual(remote_control.requirements_problem(output), '')

    def test_catalogue_omits_only_the_disabled_self_protection_group(self):
        import tomllib
        template = (Path(__file__).resolve().parents[1] / 'gate/requirements.toml').read_text()
        for kind, remaining in [('credential_reads', 3), ('app_settings', 5), ('full_access', 4)]:
            self.choose(**{kind: False})
            data = tomllib.loads(switches.requirements(template, switches.load()))
            self.assertEqual(len(data['rules']['prefix_rules']), remaining)
        self.choose(**dict.fromkeys(switches.SELF, False))
        self.assertFalse(remote_control.exec_rules_enabled())
        data = tomllib.loads(switches.requirements(template, switches.load()))
        self.assertNotIn('rules', data)

    def test_reenabled_native_protections_still_show_the_restart_notice(self):
        self.choose(full_access=False)
        switches.mark_app_start(switches.load())
        self.assertFalse(switches.status()['restart_required'])
        self.choose()
        state = switches.status()
        self.assertEqual(state['off'], [])
        self.assertTrue(state['restart_required'])
        self.assertIn('previous managed rules and launch permissions remain', state['notice'])

    def test_native_launcher_uses_the_same_signed_choices_and_keeps_the_capability_ceiling(self):
        worker = Path(__file__).resolve().parents[1]
        module = self.root / 'protection_switches.py'
        module.write_text((worker / 'protection_switches.py').read_text().replace(
            "STATE_PATH = Path('/data/protection-checks/settings.json')",
            'STATE_PATH = Path(' + repr(str(self.path)) + ')'))
        real = self.root / 'bwrap-fixture'
        real.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
        real.chmod(0o700)
        config, data = self.root / 'config', self.root / 'data'
        config.mkdir()
        data.mkdir()
        secret = config / 'secrets.yaml'
        secret.write_text('sample: synthetic\n')
        options = data / 'options.json'
        options.write_text('{}')
        launcher = self.root / 'bwrap'
        text = (worker / 'codex-bwrap').read_text()
        text = text.replace('REAL_BWRAP=/opt/codex-sandbox/bwrap.real', 'REAL_BWRAP=' + str(real))
        text = text.replace('reader=/protection_switches.py', 'reader=' + str(module))
        text = text.replace('/data/protection-checks', str(self.path.parent))
        text = text.replace('/data', str(data)).replace('/config', str(config))
        launcher.write_text(text)
        launcher.chmod(0o700)
        for credentials, settings in [(True, True), (False, True), (True, False), (False, False)]:
            with self.subTest(credentials=credentials, settings=settings):
                self.choose(credential_reads=credentials, app_settings=settings)
                args = json.loads(subprocess.check_output([str(launcher), '--', '/bin/echo', 'sample'], text=True))
                self.assertEqual(args[:2], ['--cap-drop', 'ALL'])
                self.assertEqual(str(secret) in args, credentials)
                self.assertEqual('/dev/null' in args, credentials or settings)
                self.assertEqual('--tmpfs' in args, settings)
                self.assertEqual('--bind' in args, not settings)
                self.assertEqual(args[-3:], ['--', '/bin/echo', 'sample'])

    def test_new_codex_start_regenerates_rules_without_clearing_an_older_process_notice(self):
        template = Path(__file__).resolve().parents[1] / 'gate/requirements.toml'
        output = self.root / 'requirements.toml'
        self.choose(full_access=False)
        with patch.object(switches, 'TEMPLATE_PATH', template), patch.object(switches, 'REQUIREMENTS_PATH', output):
            switches.prepare_native()
        self.assertIn('"danger-full-access"', output.read_text().split('allowed_approval_policies')[0])
        self.assertTrue(switches.status()['restart_required'])
        self.choose()
        self.assertTrue(switches.status()['restart_required'])
        switches.mark_app_start(switches.load())
        self.assertFalse(switches.status()['restart_required'])

    def test_disabled_watch_kind_does_not_restore_the_allowed_definition(self):
        config = self.root / 'config'
        config.mkdir()
        watch = remote_control.ConfigWatch(config, self.root / 'baseline.json')
        watch.take_baseline()
        self.choose(shell_commands=False)
        added = config / 'commands.yaml'
        text = 'shell_command:\n  sample: echo sample\n'
        added.write_text(text)
        self.assertEqual(watch.new_findings(remote_on=True), [])
        self.assertEqual(added.read_text(), text)

    def test_hook_process_reads_the_same_signed_kind_values(self):
        worker = Path(__file__).resolve().parents[1]
        hook_root = self.root / 'hook'
        hook_root.mkdir()
        for name in ['gate_rules.py', 'shell_words.py', 'protection_switches.py']:
            text = (worker / name).read_text()
            if name == 'protection_switches.py':
                text = text.replace("STATE_PATH = Path('/data/protection-checks/settings.json')",
                                    'STATE_PATH = Path(' + repr(str(self.path)) + ')')
            (hook_root / name).write_text(text)
        hook = hook_root / 'gate_hook.py'
        hook.write_text((worker / 'gate/gate_hook.py').read_text())
        remote = self.root / 'remote.json'
        remote.write_text('{"enabled": true}')
        commands = {'shell_commands': 'shell_command: sample', 'rest_commands': 'rest_command: sample',
                    'command_line': 'command_line: sample', 'python_scripts': 'python_script: sample',
                    'templated_actions': 'template: sample'}
        for disabled in commands:
            self.choose(**{disabled: False})
            for kind, command in commands.items():
                with self.subTest(disabled=disabled, kind=kind):
                    result = subprocess.run([sys.executable, '-I', str(hook), '--state', str(remote)],
                                            input=json.dumps({'tool_input': {'command': command}}),
                                            text=True, capture_output=True, timeout=5)
                    self.assertEqual(result.returncode, 0 if kind == disabled else 2)


class ProtectionApiTests(unittest.TestCase):
    choose = ProtectionSwitchTests.choose

    def setUp(self):
        ProtectionSwitchTests.setUp(self)
        self.app = Flask(__name__)
        self.admin = True
        worker = SimpleNamespace(app=self.app, DATA_ROOT=self.root,
                                 require_auth=lambda function: function,
                                 require_ingress=lambda function: function,
                                 ingress_admin=lambda: (object(), None) if self.admin else (None, 'Administrator required.'))
        switches.register(worker)
        self.client = self.app.test_client()

    def test_administrator_can_change_each_command_kind(self):
        for kind in switches.COMMANDS:
            response = self.client.post('/protection-checks', json={'kind': kind, 'enabled': False})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json['protections'][kind])
            self.assertIn(switches.COMMANDS[kind], response.json['notice'])
        self.assertEqual(self.client.get('/protection-checks').headers['Cache-Control'], 'no-store')

    def test_each_self_protection_requires_confirmation_and_raises_notice(self):
        for kind in switches.SELF:
            with self.subTest(kind=kind):
                self.choose()
                response = self.client.post('/protection-checks', json={'kind': kind, 'enabled': False})
                self.assertEqual(response.status_code, 409)
                self.assertTrue(switches.load()[kind])
                response = self.client.post('/protection-checks', json={'kind': kind, 'enabled': False, 'confirmed': True})
                self.assertEqual(response.status_code, 200)
                self.assertIn(switches.SELF[kind], response.json['notice'])
                response = self.client.post('/protection-checks', json={'kind': kind, 'enabled': True})
                self.assertEqual(response.json['off'], [])
                self.assertTrue(response.json['restart_required'])

    def test_confirmation_copy_comes_from_the_same_api_snapshot(self):
        state = self.client.get('/protection-checks').json
        self.assertEqual(state['confirmations'], switches.CONFIRMATIONS)
        for kind, expected in state['confirmations'].items():
            response = self.client.post('/protection-checks', json={'kind': kind, 'enabled': False})
            self.assertEqual(response.json['error'], expected)

    def test_non_administrator_cannot_read_or_change_choices(self):
        self.admin = False
        self.assertEqual(self.client.get('/protection-checks').status_code, 403)
        self.assertEqual(self.client.post('/protection-checks', json={'kind': 'full_access', 'enabled': False, 'confirmed': True}).status_code, 403)
        self.assertFalse(self.path.exists())

    def test_unknown_and_forged_payloads_are_rejected(self):
        for payload in ({'kind': 'full_access', 'enabled': 'false'},
                        {'kind': 'unknown', 'enabled': False},
                        {'kind': 'full_access', 'enabled': False, 'confirmed': 'true'},
                        {'kind': 'full_access', 'enabled': False, 'confirmed': True, 'administrator': True}):
            self.assertEqual(self.client.post('/protection-checks', json=payload).status_code, 400)
        self.assertEqual(switches.load(), switches.DEFAULTS)


if __name__ == '__main__':
    unittest.main()
