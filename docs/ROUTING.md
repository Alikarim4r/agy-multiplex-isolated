# Quota-Aware Account × Model Routing

Quota-aware routing is a core scheduler requirement, not an optional UI feature.
The scheduler chooses an eligible **account + model** pair for every lane.

## Priority

Model candidates are ordered from highest preferred quality to fallback models.
A project may keep the legacy single `model` field, or define an ordered `models` list.
A task may override the project with its own `model` or `models` values.

Example priority:

```json
{
  "model": "claude-opus-4-6-thinking",
  "models": [
    "claude-opus-4-6-thinking",
    "gemini-3.8-flash-high",
    "gemini-3.1-pro-high"
  ]
}
```
## Quota preflight

Before a run, the scheduler probes `/usage` through a disposable clone of each
selected account HOME. Raw credentials are never emitted. The stored run evidence
contains only account IDs, quota-family percentages, reset-independent routing
metadata, and probe errors.

The pinned CLI currently reports quota at provider-family level (for example
`Gemini Models` and `Claude and GPT models`). Therefore preflight routing uses the
most constrained remaining percentage across the observed 5-hour and weekly
windows for that family.

A model-specific provider failure is still handled exactly at runtime: a 429 or
`RESOURCE_EXHAUSTED` excludes only that account/model pair for the task and the
scheduler retries elsewhere.

Unknown quota is not treated as invented capacity. Known healthy routes are
preferred; an unknown route can be used only when no better known route exists.
## Automatic failover

For each preferred model, the scheduler chooses an account with an eligible free
slot and the strongest known quota headroom. If the model family is exhausted on
one account, that account is skipped while the same model is tried elsewhere.
If the preferred model has no eligible account, the next configured model is used.

When a lane exits because of quota exhaustion, the task is not immediately marked
failed. The scheduler records `LANE_ROUTE_RETRY`, excludes the failed route, and
starts a fresh isolated attempt from the current project staging head. Model-not-
available errors block that model for the project and trigger the next fallback.

A configurable retry ceiling prevents infinite failover loops. Non-routing errors
continue to fail closed under the normal lane validation rules.

## Manifest policy

```json
{
  "routing": {
    "quota_aware": true,
    "min_remaining_percent": 1,
    "max_route_attempts": 12,
    "quota_timeout_seconds": 25,
    "quota_probe_workers": 2
  }
}
```
