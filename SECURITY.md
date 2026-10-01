# Security Policy and Model

## Security boundary

Each configured Antigravity account is authenticated into its own Docker named volume. Execution never uses that master volume directly: a lane receives a disposable clone and runs one `agy` process tree against the clone.

Different accounts therefore do not share a credential store, and concurrent lanes assigned to the same account do not write to the same token file.

The host HOME, Apple Keychain, and Antigravity Manager account-switching state are not mounted into execution containers.

Every completed lane must emit authenticated identity evidence matching the SHA-256 identity hash bound to its account. A missing or foreign identity rejects the lane and prevents integration.

The scheduler owns Git lifecycle. Agent permission profiles deny Git lifecycle commands and common deployment/network tooling, and lane changes outside the declared `write_scope` are rejected.

## Credential handling

- OAuth credentials are stored in Docker volumes, not repository files.
- Account registry state stores only metadata, a masked email, and an identity hash.
- Runtime state defaults to `~/.agy-multiplex-isolated` and is outside the repository.
- The account UI binds only to `127.0.0.1` and requires a random token generated for each server launch.
- Account removal from the registry does **not** delete the credential volume automatically.
- Uninstall does **not** delete credential volumes or runtime state.

Never commit exported Docker volumes, token files, `.env` files, login evidence, or runtime state.

## Known alpha risk

The open validation question is whether multiple disposable clones derived from the same OAuth state can refresh independently for long-running concurrent lanes. Static isolation does not prove provider-side refresh-token behavior.

For this reason, normal live mode is disabled by default. Complete [LAB_GATE.md](LAB_GATE.md) on the exact pinned `agy` version before production use.

## Supported security posture

This project does not attempt to bypass provider authentication, quota, billing, eligibility checks, or rate limits. The scheduler should fail closed when account identity cannot be established.

Do not use the project to conceal unauthorized account sharing or to evade service restrictions. Operators are responsible for accounts they are authorized to use and for applicable provider terms.

## Reporting a vulnerability

For a public GitHub repository, use GitHub's private security advisory feature instead of opening a public issue for credential exposure, authentication bypass, command execution, or cross-account identity problems.

Include the affected version, host OS, Docker version, `agy` version, reproduction steps, and redacted evidence. Never include tokens or unmasked credentials in a report.
