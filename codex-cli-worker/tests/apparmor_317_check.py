#!/usr/bin/env python3
"""Pinned HAOS parser check and disposable-runner loaded-policy regression.

Copyright 2026 The Backshop. No account, HA request, pairing or private data.
"""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

VERSION = '3.1.7'
SHA256 = '64494bd99fa6547a9cbdb4fc6bc732451a02dd19e6eb70eab977b239632151eb'
URL = 'https://gitlab.com/apparmor/apparmor/-/archive/v3.1.7/apparmor-v3.1.7.tar.gz'
NAMES = ('local_codex_cli_worker', 'f3c0ffee_codex_cli_worker')
RELAY = '///opt/codex-sandbox/relay-launcher'
TOOL = '///opt/codex-sandbox/bwrap.real'
BINDIR = Path('/opt/codex-sandbox')


def run(command, **kwargs):
    result = subprocess.run(command, capture_output=True, text=True, timeout=90, **kwargs)
    if result.returncode:
        raise AssertionError('Candidate check failed: ' + result.stderr[-3000:])
    return result.stdout


def label():
    try:
        return Path('/proc/self/attr/apparmor/current').read_text().strip()
    except FileNotFoundError:
        return Path('/proc/self/attr/current').read_text().strip()


def nnp():
    return dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)['NoNewPrivs'].strip()


def build(stage):
    stage.mkdir(parents=True, exist_ok=True)
    archive = stage / 'apparmor-v3.1.7.tar.gz'
    if not archive.exists():
        with urllib.request.urlopen(URL, timeout=40) as response:
            archive.write_bytes(response.read())
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == SHA256, 'HAOS source checksum mismatch'
    with tarfile.open(archive) as source:
        source.extractall(stage, filter='data')
    run(['make', '-C', str(stage / 'apparmor-v3.1.7/parser'), '-j2', 'USE_SYSTEM=1', 'DISTRO=unknown', 'POD2MAN=true', 'POD2HTML=true', 'apparmor_parser'])
    print('PASS: built SHA-256-verified HAOS parser 3.1.7', flush=True)


def compile_profiles(stage, worker, features=None):
    source = stage / 'apparmor-v3.1.7'
    parser = source / 'parser/apparmor_parser'
    assert 'AppArmor parser version 3.1.7\n' in run([str(parser), '--config-file', '/dev/null', '--version'])
    common = [str(parser), '--config-file', '/dev/null', '-K', '-T', '-j1', '-b', str(source / 'profiles/apparmor.d')]
    if features:
        common += ['--kernel-features', str(features)]  # Never override the policy ABI.
    raw = (worker / 'apparmor.txt').read_text()
    assert raw.count('profile codex_cli_worker ') == 1
    assert 'abi <abi/3.0>,' in raw
    profiles = []
    for name in NAMES:
        path = stage / (name + '.profile')
        text = raw.replace('profile codex_cli_worker ', 'profile ' + name + ' ', 1)
        path.write_text(text.replace('abi <abi/3.0>,', ''))
        failed = subprocess.run([*common, '-Q', str(path)], capture_output=True, text=True, timeout=30)
        assert failed.returncode != 0 and 'does not support stacking' in failed.stderr, 'Missing-ABI control did not reproduce B1'
        path.write_text(text)
        run([*common, '-Q', str(path)])
        names = run([*common, '-N', str(path)]).splitlines()
        assert name + RELAY in names and name + RELAY + TOOL in names
        profiles.append((name, path, text))
        print('PASS: 3.1.7 missing-ABI control fails; shipped policy compiles: ' + name, flush=True)
    return common, profiles


def errno_read(path):
    try:
        with open(path, 'rb') as stream:
            stream.read(1)  # Only a clean fixture environment or synthetic marker.
        return 'READABLE'
    except OSError as error:
        return error.errno


def check_child(name, parent, directory):
    relay = name + RELAY
    assert set(label().removesuffix(' (enforce)').split('//&')) == {relay, relay + TOOL}
    assert label().endswith(' (enforce)') and nnp() == '1'
    sibling = subprocess.Popen([sys.executable, '-I', '-c', 'import sys;sys.stdin.buffer.read()'], stdin=subprocess.PIPE, env={})
    try:
        paths = {'relay_root': f'/proc/{parent}/root{directory}/marker', 'relay_environ': f'/proc/{parent}/environ',
                 'relay_cwd': f'/proc/{parent}/cwd/marker', 'sibling_root': f'/proc/{sibling.pid}/root{directory}/marker'}
        results = {key: errno_read(path) for key, path in paths.items()}
        assert all(code in (1, 2, 13) for code in results.values()), 'A protected synthetic proc view was readable'
        print(json.dumps({'label': label(), 'nnp': nnp(), **results}), flush=True)
    finally:
        sibling.stdin.close()
        sibling.wait(timeout=5)


