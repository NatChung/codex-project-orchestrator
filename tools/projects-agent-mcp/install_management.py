"""Operator-only installation. Backs up state/config; never launches workers or sends tasks."""
import argparse
import copy
import datetime
import json
import shutil
import sys
import tomllib
from pathlib import Path
from operator_settings import operator_settings

SOURCE=Path(__file__).resolve().parent
ROOT=Path(operator_settings()['workspace'])
BASE=Path(operator_settings()['runtime'])
sys.path.insert(0,str(SOURCE.parents[1]/'scripts'))
from project_mode import dump, atomic, get, put


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    reg=json.loads((BASE/'registry.json').read_text())
    runtime=tomllib.loads((BASE/'runtime.toml').read_text())
    state=json.loads((ROOT/'.projects-mode-state.json').read_text())
    global_config=tomllib.loads((Path.home()/'.codex/config.toml').read_text())
    writes={BASE/'operator.toml': dump(operator_settings())}
    orch=tomllib.loads((ROOT/'.codex/config.toml').read_text())
    runtime['permissions']['orch']=copy.deepcopy(orch['permissions']['orch'])
    runtime['permissions']['orch']['network']={'enabled':False}
    runtime['features'].update({feature:False for feature in ['memories', 'chronicle', 'hooks', 'apps', 'browser_use', 'browser_use_external', 'computer_use', 'in_app_browser', 'in_app_local_automation', 'image_generation', 'shell_snapshot', 'multi_agent_v2']})
    for name,spec in reg.items():
        fs=runtime['permissions'][spec['profile']]['filesystem']
        fs[spec['cwd']+'/.git']='read'
        fs[spec['cwd']+'/.agents']='read'
        # Explicit control-path denial remains more specific than future runtime exceptions.
        fs[str(BASE)]='deny'
    runtime['permissions']['orch']['filesystem'][str(BASE)]='deny'
    # start-app consumes each line as a TOML -c override, so use inline tables.
    from project_mode import value
    writes[BASE/'runtime.toml']='\n'.join(value(k)+'='+value(v) for k,v in runtime.items())+'\n'
    for project,spec in [('orchestrator',{'cwd':str(ROOT),'profile':'orch'}),*reg.items()]:
        path=Path(spec['cwd'])/'.codex/config.toml'
        cfg=tomllib.loads(path.read_text())
        changes={('permissions',spec['profile']):runtime['permissions'][spec['profile']],
                 ('features','multi_agent'):False,('features','collab'):False,
                 ('apps','_default','enabled'):False,('web_search',):'disabled'}
        for feature in ['memories', 'chronicle', 'hooks', 'apps', 'browser_use', 'browser_use_external', 'computer_use', 'in_app_browser', 'in_app_local_automation', 'image_generation', 'shell_snapshot', 'multi_agent_v2']:changes[('features',feature)]=False
        for name in set(global_config.get('mcp_servers',{}))|set(cfg.get('mcp_servers',{})):
            if name!='project_agents':changes[('mcp_servers',name,'enabled')]=False
        if project=='orchestrator':
            tools=cfg['mcp_servers']['project_agents']['enabled_tools']
            changes[('mcp_servers','project_agents','enabled_tools')]=list(dict.fromkeys(tools+['manage_worker','resume_task']))
            for name in ['manage_worker','resume_task']:changes[('mcp_servers','project_agents','tools',name,'approval_mode')]='approve'
        record=state['files'][str(path)]
        for keys,new in changes.items():
            previous=get(cfg,keys)
            put(cfg,list(keys),new)
            match=next((c for c in record['changes'] if c['keys']==list(keys)),None)
            if match:match['isolated']=new
            else:record['changes'].append({'keys':list(keys),'isolated':new,'local':previous})
        record['raw']=dump(cfg)
        local=copy.deepcopy(cfg)
        for c in record['changes']:put(local,c['keys'],c['local'])
        record['local_raw']=dump(local)
        writes[path]=dump(cfg)
    writes[ROOT/'.projects-mode-state.json']=json.dumps(state,ensure_ascii=False,indent=2)+'\n'
    for name in ['project_agents.py','rpc.py','lifecycle.py','worker-model.toml','start-mail.py','tracking_io.py','operator_settings.py']:
        writes[BASE/name]=(SOURCE/name).read_text()
    changed={p:v for p,v in writes.items() if not p.exists() or p.read_text()!=v}
    print(json.dumps({'apply':args.apply,'paths':[str(p) for p in changed]},indent=2))
    if not args.apply:return
    backup=BASE/'backups'/('management-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(parents=True,mode=0o700)
    for p,v in changed.items():
        if p.exists():
            dest=backup/str(p).lstrip('/');dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,dest)
    (backup/'manifest.json').write_text(json.dumps([str(p) for p in changed],indent=2))
    for p,v in changed.items():atomic(p,v)
    print('BACKUP',backup)
    print('Restart only mail service for retirement policy/tools. Reopen Orch and resume idle workers for new adapter/config. No workers started.')

if __name__=='__main__':main()
