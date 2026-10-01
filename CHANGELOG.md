# Changelog

## 0.1.0-alpha — 2026-10-01

- Replaced the fixed five-account assumption with a dynamic account registry.
- Added a dynamic project registry with optional manifest-free operation.
- Added configurable per-account slots and global worker caps without fixed pool cardinality.
- Added bilingual Arabic/English account login UI with dynamic account creation.
- Added 5-hour and weekly usage meters from `agy -p /usage --output-format json`, with separate Gemini and Claude/GPT quota groups when available.
- Moved runtime state outside the repository by default.
- Added portable host UID/GID Docker builds and macOS/Linux terminal adapters.
- Added release hygiene checks, unit tests, GitHub Actions CI, public documentation, and MIT license.
- Preserved the fail-closed live gate while OAuth clone refresh behavior remains under lab validation.
