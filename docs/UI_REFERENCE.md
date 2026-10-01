# UI Visual Reference

The canonical visual reference for the dashboard is the generated mockup from the design conversation on 2026-10-01.

- Source filename: `agy_multiplex_dashboard_interface.png`
- Dimensions: `1672 × 941`
- SHA-256: `8480f7923c1336ce95f860e294a8449f1e8c9db3cd3ec341bbcb66059943404c`
- In-repository layout map: [`assets/agy-dashboard-reference.svg`](assets/agy-dashboard-reference.svg)

The PNG is the visual reference; the SVG is a lightweight layout map for reviewers and CI-friendly documentation. The implementation must not invent quota values to imitate the mockup.

## Non-negotiable visual structure

1. Dark navy engineering-dashboard shell with a left navigation rail.
2. Top bar contains product identity, Arabic/English switch, and `Local only · 127.0.0.1` state.
3. Four top summary cards: total accounts, verified accounts, active projects, logical lane capacity.
4. Account management is the primary panel and uses a responsive card grid.
5. Every verified account can show two usage windows: `5h Rolling` and `7d Weekly`, each with percentage bar and reset time when available.
6. Account cards expose Sign in, Verify, Refresh usage, and Enable/Disable actions.
7. Active Projects appears as a table/list beneath the account area and remains dynamically sized.
8. Fleet Scheduler appears alongside projects and shows dynamic pool state, slots/account, active lanes, capacity, and per-account lane occupancy.
9. Arabic and English labels coexist cleanly; switching language changes document direction RTL/LTR.
10. Responsive layouts may stack panels, but must preserve the information hierarchy above.

## Functional truth beats mock data

The reference image contains illustrative numbers. Production UI must render repository/runtime truth only. Missing quota becomes `Unavailable`/`—`; idle scheduler becomes `Idle`; no project/account count is hard-coded.

## Review acceptance

A reviewer should reject a UI change if it removes the quota windows, dynamic account/project behavior, local-only indicator, summary cards, project panel, scheduler panel, bilingual switch, or weakens the security boundary to achieve closer visual similarity.
