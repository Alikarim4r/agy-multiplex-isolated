# Contributing

Contributions are welcome, especially around portability, test coverage, observability, and safer account-boundary validation.

## Development flow

1. Fork the repository and create a focused branch.
2. Keep runtime state and credentials outside the repository.
3. Run `make check` and `make test` before opening a pull request.
4. Do not weaken the default live gate, identity verification, write-scope enforcement, or no-push/no-deploy guarantees without explicit security review.
5. Add or update tests for behavioral changes.

## Pull requests

Describe the problem, the proposed change, the test evidence, and any security implications. Changes to authentication, credential storage, Docker mounts, process isolation, Git lifecycle, or live-gate behavior should include a threat-model note.

Do not attach tokens, credential exports, unmasked account emails, or private repository content to issues or pull requests.
