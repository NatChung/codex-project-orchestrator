# Repository work

This repository is the reusable tool, not an operator's orchestration workspace.
Read README.md for the supported install and lifecycle flow. For changes to
permissions, dispatch, persistence or recovery, read docs/security.md first.

Keep examples synthetic. Runtime state, credentials, local logs, project lists
and machine-specific paths belong outside this repository. Tests use temporary
fixtures and fake RPC clients unless an operator explicitly authorizes a live
Codex test. Never claim mocked tests establish OS sandbox enforcement.

Run the relevant tests with `uv run python -m unittest discover -s tests -v`.
Update the corresponding usage and security documentation when behavior changes.
