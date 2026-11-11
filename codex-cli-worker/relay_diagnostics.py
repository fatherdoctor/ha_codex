#!/usr/bin/env python3
"""Account-free command/exec and loaded-label probe; never enables remote control.

Run inside the installed worker using the lead's SSH add-on/docker exec. This
uses synthetic state, not CODEX_HOME, options, auth.json or a pairing session.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

import remote_control as rc


POLICIES = (('read-only', {'type': 'readOnly'}),
            ('workspace-write', {'type': 'workspaceWrite', 'networkAccess': False}))


def clean_env(home: Path, runtime: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('HA_', 'HASS_', 'HOME_ASSISTANT_', 'CODEX_', 'OPENAI_'))
           and k not in ('SUPERVISOR_TOKEN', 'HASSIO_TOKEN')}
    env.update(CODEX_HOME=str(home), HOME=str(home), TMPDIR=str(runtime))
    return env


def probe(binary: str, command_factory, root: Path, profile: str | None = None) -> list[dict]:
    home, runtime, work = (root / part for part in ('home', 'runtime', 'workspace'))
    for folder in (home, runtime, work):
        folder.mkdir()
    socket = root / 'control.sock'
    metadata = root / 'identity.json'
    # Capture the actual post-setup label and PID immediately before exec of
    # app-server (exec inherits the label); no hard-coded aa-exec transition.
    boot = ('import os,json;from pathlib import Path;import sys;'
            'Path(sys.argv[1]).write_text(json.dumps({"pid":os.getpid(),'
            '"label":Path("/proc/self/attr/current").read_text().strip(),'
            '"caps":{k:v.strip() for k,v in (line.split(":",1) for line in '
            'Path("/proc/self/status").read_text().splitlines() if line.startswith("Cap"))}}));'
            'os.execv(sys.argv[2],sys.argv[2:])')
    command = command_factory(sys.executable, '-c', boot, str(metadata), binary,
                              'app-server', '--listen', 'unix://' + str(socket))
    log = (root / 'server.log').open('w')
    process = subprocess.Popen(command, env=clean_env(home, runtime), cwd=work,
                               stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                               start_new_session=True)
    results = []
    fixture = None
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and process.poll() is None:
            try:
                status = rc.ControlClient(socket).status()
                if status.get('status') not in ('disabled', None):
                    raise AssertionError('Synthetic app-server unexpectedly has remote control enabled')
                break
            except rc.ControlError:
                time.sleep(.1)
        else:
            raise AssertionError('Synthetic passive app-server failed to start')
        identity = json.loads(metadata.read_text())
        relay_label = identity['label']
        expected = profile + '///opt/codex-sandbox/relay-launcher' if profile else None
        if any(int(value, 16) for value in identity['caps'].values()):
            raise AssertionError('Relay retained setup capabilities')
        if expected and relay_label.removesuffix(' (enforce)') != expected:
            raise AssertionError('Actual relay label differs from the installed expected label')
        if expected:
            # The fixed control parent must already exist with RC off. Do not
            # create it here and accidentally hide a startup-order regression.
            anchor = Path('/data') / rc.CONTROL_FOLDER
            if not anchor.is_dir():
                raise AssertionError('Worker did not create the control mask anchor at startup')
            fd, path = tempfile.mkstemp(prefix='probe-', dir=anchor)
            os.write(fd, b'synthetic-only')
            os.close(fd)
            fixture = Path(path)
        for mode, original in POLICIES:
            policy = dict(original)
            if mode == 'workspace-write':
                policy['writableRoots'] = [str(work)]
            # Read real labels and attempts only: no private bytes are returned.
            script = ('import json,os;from pathlib import Path;'
                      'd={"label":Path("/proc/self/attr/current").read_text().strip()};'
                      'paths=json.loads(os.environ["PROBE_PATHS"]);'
                      '\nfor key,path in paths.items():\n'
                      ' try:\n'
                      '  with open(path,"rb") as f:f.read(1)\n'
                      '  d[key]="READABLE"\n'
                      ' except OSError as e:d[key]=e.errno\n'
                      '\nif os.environ.get("PROBE_WRITE"):\n'
                      ' try:\n'
                      '  with open(os.environ["PROBE_WRITE"],"w") as f:f.write("synthetic-only")\n'
                      '  d["proc_write"]="WRITABLE"\n'
                      ' except OSError as e:d["proc_write"]=e.errno\n'
                      'print(json.dumps(d))')
            paths = {}
            if expected:
                pid = identity['pid']
                paths = {'environ': f'/proc/{pid}/environ',
                         'root': f'/proc/{pid}/root{fixture}',
                         'direct': str(fixture),
                         'cwd': f'/proc/{pid}/cwd/../identity.json'}
            params = {'command': [sys.executable, '-c', script], 'cwd': str(work),
                      'sandboxPolicy': policy, 'env': {'PROBE_PATHS': json.dumps(paths),
                      'PROBE_WRITE': f'/proc/{identity["pid"]}/cwd/probe-write' if expected else ''},
                      'timeoutMs': 10000}
            result = rc.ControlClient(socket)._request('command/exec', params, 15, lambda: False)
            if result.get('exitCode') != 0:
                raise AssertionError('Pipe command/exec failed')
            observed = json.loads(result['stdout'])
            if expected:
                child_label = expected + '///opt/codex-sandbox/bwrap.real'
                if observed['label'].removesuffix(' (enforce)') != child_label:
                    raise AssertionError('Actual tool label differs from the relay and expected sandbox label')
                if any(observed[key] not in (2, 13, 1, 30) for key in (*paths, 'proc_write')):
                    raise AssertionError('Sandbox could read a protected synthetic relay path')
            results.append({'mode': mode, 'tty': False, 'exit': 0, **observed})
            result = rc.ControlClient(socket)._request('command/exec', {
                **params, 'command': ['/bin/sh', '-c', 'printf synthetic-pty-ok'],
                'tty': True, 'processId': 'probe-' + mode}, 15, lambda: False)
            if result.get('exitCode') != 0:
                raise AssertionError('PTY command/exec failed')
            results.append({'mode': mode, 'tty': True, 'exit': 0})
        # Only the unmanaged native fixture exercises this standalone route.
        # The installed worker's managed policy must keep refusing full access.
        if profile is None:
            for tty in (False, True):
                params = {'command': ['/bin/sh', '-c', 'printf synthetic-outside-ok'], 'cwd': str(work),
                          'sandboxPolicy': {'type': 'dangerFullAccess'}, 'timeoutMs': 10000}
                if tty:
                    params.update(tty=True, processId='probe-outside')
                result = rc.ControlClient(socket)._request('command/exec', params, 15, lambda: False)
                if result.get('exitCode') != 0:
                    raise AssertionError('Outside-sandbox execution failed')
                results.append({'mode': 'outside-synthetic', 'tty': tty, 'exit': 0,
                                'relay_label': relay_label, 'caps': identity['caps']})
        return results
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGINT)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        log.close()
        if fixture is not None:
            fixture.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', default='local_codex_cli_worker')
    parser.add_argument('--codex', default='/usr/local/bin/codex')
    parser.add_argument('--plain', action='store_true', help='also verify the fail-closed namespace fallback')
    args = parser.parse_args()
    parent = Path('/proc/self/attr/current').read_text().strip().removesuffix(' (enforce)')
    if parent != args.profile:
        raise SystemExit('Run inside the worker with its loaded enforcing profile; no aa-exec needed.')
    factory = ((lambda binary, *rest: [rc.RELAY_LAUNCHER, '--plain', binary, *rest]) if args.plain
               else (lambda binary, *rest: rc.build_command(binary, 'Synthetic relay', *rest)))
    with tempfile.TemporaryDirectory(prefix='relay-proof-', dir='/data') as folder:
        for item in probe(args.codex, factory, Path(folder), args.profile):
            print(json.dumps(item), flush=True)


if __name__ == '__main__':
    main()
