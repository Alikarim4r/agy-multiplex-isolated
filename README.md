# AGY Multiplex Isolated

**Experimental container-isolated scheduler for Antigravity CLI (`agy`) across a dynamic account pool and a dynamic project pool.**

[العربية](README_AR.md) · English

> Status: **0.1.0-alpha**. Live multiplex execution is disabled by default. Complete the lab gate before relying on concurrent OAuth refresh behavior.

## What it does

AGY Multiplex Isolated lets you register **any number of accounts and projects in software**. There is no hard-coded account count or project count. Actual throughput is limited by CPU, RAM, Docker, provider quotas, and the configured concurrency.

Each account has an isolated Docker master HOME. Every execution lane receives a disposable clone of that HOME, a separate container, and a separate Git worktree. The scheduler never mounts the host HOME or macOS Keychain into execution lanes.

Logical capacity is:

```text
selected accounts × per_account_slots
```

`max_workers` can cap that capacity globally.

## Safety model

- One independently authenticated Docker master volume per account.
- One disposable credential clone per active lane.
- No Antigravity Manager account switching during lane execution.
- No host HOME or Apple Keychain mount in lanes.
- Identity evidence must match the account binding or the lane is rejected.
- Agent writes are limited to declared `write_scope` values.
- Git push, merge-to-main, and deployment are not performed by the scheduler.
- Normal live mode is off until explicitly enabled after lab validation.
- Runtime data lives outside the repository in `~/.agy-multiplex-isolated` by default.

See [SECURITY.md](SECURITY.md) and [LAB_GATE.md](LAB_GATE.md).

## Requirements

- Python 3.11+
- Docker Desktop or Docker Engine
- Git
- Network access for installing and using the official Antigravity CLI

Host support in this alpha release: macOS and Linux. WSL2 may work through the Linux path but is not yet a validated target.

This is an independent community tool and is not affiliated with or endorsed by Google. Users are responsible for complying with the terms and quotas of the services they connect.

## Install

```bash
git clone <your-repository-url>
cd AGY-MULTIPLEX-ISOLATED
./install.sh
agy-multiplex-isolated build-image
agy-multiplex-isolated init
```

The Docker build pins `agy` to the expected version and fails closed if the installer returns a different version.

## Accounts: dynamic pool

Add as many account boundaries as you need:

```bash
agy-multiplex-isolated add-account personal
agy-multiplex-isolated add-account work-1
agy-multiplex-isolated add-account work-2
agy-multiplex-isolated accounts
```

Authenticate and bind each account:

```bash
agy-multiplex-isolated login work-1
agy-multiplex-isolated bind work-1
```

Or use the bilingual local login UI:

```bash
agy-account-login-ui
```

The UI binds only to `127.0.0.1`, uses a random per-launch UI token, and never displays OAuth tokens.

## Projects: dynamic registry

Register any number of Git projects:

```bash
agy-multiplex-isolated add-project \
  --name saferim \
  --repo ~/saferim \
  --plan ~/plans/saferim.json \
  --goal "Implement the approved SafeRim scope"

agy-multiplex-isolated projects
```

Registered accounts and projects can be enabled or disabled without deleting repositories or credential volumes:

```bash
agy-multiplex-isolated disable-account work-2
agy-multiplex-isolated enable-account work-2
agy-multiplex-isolated disable-project saferim
agy-multiplex-isolated enable-project saferim
```

`remove-account` removes only the registry entry and deliberately preserves the credential volume. `remove-project` never deletes the repository.

## Plan format

Each project plan contains tasks with independent write scopes and optional dependencies. See [`examples/plan.example.json`](examples/plan.example.json).

Tasks whose dependencies are complete and whose write scopes do not overlap may run concurrently.

## Run from registries

When `--manifest` is omitted, all enabled registered accounts and projects are selected:

```bash
agy-multiplex-isolated validate
agy-multiplex-isolated dry-run
```

For a controlled lab run:

```bash
agy-multiplex-isolated run --lab --max-workers 4
```

A manifest is optional. Use one when you want to select a subset or override concurrency:

```json
{
  "accounts": "all",
  "projects": "all",
  "per_account_slots": 3,
  "max_workers": 12
}
```

```bash
agy-multiplex-isolated dry-run --manifest manifest.json
```

There is no software maximum for the number of account or project records. `per_account_slots` is also configurable with no hard-coded upper bound; use conservative values that match your machine and provider quota.

## Live gate

Normal live mode is intentionally disabled by default. After completing the documented lab gate:

```bash
agy-multiplex-isolated enable-live --ack "I ACCEPT LAB GATE REQUIREMENTS"
```

Disable it again at any time:

```bash
agy-multiplex-isolated disable-live
```

Enabling the flag is not evidence that the lab gate passed; it is only an explicit local operator acknowledgement.

## Development

```bash
make check
make test
```

GitHub Actions runs syntax, release-hygiene, and unit checks on pushes and pull requests.

## Data and secrets

The repository does not store credentials. Local state defaults to:

```text
~/.agy-multiplex-isolated/
```

Override it with `AGY_MULTIPLEX_HOME`. Docker named volumes hold the account credential stores and are intentionally not deleted by uninstall or normal registry removal.

## License

MIT. See [LICENSE](LICENSE).
