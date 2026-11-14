"""Exercise managed requirements in an isolated filesystem with the pinned CLI.

Manual offline check (the CLI is the worker's declared @openai/codex dependency):
python tests/execrules_runtime_check.py --codex /path/to/0.166/codex --catalog /path/to/models.json
Requires the declared Bubblewrap runtime. Uses a loopback synthetic provider,
no account, relay, live Home Assistant or model request. Never writes /etc.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import itertools
import json
import os
from pathlib import Path
import queue
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import remote_control


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex', required=True)
    parser.add_argument('--catalog', required=True)
    args = parser.parse_args()
    results = []
    with tempfile.TemporaryDirectory(prefix='managed-rules-') as folder:
        scratch = Path(folder)
        home = scratch / 'home'; home.mkdir()
        managed = scratch / 'managed'; managed.mkdir()
        config = scratch / 'config'; config.mkdir()
        # Native workspace-write protects these existing metadata anchors.
        for name in ('.git', '.codex', '.agents', '.aws'):
            (config / name).mkdir()
        data = scratch / 'data'; data.mkdir()
        environment = scratch / 'environment'; environment.mkdir()
        binary_dir = scratch / 'bin'; binary_dir.mkdir()
        text = (ROOT / 'gate/requirements.toml').read_text()
        (managed / 'requirements.toml').write_text(text)
        for name in ('ha', 'codex'):
            target = binary_dir / name
            target.write_text('#!/bin/sh\nprintf EXECUTED\n'); target.chmod(0o755)
        for name in ('secrets.yaml', 'secrets.yml', '.storage/auth', '.storage/auth_provider.homeassistant'):
            p = config / name; p.parent.mkdir(exist_ok=True); p.write_text('SYNTHETIC_ONLY\n')
        for name in ('options.json', 'codex-home/config.toml'):
            p = data / name; p.parent.mkdir(exist_ok=True); p.write_text('SYNTHETIC_ONLY\n')
        for name in ('SUPERVISOR_TOKEN', 'HASSIO_TOKEN'):
            (environment / name).write_text('SYNTHETIC_ONLY\n')
        (managed / 'managed_config.toml').write_text('# No hook: prove the native layer alone.\n')
        requests = []
        commands = queue.Queue()

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(request)
                rid = 'fixture-' + str(len(requests))
                events = [{'type': 'response.created', 'response': {'id': rid}}]
                if not any(item.get('type') == 'function_call_output' for item in request.get('input', [])):
                    command = commands.get(timeout=5)
                    events.append({'type': 'response.output_item.done', 'item': {
                        'type': 'function_call', 'call_id': 'fixture-call', 'name': 'exec_command',
                        'arguments': json.dumps({'cmd': command, 'workdir': '/config', 'shell': '/bin/sh',
                                                 'login': False, 'yield_time_ms': 1000})}})
                else:
                    events.append({'type': 'response.output_item.done', 'item': {
                        'type': 'message', 'id': 'message', 'role': 'assistant',
                        'content': [{'type': 'output_text', 'text': 'Fixture complete.'}]}})
                events.append({'type': 'response.completed', 'response': {
                    'id': rid, 'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}})
                body = ''.join('data: ' + json.dumps(e) + '\n\n' for e in events).encode()
                self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        serving = threading.Thread(target=server.serve_forever); serving.start()
        (home / 'config.toml').write_text(
            'model_provider = "fixture"\nmodel = "gpt-6.1-sol"\nmodel_catalog_json = '
            + json.dumps(str(Path(args.catalog).resolve())) + '\n'
            '[features]\nshell_snapshot = false\nshell_snapshot_v2 = false\n'
            '[model_providers.fixture]\nname = "fixture"\nbase_url = "http://127.0.0.1:'
            + str(server.server_port) + '/v1"\nwire_api = "responses"\nrequires_openai_auth = false\n'
            'supports_websockets = false\n[projects."/config"]\ntrust_level = "trusted"\n')
        env = {'PATH': str(binary_dir) + ':/usr/bin:/bin', 'CODEX_HOME': str(home), 'HOME': str(home),
               'LANG': 'C.UTF-8', 'SUPERVISOR_TOKEN': 'SYNTHETIC_ONLY', 'HASSIO_TOKEN': 'SYNTHETIC_ONLY'}
        # Empty root, real executables read-only, synthetic protected paths only.
        namespace = ['bwrap', '--unshare-pid', '--tmpfs', '/', '--ro-bind', '/usr', '/usr',
                     '--ro-bind', '/lib', '/lib', '--ro-bind', '/lib64', '/lib64',
                     '--bind', str(scratch), str(scratch),
                     '--dir', '/etc', '--ro-bind', str(managed), '/etc/codex',
                     '--bind', str(config), '/config', '--ro-bind', str(data), '/data',
                     '--dir', '/run/s6', '--ro-bind', str(environment), '/run/s6/container_environment',
                     '--dir', '/tmp',
                     '--symlink', 'usr/bin', '/bin', '--proc', '/proc', '--dev', '/dev', '--chdir', '/config', '--']

        # Mount only the CLI resources and its declared model catalogue, at the
        # same absolute paths so the fixture also works outside this workspace.
        for path in sorted({Path(args.codex).resolve().parent, Path(args.catalog).resolve().parent}):
            namespace[-1:-1] = ['--ro-bind', str(path), str(path)]

        def launch(argv, **kwargs):
            kwargs['cwd'] = scratch
            return subprocess.Popen(namespace + argv, **kwargs)

        def batch(cases, expect_warning=False, expect_refused=True):
            lines = queue.Queue(); messages = []; approvals = []
            proc = launch([args.codex, 'app-server'], env=env, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                          bufsize=1, start_new_session=True)
            reader = threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout]); reader.start()
            counter = itertools.count(1)

            def send(message):
                proc.stdin.write(json.dumps(message) + '\n'); proc.stdin.flush()

            def receive(until):
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    try:
                        line = lines.get(timeout=0.2)
                    except queue.Empty:
                        if proc.poll() is not None: raise AssertionError('isolated CLI exited')
                        continue
                    message = json.loads(line); messages.append(message)
                    if 'id' in message and str(message.get('method', '')).endswith('requestApproval'):
                        approvals.append(message); send({'id': message['id'], 'result': {'decision': 'decline'}})
                    if until(message): return message
                raise AssertionError('isolated CLI timed out')

            try:
                ident = next(counter); send({'id': ident, 'method': 'initialize', 'params': {
                    'clientInfo': {'name': 'managed_rules_fixture', 'version': '0'},
                    'capabilities': {'experimentalApi': True}}})
                receive(lambda m: m.get('id') == ident)
                send({'method': 'initialized'})
                for label, command in cases:
                    start_requests, start_approvals = len(requests), len(approvals)
                    ident = next(counter); send({'id': ident, 'method': 'thread/start', 'params': {
                        'cwd': '/config', 'ephemeral': True, 'approvalPolicy': 'on-request', 'sandbox': 'workspace-write'}})
                    reply = receive(lambda m: m.get('id') == ident)
                    assert reply.get('result', {}).get('approvalPolicy') == 'on-request', 'unexpected policy'
                    tid = reply['result']['thread']['id']; commands.put(command)
                    ident = next(counter); send({'id': ident, 'method': 'turn/start', 'params': {
                        'threadId': tid, 'input': [{'type': 'text', 'text': 'Run the synthetic fixture once.'}]}})
                    receive(lambda m: m.get('method') == 'turn/completed' and m['params']['threadId'] == tid)
                    observed = [item.get('output', '') for req in requests[start_requests:]
                                for item in req.get('input', []) if item.get('type') == 'function_call_output']
                    if expect_refused:
                        assert observed and any('rejected: The managed command rule' in out for out in observed), label
                    else:
                        assert observed and any('SYNTHETIC_ONLY' in out for out in observed), (label, observed)
                    assert len(approvals) == start_approvals, label
                    results.append({'case': label, 'refused': expect_refused, 'approval_requests': 0})
                warnings = [m for m in messages if remote_control.exec_rules_warning_problem(m)]
                assert bool(warnings) == expect_warning, ('unexpected warning state', [m.get('params', {}).get('summary') for m in warnings])
            finally:
                remote_control.Supervisor._terminate(None, proc)
                reader.join(timeout=5); proc.stdin.close(); proc.stdout.close()
                assert not reader.is_alive(), 'reader remains'

        try:
            version = subprocess.check_output(namespace + [args.codex, '--version'], cwd=scratch, env=env, text=True, timeout=10).strip()
            assert version == 'codex-cli 0.166.0', 'the worker pin changed; revisit these checks'
            (managed / 'requirements.toml').write_text(text.split('# Literal command refusals supplement')[0])
            batch([('no managed rules: synthetic read executes', 'cat /config/secrets.yaml')], expect_refused=False)
            (managed / 'requirements.toml').write_text(text)
            requests.clear()
            assert not remote_control.exec_rules_self_test(args.codex, env, '/config', popen=launch), 'valid requirements failed self-test'
            assert not requests, 'self-test made a model request'
            patterns = tomllib.loads(text)['rules']['prefix_rules']
            cases = []
            for n, rule in enumerate(patterns, 1):
                options = [t.get('any_of', [t.get('token')]) for t in rule['pattern']]
                for tokens in itertools.product(*options):
                    literal = shlex.join(tokens)
                    cases.extend([(f'rule{n} {literal}', literal), (f'rule{n} pipeline {literal}', 'printf fixture | ' + literal)])
            batch(cases)
            # A malformed ordinary companion cannot erase managed refusals, but
            # it must prevent the production readiness check from allowing Turn on.
            (home / 'rules').mkdir(); (home / 'rules' / 'invalid.rules').write_text('not_valid_starlark(\n')
            before = len(requests)
            problem = remote_control.exec_rules_self_test(args.codex, env, '/config', popen=launch)
            assert problem == remote_control.EXEC_RULES_WARNING, 'malformed ordinary file passed self-test'
            assert len(requests) == before, 'malformed self-test made a model request'
            sup = remote_control.Supervisor('unused', scratch / 'state', dict, lambda: '', gate=lambda: '')
            ok, state = sup.start(); assert not ok and not state['available'] and state['blocked_reason'] == problem
            batch([('malformed companion retains managed refusal', 'ha supervisor')], expect_warning=True)
            print(json.dumps({'managed_rules': len(patterns), 'literal_and_pipeline_cases': len(cases),
                              'baseline_synthetic_read_executed': True,
                              'all_refused_without_approval': True, 'malformed_self_test': 'blocked',
                              'malformed_managed_refusal_retained': True, 'self_test_model_requests': 0,
                              'runtime': 'synthetic local provider, pinned CLI; no account'}, sort_keys=True), flush=True)
        finally:
            server.shutdown(); server.server_close(); serving.join()


if __name__ == '__main__':
    main()
