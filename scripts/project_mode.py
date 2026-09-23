#!/usr/bin/env python3
"""Switch only the Projects isolation overlay; retain unrelated local settings."""
import argparse
import copy
import fcntl
import json
import os
from pathlib import Path
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
BASE = Path.home() / '.local/share/projects-agent-mcp'
if os.environ.get('PROJECTS_AGENT_SETTINGS'):
    operator = tomllib.loads(Path(os.environ['PROJECTS_AGENT_SETTINGS']).read_text())
    ROOT = Path(operator['workspace'])
    BASE = Path(operator['runtime'])
    if not ROOT.is_absolute() or not BASE.is_absolute():
        raise ValueError('operator workspace/runtime must be absolute')
MODE = ROOT / 'projects-mode.toml'
STATE = ROOT / '.projects-mode-state.json'
MISSING = {'__projects_mode_missing__': True}
BLOCKED = ('node_repl', 'codegraph', 'mobile', 'computer-use', 'domain-tools', 'knowledge-tools')


def read_mode():
    mode = tomllib.loads(MODE.read_text())['mode']
    if mode not in ('local', 'isolated'):
        raise ValueError('mode must be local or isolated')
    return mode


def value(v):
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, dict):
        return '{' + ', '.join(value(k) + '=' + value(x) for k, x in v.items()) + '}'
    if isinstance(v, list):
        return '[' + ', '.join(map(value, v)) + ']'
    if isinstance(v, (int, float)):
        return str(v)
    if hasattr(v, 'isoformat'):
        return v.isoformat()
    raise TypeError(type(v))


def dump(data):
    lines = []
    def section(d, path):
        if path:
            lines.extend(['', '[' + '.'.join(map(value, path)) + ']'])
        for k, v in d.items():
            if not isinstance(v, dict):
                lines.append(value(k) + ' = ' + value(v))
        for k, v in d.items():
            if isinstance(v, dict):
                section(v, path + [k])
    section(data, [])
    return '\n'.join(lines).lstrip() + '\n'


def get(data, keys):
    for key in keys:
        if not isinstance(data, dict) or key not in data:
            return MISSING
        data = data[key]
    return data


def put(data, keys, v):
    key = keys[0]
    if len(keys) == 1:
        if v == MISSING:
            data.pop(key, None)
        else:
            data[key] = copy.deepcopy(v)
        return
    put(data.setdefault(key, {}), keys[1:], v)
    if not data[key]:
        data.pop(key)


def atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.project-mode-')
    try:
        with os.fdopen(fd, 'w') as out:
            out.write(text)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def targets():
    reg = json.loads((BASE / 'registry.json').read_text())
    extras = tomllib.loads(MODE.read_text()).get('extra_configs', [])
    extra_paths = [(ROOT / p).resolve() for p in extras]
    if any(not p.is_relative_to(ROOT.resolve()) or p.name != 'config.toml' or p.parent.name != '.codex' for p in extra_paths):
        raise ValueError('extra_configs must be project-local .codex/config.toml paths under Projects')
    return list(dict.fromkeys([ROOT / '.codex/config.toml'] + [Path(s['cwd']) / '.codex/config.toml' for s in reg.values()] + extra_paths))


def capture(paths):
    records = {}
    for p in paths:
        raw = p.read_text()
        d = tomllib.loads(raw)
        profile = d.get('default_permissions', '')
        if profile != 'orch' and not profile.startswith('worker-') and 'sandbox_mode' not in d:
            raise ValueError(f'Cannot capture isolation baseline for {p}: {profile}')
        old = BASE / 'backups/config' / str(p).lstrip('/')
        before = tomllib.loads(old.read_text()) if old.exists() else {}
        changes = []
        def change(keys, local):
            changes.append({'keys': keys, 'isolated': get(d, keys), 'local': local})
        change(['default_permissions'], ':danger-full-access')
        # Avoid stale legacy sandbox selection overriding the selected profile.
        if 'sandbox_mode' in d:
            change(['sandbox_mode'], MISSING)
        change(['features', 'plugins'], True)
        change(['features', 'network_proxy'], False)
        for name in BLOCKED:
            if get(d, ['mcp_servers', name, 'enabled']) is False:
                change(['mcp_servers', name, 'enabled'], get(before, ['mcp_servers', name, 'enabled']))
        if 'project_agents' in d.get('mcp_servers', {}):
            change(['mcp_servers', 'project_agents', 'enabled'], False)
        local = copy.deepcopy(d)
        for c in changes:
            put(local, c['keys'], c['local'])
        records[str(p)] = {'raw': raw, 'local_raw': dump(local), 'changes': changes}
    return {'version': 1, 'applied': 'isolated', 'files': records}


def check_targets(state, paths):
    if set(state['files']) != set(map(str, paths)):
        raise ValueError('Registry changed: enroll/review new isolation configs before switching; no files changed.')


def plan(state, mode):
    writes = {}
    for name, record in state['files'].items():
        p = Path(name)
        d = tomllib.loads(p.read_text())
        for c in record['changes']:
            actual = get(d, c['keys'])
            if actual != c[state['applied']]:
                raise ValueError(f'Managed setting drift: {name}: {".".join(c["keys"])}; no files changed.')
            put(d, c['keys'], c[mode])
        raw = record['raw'] if mode == 'isolated' else record['local_raw']
        writes[p] = raw if tomllib.loads(raw) == d else dump(d)
    if mode == 'isolated':
        orch = tomllib.loads(writes[ROOT / '.codex/config.toml'])
        if (orch.get('default_permissions') != 'orch'
                or not orch.get('permissions', {}).get('orch')
                or 'sandbox_mode' in orch):
            raise ValueError('Invalid isolated Orch configuration: require named orch profile without legacy sandbox override; no files changed.')
    return writes


def switch(mode, dry_run=False):
    paths = targets()
    state = json.loads(STATE.read_text()) if STATE.exists() else capture(paths)
    check_targets(state, paths)
    writes = plan(state, mode)
    # Prepare and validate every file before changing any config. State commits last.
    for raw in writes.values():
        tomllib.loads(raw)
    summary = {'mode': mode, 'configs': len(writes), 'dry_run': dry_run,
               'changed': [str(p) for p, raw in writes.items() if p.read_text() != raw]}
    if dry_run:
        return summary
    if not STATE.exists():
        atomic(STATE, json.dumps(state, ensure_ascii=False, indent=2) + '\n')
    originals = {p: p.read_text() for p in [*writes, MODE, STATE]}
    try:
        for p, raw in writes.items():
            if p.read_text() != raw:
                atomic(p, raw)
        mode_config = tomllib.loads(MODE.read_text())
        mode_config['mode'] = mode
        atomic(MODE, '# Use scripts/project_mode.py to apply this mode to all managed projects.\n' + dump(mode_config))
        state['applied'] = mode
        atomic(STATE, json.dumps(state, ensure_ascii=False, indent=2) + '\n')
    except BaseException:
        for p, raw in originals.items():
            atomic(p, raw)
        raise
    return summary


def status():
    state = json.loads(STATE.read_text()) if STATE.exists() else None
    result = {'mode': read_mode(), 'applied': state['applied'] if state else None}
    if state:
        check_targets(state, targets())
        plan(state, state['applied'])
        result['configs'] = len(state['files'])
        result['consistent'] = result['mode'] == state['applied']
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['local', 'isolated', 'apply', 'status'])
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    with (ROOT / '.projects-mode.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = status() if args.action == 'status' else switch(read_mode() if args.action == 'apply' else args.action, args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
