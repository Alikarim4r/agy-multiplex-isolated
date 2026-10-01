# Architecture

## Control plane

The host-side Python scheduler owns account/project registries, Git worktrees, lane scheduling, result validation, and integration into per-project staging branches.

The control plane does not place OAuth secrets in JSON. Account registry records contain a Docker volume name, enabled state, masked identity, and SHA-256 identity hash.

## Account boundary

Each account has one master Docker HOME authenticated by the operator. A lane clones that master into a disposable volume before `agy` starts. Master volumes are not mounted into execution lanes.

This allows multiple lanes to be assigned to the same account without concurrent writes to one local credential file. Provider-side refresh behavior remains subject to the live gate.

## Project boundary

Each registered project points to a Git repository and a decomposition plan. Each run creates a project staging worktree. Every active task gets its own lane worktree created from the current staging head.

## Scheduler

Accounts and projects are both dynamic lists. There is no fixed cardinality in the scheduler.

For `N` selected accounts and `S` slots per account, logical account-slot capacity is `N × S`. A global `max_workers` cap may reduce that number. Ready tasks are selected round-robin across projects while conflicting write scopes within one project are kept serial.

## Fail-closed checks

A lane is rejected when any of these conditions occur:

- no bound identity hash exists for the assigned account;
- no authenticated identity event is observed;
- a foreign identity is observed;
- `agy` exits unsuccessfully;
- the executor changes Git HEAD;
- files outside the task's `write_scope` change;
- integration into project staging conflicts.

Normal live mode is disabled unless the operator explicitly enables it after lab validation.
