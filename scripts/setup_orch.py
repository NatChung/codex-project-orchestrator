#!/usr/bin/env python3
"""Localize Projects' Orch default; never install machine-wide requirements."""
import argparse
import copy
import json
from pathlib import Path
import tempfile
import tomllib

from project_mode import atomic, dump


def localized(config, root, home):
    root, home = root.resolve(), home.resolve()
    if not root.is_relative_to(home) or root == home:
        raise ValueError('Projects must be a directory below your home for this deny-home profile.')
    data = copy.deepcopy(config)
    fs = data['permissions']['orch']['filesystem']
    roots = [Path(k).parent for k in fs if k.endswith('/.codex')]
    if len(roots) != 1:
        raise ValueError('Cannot uniquely identify the source Projects root; no files changed.')
    old_root = roots[0]
    homes = [Path(k) for k, v in fs.items() if k.startswith('/') and v == 'deny' and old_root.is_relative_to(Path(k))]
    if len(homes) != 1 or fs.get(':minimal') != 'read':
        raise ValueError('Unexpected Orch profile; review it before migration.')
    old_home = homes[0]

    def remap(value):
        if isinstance(value, str):
            for old, new in [(old_root, root), (old_home, home)]:
                if value == str(old) or value.startswith(str(old) + '/'):
                    return str(new) + value[len(str(old)):]
            return value
        if isinstance(value, list):
            return [remap(v) for v in value]
        if isinstance(value, dict):
            return {remap(k): remap(v) for k, v in value.items()}
        return value

    data = remap(data)
    if 'sandbox_mode' in data or 'sandbox_workspace_write' in data:
        raise ValueError('Legacy sandbox settings conflict with named profiles; review them first.')
    data['default_permissions'] = 'orch'
    return data


def setup(root, home, apply=False):
    path = root / '.codex/config.toml'
    raw = path.read_text()
    config = tomllib.loads(raw)
    if tomllib.loads((root / 'projects-mode.toml').read_text())['mode'] != 'isolated':
        raise ValueError('Mode is local. Use project_mode.py to choose isolated before setup.')
    target = localized(config, root, home)
    changed = config != target
    # Existing mode snapshots belong to that host. Never silently invalidate them.
    if changed and (root / '.projects-mode-state.json').exists():
        raise ValueError('Existing mode snapshot needs reconciliation first; no files changed. Do not copy another host snapshot or delete this one to bypass the check.')
    backup = None
    if apply and changed:
        backup_dir = root / '.orch-setup-backups'
        backup_dir.mkdir(exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(mode='w', dir=backup_dir, prefix='config-', suffix='.toml', delete=False) as f:
            f.write(raw)
            backup = f.name
        atomic(path, dump(target))
    adapter = target.get('mcp_servers', {}).get('project_agents', {})
    command = Path(adapter.get('command', '/missing'))
    args = adapter.get('args', [])
    adapter_file = Path(args[0]) if args else Path('/missing')
    return {
        'default_setup': 'applied' if apply and changed else ('needs_apply' if changed else 'already_configured'),
        'root': str(root), 'default_permissions': target['default_permissions'],
        'manual_profile_switch': 'allowed_by_this_setup',
        'global_config_modified': False, 'cli_alias_modified': False,
        'backup': backup,
        'adapter_files_present': command.is_file() and adapter_file.is_file(),
        'worker_dispatch': 'not_verified', 'app_runtime': 'not_verified',
        'next_step': 'Open a new Projects task and verify its effective Orch profile. Missing external MCP services require separate setup; Git pull does not install them.',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1], help='Projects clone to configure (defaults to this checkout)')
    parser.add_argument('--apply', action='store_true', help='Back up and apply localized project settings; otherwise check only')
    args = parser.parse_args()
    try:
        result = setup(args.root.resolve(), Path.home(), args.apply)
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(2, str(exc) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result['default_setup'] == 'needs_apply':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
