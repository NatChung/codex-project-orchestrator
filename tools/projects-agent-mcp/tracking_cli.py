"""Maintenance entry point; same tracking operations as the Orch MCP tool."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys

from tracking_io import OPERATIONS, run_tracking


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=sorted(OPERATIONS))
    parser.add_argument('--payload', type=Path, help='JSON arguments file; defaults to {}')
    parser.add_argument('--runtime', type=Path, default=Path.home() / '.local/share/projects-agent-mcp')
    args = parser.parse_args()
    payload = json.loads(args.payload.read_text()) if args.payload else {}
    spec = importlib.util.spec_from_file_location('tracking_runtime', args.runtime / 'project_agents.py')
    if spec is None or spec.loader is None:
        raise RuntimeError('Cannot load the installed Projects adapter')
    adapter = importlib.util.module_from_spec(spec)
    old_argv = sys.argv
    try:
        sys.argv = [str(args.runtime / 'project_agents.py'), 'orchestrator']
        sys.modules[spec.name] = adapter
        spec.loader.exec_module(adapter)
    finally:
        sys.argv = old_argv
    print(json.dumps(run_tracking(adapter, args.operation, payload), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
