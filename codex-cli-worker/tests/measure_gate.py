"""Measure isolated hook calls, including the inventory that formerly timed out."""
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The slowest-supported-machine factor is an explicit engineering assumption,
# not a measurement from an owner's machine. Keep 2 seconds of the hook unused.
SLOW_MACHINE_FACTOR = 6
bootstrap = """
import atexit, json, runpy, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import gate_rules
if sys.argv[3] == 'python':
    gate_rules.YAML_LOADER = gate_rules.yaml.SafeLoader
gate_rules.CONFIG_ROOT = Path(sys.argv[2])
original = gate_rules.ParseScan
scans = []
def capture(*args, **kwargs):
    scan = original(*args, **kwargs)
    scans.append(scan)
    return scan
gate_rules.ParseScan = capture
def report():
    print(json.dumps({'nodes': sum(s.nodes for s in scans),
                      'bytes': sum(s.bytes for s in scans)}), file=sys.stderr)
atexit.register(report)
sys.argv = sys.argv[4:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""


def measure():
    results = []
    with tempfile.TemporaryDirectory() as folder:
        tmp = Path(folder)
        config = tmp / 'config'
        config.mkdir()
        state = tmp / 'state.json'
        state.write_text('{"enabled":true}')
        normal = 'shell_command:\n  example: before\n'
        stress = 'name: before\n' + '  value: stable\n' * 30000 + 'last: old\n'
        broad = ''.join(f'k{n}: value\n' for n in range(37000)) + 'last: old\n'
        cases = [
            ('Bash listing', '', 'ls /config', 0, {}),
            ('captured edit below definition', normal,
             '*** Begin Patch\n*** Update File: configuration.yaml\n@@\n-  example: before\n+  example: after\n*** End Patch', 2, {}),
            ('450 KiB malformed repetitive YAML', stress,
             '*** Begin Patch\n*** Update File: configuration.yaml\n@@\n-last: old\n+last: new\n*** End Patch', 2, {}),
            ('broad accepted mapping edit', broad,
             '*** Begin Patch\n*** Update File: configuration.yaml\n@@\n-last: old\n+last: new\n*** End Patch', 0, {}),
            ('four formerly allowed 242 KiB lists', '',
             '*** Begin Patch\n*** Add File: planted.yaml\n+shell_command: {sample: echo sample}\n*** End Patch', 2,
             {f'list_{n}.yaml': f'zz_list_{n}: [' + ','.join([str(n)] * 124000) + ']' for n in range(4)}),
            ('million item inventory', '',
             '*** Begin Patch\n*** Add File: ordinary.yaml\n+name: sample\n*** End Patch', 2,
             {'big.yaml': '[' + ','.join(['a'] * 1000100) + ']'}),
            ('total node boundary', '',
             '*** Begin Patch\n*** Add File: ordinary.yaml\n+name: sample\n*** End Patch', 2,
             {'boundary.yaml': '[' + ','.join(['a'] * 150000) + ']'}),
        ]
        for loader in ('native', 'python'):
            for label, body, command, expected, inventory in cases:
                for path in config.glob('*.yaml'):
                    path.unlink()
                (config / 'configuration.yaml').write_text(body)
                for name, text in inventory.items():
                    (config / name).write_text(text)
                samples, counts = [], []
                for _ in range(3):
                    start = time.perf_counter()
                    result = subprocess.run(
                        [sys.executable, '-I', '-c', bootstrap, str(ROOT), str(config), loader,
                         str(ROOT / 'gate/gate_hook.py'), '--state', str(state)],
                        input=json.dumps({'hook_event_name': 'PreToolUse', 'cwd': str(config),
                                          'tool_input': {'command': command}}),
                        text=True, capture_output=True, timeout=9)
                    samples.append(time.perf_counter() - start)
                    expect = 2 if loader == 'python' and expected == 0 and label != 'Bash listing' else expected
                    assert result.returncode == expect, (label, loader, result.returncode)
                    count = json.loads(result.stderr.splitlines()[-1])
                    assert count['nodes'] <= 150000 and count['bytes'] <= 2097152, count
                    counts.append(count)
                results.append({'case': label, 'loader': loader, 'samples': len(samples),
                                'median_ms': round(statistics.median(samples) * 1000, 2),
                                'max_ms': round(max(samples) * 1000, 2),
                                'max_nodes': max(c['nodes'] for c in counts),
                                'max_bytes': max(c['bytes'] for c in counts)})
    maximum = max(r['max_ms'] for r in results)
    return {'hook_timeout_ms': 10000, 'slow_machine_factor': SLOW_MACHINE_FACTOR,
            'max_ms': maximum, 'projected_ms': round(maximum * SLOW_MACHINE_FACTOR, 2),
            'cases': results}


if __name__ == '__main__':
    print(json.dumps(measure(), indent=2))
