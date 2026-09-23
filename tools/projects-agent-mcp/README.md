# Existing Agent Mail adapter compatibility

This directory preserves lifecycle repairs for an existing, operator-managed
Agent Mail installation. New installations use the main `cpo` package and its
SQLite mailbox. This adapter is source-only, not part of the wheel, and does not
vendor or install Agent Mail. Verify that dependency's license separately.

## Configuration and deployment

Copy `operator.example.toml` to a private `operator.toml` outside the checkout.
Set `PROJECTS_AGENT_SETTINGS` to its absolute path when running maintenance tools.
Use an operator-controlled absolute workspace and runtime path. The optional
`communications_worker` is a registered role, not a permission grant. Tracking
requires explicit `[tracking] repo` and `account`; no public tracker is assumed.

An existing installation must already contain registry, identities, runtime
permission profiles, mode snapshot, model policy, authenticated mailbox service,
app-server socket and dependencies. This compatibility backend retains its
existing localhost port 18766 and `/projects-agent-mcp` mailbox key; the mail
launcher uses macOS `sandbox-exec`. These are not portable bootstrap defaults. Back these up before using
`install_management.py --apply`; without `--apply` it reports changes only.
The installer copies `operator_settings.py` and persists the supplied private
settings as `runtime/operator.toml`; deployed MCP processes need no environment override.
The installer updates source/configuration, never sends tasks or wakes workers.
Restart an idle mail service to load retirement policy; restart the app-server
when its permission definitions change. Open a fresh Orch session for new MCP
tools and effective permissions. Active workers are not hot-swapped.

## Controlled lifecycle

Only Orch exposes `manage_worker(project, operation, request_id)`. The project
must exist in the operator registry; callers cannot choose paths, profiles,
models, shell commands or thread IDs. Operations are create, restore, stop,
archive, retire, replace, rotate and reconcile. Workers only expose list/send/
fetch/acknowledge. Fixed worker model is `gpt-5.6-sol`, with no fallback; the
main session model remains the user's choice.

Stop interrupts the active turn and holds dispatch; it does not prove derived
processes stopped. Archive retains transcripts, task mappings and worktrees.
Retire also retires the mailbox agent; restore recovers the same identity and
session without replay. Replace preserves the old session mapping and holds
pending tasks for reconciliation. No operation deletes project files or results.

A task owns one worker context. After Orch verifies and acknowledges the task
and its reports, rotate archives that context. The next task starts a fresh
session with the same mailbox identity. Old conversation is not loaded; only
explicit continuation uses `resume_task(project, message_id)`. Legacy unread
messages are not automatically admitted. Unknown delivery or execution outcomes
block automatic retries. Stable request IDs and delivery journals deduplicate
requests, but cannot guarantee exactly-once external effects.

## Permissions and evidence

The installed profiles constrain Orch to coordination files and each worker to
its project. Control files, other projects, inherited MCP gateways, plugins,
memories and alternate tool surfaces are denied. Explicit connector exceptions
remain operator-owned configuration. Communication instructions are still prompts;
there is no automatic worker-to-Orch-to-communication-worker workflow here.

Offline checks, from the repository root:

```sh
uv run python -m unittest discover -s tools/projects-agent-mcp -v
uv run python -m unittest discover -s scripts -v
```

`verify_management_policies.py` performs bounded real OS probes against an existing
installation; it does not launch model tasks. Offline tests do not establish
sandbox enforcement. After deployment, separately verify a restricted Orch round
trip and actual worker model/profile. Queued, active and notLoaded are not task
completion evidence. Machine-specific receipts stay private and are not published.
