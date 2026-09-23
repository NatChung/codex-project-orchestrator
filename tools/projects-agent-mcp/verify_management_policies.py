"""Operator-only bounded OS probes; no model turn or business task is launched."""
import errno
import hashlib
import json
import sys
import uuid
from pathlib import Path
from operator_settings import operator_settings

BASE=Path(operator_settings()['runtime'])
sys.path.insert(0,str(BASE))
from rpc import RPC


def main():
    reg=json.loads((BASE/'registry.json').read_text())
    root=Path(operator_settings()['workspace'])
    marker='.orch-policy-'+uuid.uuid4().hex
    fixtures=[Path(s['cwd'])/marker for s in reg.values()]+[root/'docs'/marker]
    for p in fixtures:p.write_text('synthetic permission probe\n')
    evidence=[]
    try:
        for role,spec in [('orchestrator',{'cwd':str(root),'profile':'orch'}),*reg.items()]:
            checks=[]
            for p in fixtures:
                for op in ['read','write']:
                    checks.append({'path':str(p),'op':op,'expect':p.parent==Path(spec['cwd']) or role=='orchestrator' and p.parent==root/'docs'})
            for p in [BASE/'http-token',BASE/'registry.json',BASE/'runtime.toml',Path.home()/'.codex/config.toml',Path.home()/'.codex/memories/MEMORY.md']:
                for op in ['read','write']:checks.append({'path':str(p),'op':op,'expect':False})
            checks.append({'path':spec['cwd']+'/.codex/config.toml','op':'write','expect':False})
            code='''import os,json,socket
checks=json.loads(%r)
for v in checks:
 try:
  fd=os.open(v['path'],os.O_RDONLY if v['op']=='read' else os.O_WRONLY);os.close(fd);v['allowed']=True
 except OSError as e:v.update(allowed=False,errno=e.errno)
for family,address in [(socket.AF_UNIX,%r),(socket.AF_INET,('127.0.0.1',18766))]:
 v={'op':'connect','path':str(address),'expect':False};s=socket.socket(family);s.settimeout(1)
 try:s.connect(address);v['allowed']=True
 except OSError as e:v.update(allowed=False,errno=e.errno)
 finally:s.close()
 checks.append(v)
print(json.dumps(checks))
'''%(json.dumps(checks),str(BASE/'app.sock'))
            c=RPC()
            try:r=c.call('command/exec',{'command':[sys.executable,'-c',code],
                  'cwd':spec['cwd'],'permissionProfile':spec['profile'],'timeoutMs':10000})
            finally:c.close()
            if r['exitCode']!=0:raise RuntimeError({'role':role,'result':r})
            rows=json.loads(r['stdout'])
            failures=[v for v in rows if v['allowed']!=v['expect'] or not v['allowed'] and v.get('errno') not in [errno.EPERM,errno.EACCES]]
            evidence.append({'role':role,'checks':rows,'passed':not failures})
            print(role,'PASS' if not failures else json.dumps(failures))
        result={'runtime_sha256':hashlib.sha256((BASE/'runtime.toml').read_bytes()).hexdigest(),
                'app_pid':json.loads((BASE/'service-pids.json').read_text())['app'],
                'passed':all(x['passed'] for x in evidence),'profiles':evidence}
        (BASE/'management-policy-verification.json').write_text(json.dumps(result,indent=2))
        if not result['passed']:raise SystemExit(1)
        print('PASS',len(evidence),'profiles',sum(len(x['checks']) for x in evidence),'OS probes; no model turns')
    finally:
        for p in fixtures:p.unlink(missing_ok=True)

if __name__=='__main__':main()
