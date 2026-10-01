from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class RouteChoice:
    account: str
    slot: int
    model: str | None
    family: str
    remaining_percent: float | None
    cursor: int


def model_family(model: str | None) -> str:
    text = (model or "").casefold()
    if "gemini" in text:
        return "gemini"
    if "claude" in text or "gpt" in text:
        return "claude_gpt"
    cleaned = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return cleaned or "default"
def normalize_models(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = [x.strip() for x in value.split(",")]
    elif isinstance(value, (list, tuple)):
        raw = [str(x).strip() for x in value]
    else:
        raise ValueError("models must be a list or comma-separated string")
    out: list[str] = []
    seen: set[str] = set()
    for model in raw:
        if not model:
            continue
        key = model.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(model)
    return out


def task_models(project_models: Iterable[str], task: dict[str, Any]) -> list[str | None]:
    task_list = normalize_models(task.get("models"))
    legacy = str(task.get("model") or "").strip()
    if legacy and legacy.casefold() not in {x.casefold() for x in task_list}:
        task_list.insert(0, legacy)
    models = task_list or normalize_models(list(project_models))
    return models or [None]
def quota_remaining(groups: list[dict[str, Any]] | None, model: str | None) -> float | None:
    if not groups:
        return None
    family = model_family(model)
    family_matches: list[float] = []
    for group in groups:
        gid = str(group.get("id") or "").casefold()
        label = str(group.get("label") or "").casefold()
        if gid != family:
            if family == "gemini" and "gemini" not in label:
                continue
            if family == "claude_gpt" and not ("claude" in label or "gpt" in label):
                continue
            if family not in {"gemini", "claude_gpt"}:
                continue
        values: list[float] = []
        for window in (group.get("windows") or {}).values():
            raw = window.get("remaining_percent") if isinstance(window, dict) else None
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if 0.0 <= value <= 100.0:
                values.append(value)
        if values:
            family_matches.append(min(values))
    return min(family_matches) if family_matches else None


def choose_route(
    *,
    accounts: list[str],
    slots: int,
    active_pairs: set[tuple[str, int]],
    models: list[str | None],
    quota_matrix: dict[str, list[dict[str, Any]]],
    exclusions: set[tuple[str, str]],
    blocked_models: set[str],
    min_remaining_percent: float,
    cursor: int,
) -> RouteChoice | None:
    free = [(a, s) for a in accounts for s in range(1, slots + 1) if (a, s) not in active_pairs]
    if not free:
        return None
    for model in models:
        model_key = (model or "").casefold()
        if model_key and model_key in blocked_models:
            continue
        candidates: list[tuple[float, int, str, int, float | None]] = []
        for idx, (account, slot) in enumerate(free):
            if (account, model_key) in exclusions:
                continue
            remaining = quota_remaining(quota_matrix.get(account), model)
            if remaining is not None and remaining <= min_remaining_percent:
                continue
            known_score = remaining if remaining is not None else -1.0
            rotation = (idx - cursor) % max(1, len(free))
            candidates.append((known_score, -rotation, account, slot, remaining))
        if not candidates:
            continue
        candidates.sort(reverse=True)
        _score, _rotation, account, slot, remaining = candidates[0]
        next_cursor = (cursor + 1) % max(1, len(free))
        return RouteChoice(
            account=account,
            slot=slot,
            model=model,
            family=model_family(model),
            remaining_percent=remaining,
            cursor=next_cursor,
        )
    return None


def route_failure_kind(text: str) -> str | None:
    low = (text or "").casefold()
    quota_markers = (
        "resource_exhausted",
        "individual quota reached",
        "quota reached",
        "too many requests",
        "error_code\":429",
        "code 429",
    )
    if any(marker in low for marker in quota_markers):
        return "quota_exhausted"
    model_markers = (
        "model not found",
        "requested model",
        "model is unavailable",
        "unsupported model",
        "unknown model",
    )
    if any(marker in low for marker in model_markers):
        return "model_unavailable"
    return None