def fixture(name, directory):
    assert label() == name + RELAY + ' (enforce)' and nnp() == '1'
    directory = Path(directory)
    (directory / 'marker').write_text('synthetic-only')
    os.chdir(directory)
    # Positive control: same relay label, known synthetic bytes, no guard.
    run([sys.executable, '-I', '-c', 'import sys;assert open(sys.argv[1],"rb").read()==b"synthetic-only"', f'/proc/{os.getpid()}/root{directory}/marker'], env={})
    command = [str(BINDIR / 'bwrap'), '--unshare-user', '--bind', '/', '/', '--dev', '/dev', '--', sys.executable, '-I', str(Path(__file__).resolve()), 'child', name, str(os.getpid()), str(directory)]
    for tty in (False, True):
        if not tty:
            output = run(command, env={})
        else:
            master, slave = pty.openpty()
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, env={}, start_new_session=True)
            os.close(slave)
            data = bytearray()
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if select.select([master], [], [], .2)[0]:
                        try:
                            block = os.read(master, 8192)
                        except OSError as error:
                            if error.errno == errno.EIO:
                                break
                            raise
                        if not block:
                            break
                        data.extend(block)
                        assert len(data) < 16384
                    elif process.poll() is not None:
                        break
                assert process.wait(timeout=2) == 0, data.decode(errors='replace')
                output = data.decode()
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, 9)
                    process.wait()
                os.close(master)
        observed = json.loads(output)
        assert observed['nnp'] == '1'
        print(json.dumps({'tty': tty, **observed}), flush=True)


def enforce(stage, worker):
    assert os.geteuid() == 0, 'Loaded-policy test requires disposable-runner root'
    assert Path('/sys/kernel/security/apparmor/profiles').is_file(), 'AppArmor enforcement interface unavailable; no skip'
    assert not BINDIR.exists(), 'Do not overwrite an existing sandbox installation'
    common, profiles = compile_profiles(stage, worker)
    existing = Path('/sys/kernel/security/apparmor/profiles').read_text()
    assert not any(name in existing for name in NAMES), 'Do not replace an existing profile'
    BINDIR.mkdir(parents=True)
    loaded = []
    try:
        for src, dst in [('codex-bwrap', 'bwrap'), ('codex-bwrap-probe.py', 'codex-bwrap-probe.py'), ('relay-launcher', 'relay-launcher'), ('relay-tool-launcher', 'bwrap.real')]:
            shutil.copyfile(worker / src, BINDIR / dst)
            (BINDIR / dst).chmod(0o555)
        for dst in ('bwrap.exec', 'relay-bwrap'):
            shutil.copyfile('/usr/bin/bwrap', BINDIR / dst)
            (BINDIR / dst).chmod(0o555)
        # The checkout's runner-owned parent can require DAC override. After
        # dropping capabilities the fixture must use a traversable root-owned
        # path, just like the production immutable launchers.
        fixture_script = BINDIR / 'apparmor-proof.py'
        shutil.copyfile(Path(__file__).resolve(), fixture_script)
        fixture_script.chmod(0o555)
        sys.path.insert(0, str(worker))
        import remote_control as rc
        for name, path, text in profiles:
            run([*common, '-r', str(path)])
            loaded.append(path)
            for plain in (False, True):
                with tempfile.TemporaryDirectory(prefix='apparmor-proof-') as directory:
                    payload = [sys.executable, '-I', str(fixture_script), 'fixture', name, directory]
                    launch = [rc.RELAY_LAUNCHER, '--plain', *payload] if plain else rc.build_command(payload[0], 'Synthetic relay', *payload[1:])
                    # Enter the container root label immediately, then let the
                    # production exec rule select the relay's cx profile.
                    # aa-exec's default change-on-exec would override that cx.
                    print(run(['/usr/bin/aa-exec', '-i', '-p', name, '--', *launch], env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), end='', flush=True)
            # Kernel-enforced failing control: without only the stack permission,
            # the real guard must fail before native Bubblewrap is reached.
            path.write_text('\n'.join(line for line in text.splitlines() if not line.strip().startswith('change_profile ')) + '\n')
            run([*common, '-r', str(path)])
            denied = subprocess.run(['/usr/bin/aa-exec', '-p', name + RELAY, '--', '/usr/bin/setpriv', '--no-new-privs', str(BINDIR / 'bwrap.real'), '--help'], capture_output=True, text=True, timeout=20, env={'PATH': '/usr/bin:/bin'})
            assert denied.returncode != 0 and 'AppArmor relay-tool stack transition' in denied.stderr, 'Missing-permission control did not fail closed'
            path.write_text(text)
            run([*common, '-r', str(path)])
            print('PASS: loaded 3.1.7 policy, both actual launcher routes, pipe/PTY stack+NNP+proc denials, and missing-permission control: ' + name, flush=True)
    finally:
        try:
            for path in reversed(loaded):
                run([*common, '-R', str(path)])
        finally:
            shutil.rmtree(BINDIR)


def main():
    if sys.argv[1] == 'child':
        check_child(sys.argv[2], int(sys.argv[3]), sys.argv[4]); return
    if sys.argv[1] == 'fixture':
        fixture(sys.argv[2], sys.argv[3]); return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['build', 'compile', 'enforce'])
    parser.add_argument('stage', type=Path)
    parser.add_argument('--worker', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--features', type=Path)
    args = parser.parse_args()
    if args.mode == 'build': build(args.stage)
    elif args.mode == 'compile': compile_profiles(args.stage, args.worker, args.features)
    else:
        assert args.features is None, 'Never override kernel features during enforcement'
        enforce(args.stage, args.worker)


if __name__ == '__main__':
    main()
