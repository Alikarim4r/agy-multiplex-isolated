# CLI Reference

## Bootstrap

- `build-image` — build the pinned Linux `agy` image using the host UID/GID.
- `init [ACCOUNT ...]` — initialize local registries and optionally add account IDs.
- `doctor` — report Docker, image version, pool sizes, data root, and live-gate state.

## Accounts

- `accounts`
- `add-account ID`
- `enable-account ID`
- `disable-account ID`
- `remove-account ID` — registry-only; credential volume is preserved.
- `login ID`
- `bind ID`

Account IDs use letters, numbers, `_`, and `-`, are unique case-insensitively, and do not imply a fixed pool size.

## Projects

- `projects`
- `add-project --name ID --repo PATH --plan PATH [--goal TEXT] [--model MODEL]`
- `enable-project ID`
- `disable-project ID`
- `remove-project ID` — unregisters only; the repository is never deleted.

## Planning and execution

- `validate [--manifest FILE]`
- `dry-run [--manifest FILE]`
- `run [--manifest FILE] [--max-workers N] [--lab]`
- `status RUN_ID`
- `cleanup RUN_ID`

If no manifest is supplied, all enabled account and project registry entries are used. A manifest can specify `accounts: "all"`, `projects: "all"`, explicit lists, `per_account_slots`, and `max_workers`.

## Live gate

- `enable-live --ack "I ACCEPT LAB GATE REQUIREMENTS"`
- `disable-live`

Live enablement is only an operator acknowledgement; it does not prove that the lab gate passed.
