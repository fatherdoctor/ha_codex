"""Measure isolated hook calls, including the inventory that formerly timed out."""
import json
import shutil
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
sys.path.insert(0, str(ROOT))
import gate_rules
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
walked = []
compose = gate_rules._compose_yaml
class CountedValues(list):
    def __iter__(self):
        walked.append(len(self))
        return super().__iter__()
def counted_compose(text):
    node = compose(text)
    todo, seen = [node], set()
    while todo:
        item = todo.pop()
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, gate_rules.yaml.MappingNode):
            for key, value in item.value:
                todo.extend((key, value))
            item.value = CountedValues(item.value)
        elif isinstance(item, gate_rules.yaml.SequenceNode):
            todo.extend(item.value)
            item.value = CountedValues(item.value)
    return node
gate_rules._compose_yaml = counted_compose
def capture(*args, **kwargs):
    scan = original(*args, **kwargs)
    scans.append(scan)
    return scan
gate_rules.ParseScan = capture
def report():
    print(json.dumps({'nodes': sum(s.nodes for s in scans),
                      'bytes': sum(s.bytes for s in scans), 'walked_items': sum(walked)}), file=sys.stderr)
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
            ('450 KiB unrelated malformed YAML retains line rules', stress,
             '*** Begin Patch\n*** Update File: configuration.yaml\n@@\n-last: old\n+last: new\n*** End Patch', 0, {}),
            ('broad accepted mapping edit', broad,
             '*** Begin Patch\n*** Update File: configuration.yaml\n@@\n-last: old\n+last: new\n*** End Patch', 0, {}),
            ('four formerly allowed 242 KiB lists', '',
             '*** Begin Patch\n*** Add File: planted.yaml\n+shell_command: {sample: echo sample}\n*** End Patch', 2,
             {f'list_{n}.yaml': f'zz_list_{n}: [' + ','.join([str(n)] * 124000) + ']' for n in range(4)}),
            ('million item relevant inventory', '',
             '*** Begin Patch\n*** Add File: ordinary.yaml\n+name: sample\n*** End Patch', 2,
             {'big.yaml': 'sequence: [' + ','.join(['a'] * 1000100) + ']'}),
            ('reference-sized script and automation inventory',
             'automation: !include automations.yaml\nscript: !include scripts.yaml\n',
             '*** Begin Patch\n*** Add File: ordinary.yaml\n+# ordinary comment\n*** End Patch', 0,
             {'automations.yaml': '- alias: Sample\n  trigger: []\n  action: [{action: light.turn_on, target: {entity_id: light.sample}}]\n' * 6300,
              'scripts.yaml': ''.join(f'sample_{n}:\n  alias: Sample\n  sequence: [{{action: light.turn_on}}]\n' for n in range(3500))}),
            ('identical graph inventory', '',
             '*** Begin Patch\n*** Add File: ordinary.yaml\n+name: sample\n*** End Patch', 0,
             {f'list_{n}.yaml': '[' + ','.join(['{}'] * 1500) + ']'
              for n in range(2500)}),
            ('total node boundary', '',
             '*** Begin Patch\n*** Add File: ordinary.yaml\n+name: sample\n*** End Patch', 2,
             {'boundary.yaml': 'sequence: [' + ','.join(['a'] * 150000) + ']'}),
        ]
        chain = {f'c{n}.inc': (f'k{n}: !include c{n+1}.inc\n' if n < 59
                               else 'last: {sequence: []}\n') for n in range(60)}
        for remote in (False, True):
            cases.append(('60 includes beside a 140000-item document',
                'script: !include c0.inc\nzz: [' + ','.join(['1'] * 140000) + ']\n',
                '*** Begin Patch\n*** Add File: ordinary.yaml\n+# ordinary comment\n*** End Patch',
                2 if remote else 0, chain, remote))
        for loader in ('native', 'python'):
            for case in cases:
                label, body, command, expected, inventory = case[:5]
                remote = case[5] if len(case) > 5 else True
                state.write_text(json.dumps({'enabled': remote}))
                shutil.rmtree(config)
                config.mkdir()
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
                    relevant = any(gate_rules.relevant_yaml(text, gate_rules.load_rules())
                                   for text in [body, command, *inventory.values()])
                    expect = 2 if loader == 'python' and remote and relevant and label != 'Bash listing' else expected
                    assert result.returncode == expect, (label, loader, result.returncode)
                    count = json.loads(result.stderr.splitlines()[-1])
                    assert count['nodes'] <= 150000 and count['bytes'] <= 2097152, count
                    if '60 includes' in label:
                        assert count['walked_items'] < 1000000, count
                    counts.append(count)
                results.append({'case': label, 'loader': loader, 'remote': remote, 'samples': len(samples),
                                'median_ms': round(statistics.median(samples) * 1000, 2),
                                'max_ms': round(max(samples) * 1000, 2),
                                'max_nodes': max(c['nodes'] for c in counts),
                                'max_bytes': max(c['bytes'] for c in counts),
                                'max_walked_items': max(c['walked_items'] for c in counts)})
    maximum = max(r['max_ms'] for r in results)
    return {'hook_timeout_ms': 10000, 'slow_machine_factor': SLOW_MACHINE_FACTOR,
            'max_ms': maximum, 'projected_ms': round(maximum * SLOW_MACHINE_FACTOR, 2),
            'reserve_target_ms': 2000,
            'reserve_met': maximum * SLOW_MACHINE_FACTOR <= 8000,
            'cases': results}


if __name__ == '__main__':
    print(json.dumps(measure(), indent=2))
