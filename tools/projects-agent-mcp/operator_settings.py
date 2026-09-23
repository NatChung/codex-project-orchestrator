"""Operator-owned configuration, never supplied through worker MCP arguments."""
import os
import tomllib
from pathlib import Path

def operator_settings():
    path = Path(os.environ.get('PROJECTS_AGENT_SETTINGS', Path(__file__).resolve().parent / 'operator.toml'))
    settings = tomllib.loads(path.read_text())
    for key in ('workspace', 'runtime'):
        value = Path(settings[key])
        if not value.is_absolute():
            raise ValueError(f'{key} must be an absolute operator-owned path')
        settings[key] = str(value.resolve())
    require_separate_runtime(settings['runtime'], settings['workspace'])
    return settings

def require_separate_runtime(runtime, workspace):
    runtime, workspace = Path(runtime).resolve(), Path(workspace).resolve()
    if runtime.is_relative_to(workspace) or workspace.is_relative_to(runtime):
        raise ValueError('runtime must not overlap a workspace or worker project')
