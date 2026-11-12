"""Measure the actual unchanged hook process with captured shapes and bounded synthetic config."""
import json,os,statistics,subprocess,sys,tempfile,time
from pathlib import Path
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
bootstrap=("import sys,runpy;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import gate_rules;"
           "gate_rules.CONFIG_ROOT=Path(sys.argv[2]);"
           "sys.argv=sys.argv[3:];runpy.run_path(sys.argv[0],run_name='__main__')")
values=[];results=[]
with tempfile.TemporaryDirectory() as folder:
    tmp=Path(folder); config=tmp/'config';config.mkdir()
    state=tmp/'state.json';state.write_text('{"enabled":true}')
    target=config/'configuration.yaml'
    normal='shell_command:\n  example: before\n'
    # Long repetitive context stresses ambiguous-match refusal without quadratic work.
    stress='name: before\n'+('  value: stable\n'*30000)+'last: old\n'
    for label,body,payload,expect in [
        ('Bash listing','',{'tool_name':'Bash','tool_input':{'command':'ls /config'}},0),
        ('captured edit below definition',normal,{'tool_name':'apply_patch','tool_input':{'command':'*** Begin Patch\n*** Update File: configuration.yaml\n@@\n-  example: before\n+  example: after\n*** End Patch'}},2),
        ('450 KiB repetitive ordinary YAML',stress,{'tool_name':'apply_patch','tool_input':{'command':'*** Begin Patch\n*** Update File: configuration.yaml\n@@\n-last: old\n+last: new\n*** End Patch'}},0),
    ]:
        samples=[];target.write_text(body)
        for _ in range(10):
            start=time.perf_counter()
            result=subprocess.run([sys.executable,'-I','-c',bootstrap,str(root),str(config),
                str(root/'gate/gate_hook.py'),'--state',str(state)],
                input=json.dumps({**payload,'hook_event_name':'PreToolUse','cwd':str(config)}),
                text=True,capture_output=True,timeout=5)
            elapsed=time.perf_counter()-start;samples.append(elapsed);values.append(elapsed)
            assert result.returncode==expect,(label,result.returncode,result.stderr)
        results.append({'case':label,'samples':len(samples),'median_ms':round(statistics.median(samples)*1000,2),'max_ms':round(max(samples)*1000,2)})
output={'samples':len(values),'max_ms':round(max(values)*1000,2),'hook_timeout_ms':10000,'cases':results}
print(json.dumps(output,indent=2))
