from __future__ import annotations

import json
import math
import re
import subprocess
import uuid
from typing import Any


def _json_object_from_output(text: str) -> dict[str, Any] | None:
    """Accept only a complete JSON object; never scan arbitrary log text."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _nested_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(\{.*\})\s*```", text, re.I | re.S)
    if fenced:
        text = fenced.group(1)
    if not (text.startswith("{") and text.endswith("}")):
        return value
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return value
    return decoded

def _find_quota_groups(value: Any) -> list[dict[str, Any]] | None:
    value = _nested_json(value)
    if isinstance(value, dict):
        groups = value.get("groups")
        if isinstance(groups, list) and any(
            isinstance(group, dict) and isinstance(group.get("buckets"), list)
            for group in groups
        ):
            return groups
        for child in value.values():
            found = _find_quota_groups(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_quota_groups(child)
            if found is not None:
                return found
    return None


def _pick(mapping: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return None


def _group_id(label: str) -> str:
    low = label.casefold()
    if "gemini" in low:
        return "gemini"
    if "claude" in low or "gpt" in low or "3p" in low:
        return "claude_gpt"
    cleaned = "-".join(label.casefold().split())
    return cleaned[:60] or "quota"

def _window_key(marker: str) -> str | None:
    marker = marker.casefold().strip()
    if re.search(r"(?:^|[^a-z0-9])(?:weekly|week|7d|7[ _-]?days?|168h)(?:$|[^a-z0-9])", marker):
        return "weekly"
    if re.search(r"(?:^|[^a-z0-9])(?:5h|5[ _-]?hours?|five[ _-]?hours?)(?:$|[^a-z0-9])", marker):
        return "five_hour"
    return None


def _remaining_fraction(raw: Any) -> float | None:
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        return None
    return value


def _quota_window(remaining: float, reset_time: Any) -> dict[str, Any]:
    return {
        "used_percent": round((1.0 - remaining) * 100.0, 1),
        "remaining_percent": round(remaining * 100.0, 1),
        "reset_time": reset_time,
    }


def _parse_tsv_response(response: str) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for raw_line in response.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        cols = line.split("\t")
        if len(cols) != 4:
            continue
        label, window_label, remaining_text, reset_time = (x.strip() for x in cols)
        normalized = window_label.casefold()
        if normalized == "five hour limit remaining":
            key = "five_hour"
        elif normalized == "weekly limit remaining":
            key = "weekly"
        else:
            continue
        match = re.fullmatch(r"(100(?:\.0+)?|(?:\d{1,2})(?:\.\d+)?)%", remaining_text)
        if not match:
            continue
        remaining_percent = float(match.group(1))
        if not math.isfinite(remaining_percent) or not 0.0 <= remaining_percent <= 100.0:
            continue
        if label not in groups:
            groups[label] = {"id": _group_id(label), "label": label, "windows": {}}
            order.append(label)
        candidate = _quota_window(remaining_percent / 100.0, reset_time or None)
        previous = groups[label]["windows"].get(key)
        if previous is None or candidate["remaining_percent"] < previous["remaining_percent"]:
            groups[label]["windows"][key] = candidate
    return [groups[label] for label in order if groups[label]["windows"]]


def _parse_bucket_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    groups = _find_quota_groups(payload)
    if not groups:
        return []
    parsed: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        label = str(_pick(group, "display_name", "displayName", "name") or "Quota")
        windows: dict[str, dict[str, Any]] = {}
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict):
                continue
            window_raw = str(_pick(bucket, "window") or "").strip()
            bucket_raw = str(_pick(bucket, "bucket_id", "bucketId") or "").strip()
            if window_raw:
                # An explicit window is authoritative. Unknown explicit windows are
                # rejected rather than guessed from a bucket name such as "*-5h".
                key = _window_key(window_raw)
                if not key:
                    continue
                bucket_key = _window_key(bucket_raw) if bucket_raw else None
                if bucket_key and bucket_key != key:
                    continue
            else:
                key = _window_key(bucket_raw)
                if not key:
                    continue
            remaining = _remaining_fraction(_pick(bucket, "remaining_fraction", "remainingFraction"))
            if remaining is None:
                continue
            candidate = _quota_window(remaining, _pick(bucket, "reset_time", "resetTime"))
            previous = windows.get(key)
            if previous is None or candidate["remaining_percent"] < previous["remaining_percent"]:
                windows[key] = candidate
        if windows:
            parsed.append({"id": _group_id(label), "label": label, "windows": windows})
    return parsed


def parse_usage_output(text: str) -> list[dict[str, Any]]:
    payload = _json_object_from_output(text)
    if payload is None:
        return []
    response = payload.get("response")
    if isinstance(response, str):
        parsed = _parse_tsv_response(response)
        if parsed:
            return parsed
    return _parse_bucket_payload(payload)


def validate_usage_envelope(text: str) -> None:
    payload = _json_object_from_output(text)
    if payload is None:
        raise RuntimeError("quota_payload_unavailable")
    status = str(payload.get("status") or "").upper()
    if status and status != "SUCCESS":
        raise RuntimeError("quota_command_failed")
    usage = payload.get("usage")
    if isinstance(usage, dict):
        for key in ("input_tokens", "output_tokens", "thinking_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, bool):
                raise RuntimeError("quota_usage_metadata_invalid")
            if isinstance(value, (int, float)) and value != 0:
                raise RuntimeError("quota_command_consumed_tokens")


def probe_account_usage(*, account: str, account_volume: str, image: str,
                        timeout: int = 25) -> list[dict[str, Any]]:
    container_name = f"agy-quota-{uuid.uuid4().hex[:12]}"
    command = [
        "docker", "run", "--rm", "--name", container_name, "-i",
        "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--label", f"agy.multiplex.account={account}",
        "--label", "agy.multiplex.kind=quota",
        "-e", "AGY_CLI_DISABLE_AUTO_UPDATE=true",
        "-v", f"{account_volume}:/home/agy",
        image, "agy", "-p", "/usage",
        "--output-format", "json",
        "--print-timeout", f"{max(3, timeout - 5)}s",
        "--log-file", "/tmp/agy-usage.log",
    ]
    try:
        result = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        )
        raise
    if result.returncode != 0:
        raise RuntimeError(f"agy_usage_exit_{result.returncode}")
    validate_usage_envelope(result.stdout or "")
    groups = parse_usage_output(result.stdout or "")
    if not groups:
        raise RuntimeError("quota_payload_unavailable")
    return groups
