# Shared source, private workspace

Use this repository's public history on every machine. A workspace may contain
independent project repositories alongside the tool source. Root `.gitignore`
is an exact file allowlist: unlisted children, configuration, transcripts and
reports remain local. Add new reusable source files explicitly to the allowlist.
Ignored does not mean backed up, and does not remove previously committed history.

Local workspace files include `PROJECTS.md`, `COMMUNICATION.md`, `CONTEXT.md`,
`projects-mode.toml`, `.projects-mode-state.json`, `.codex/`, `.agents/`, and
`docs/local-workspace.md`. The shared `AGENTS.md` reads the local entry when it
exists. Keep project indexes, local rules and validation receipts in these ignored
files. Keep a separate private backup of them and external runtime state.

When adopting this source in a pre-existing private repository:

1. Commit outstanding repairs in the private history and preserve its Git metadata,
   linked worktrees, local files and a bundle outside the public checkout.
2. Prepare a clean checkout based on this repository's current public branch.
   Export reusable changes with synthetic examples; do not merge unrelated private
   history, copy credentials, or force-push over the public branch.
3. Retain the operator entry as `docs/local-workspace.md`. Preserve private root
   README content as `README.local.md`; update its relative links when moving it.
4. Replace only the source checkout and Git metadata after backups are verified.
   Retain project directories, runtime, identities and tasks at their existing paths.
5. Verify `git status`, the complete tracked-file list, ignored private files,
   commit ancestry, tests and the remote commit. Reopen orchestration sessions to
   load the new entry. Git migration does not change session permissions.

The `scripts/project_mode.py` and `scripts/setup_orch.py` helpers support existing
Agent Mail workspace configuration. They are not replacements for `cpo init` or
`cpo mode`; use the commands for the backend actually installed. Never copy one
machine's runtime snapshot, session IDs, token files or credential paths to another.

Private assignment trackers are independent of this public code repository.
Changing the code remote does not authorize publishing tasks or moving issues.
Email/Slack/LINE/Calendar automatic follow-up routing remains unimplemented;
this repository migration does not add that behavior.
