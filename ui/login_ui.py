#!/usr/bin/env python3
from __future__ import annotations

import json, os, platform, secrets, shlex, shutil, subprocess, sys, threading, time
import urllib.parse, webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT))
import multiplex as core
from ui import quota_usage

STATE = core.STATE
UI_STATE_FILE = STATE / "login_ui_state.json"
HTML_FILE = Path(__file__).with_name("login_ui.html")
IMAGE = os.environ.get("AGY_MULTIPLEX_IMAGE", core.DEFAULT_IMAGE)
HOST = "127.0.0.1"
PORT = int(os.environ.get("AGY_LOGIN_UI_PORT", "8765"))
TOKEN = secrets.token_urlsafe(24)
LOCK = threading.Lock()
QUOTA_TTL_SECONDS = max(15, int(os.environ.get("AGY_QUOTA_TTL_SECONDS", "60")))
QUOTA_TIMEOUT_SECONDS = max(8, int(os.environ.get("AGY_QUOTA_TIMEOUT_SECONDS", "25")))
QUOTA_MAX_PARALLEL = max(1, int(os.environ.get("AGY_QUOTA_MAX_PARALLEL", "2")))
QUOTA_RETRY_SECONDS = max(30, int(os.environ.get("AGY_QUOTA_RETRY_SECONDS", "300")))
QUOTA_STALE_SECONDS = max(300, int(os.environ.get("AGY_QUOTA_STALE_SECONDS", "900")))
QUOTA_AUTO_REFRESH = os.environ.get("AGY_QUOTA_AUTO_REFRESH", "false").strip().lower() in {"1", "true", "yes", "on"}
QUOTA_EXECUTOR = ThreadPoolExecutor(max_workers=QUOTA_MAX_PARALLEL, thread_name_prefix="agy-quota")
QUOTA_SUBMIT_SLOTS = threading.BoundedSemaphore(QUOTA_MAX_PARALLEL * 2)
PLAN_CACHE: dict[str, tuple[int, int | None]] = {}

def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data) -> None:
    core.write_json(path, data)


def ui_state() -> dict:
    return read_json(UI_STATE_FILE, {"accounts": {}})


def patch_ui(account: str, **values) -> None:
    with LOCK:
        data = ui_state()
        data.setdefault("accounts", {}).setdefault(account, {}).update(values)
        write_json(UI_STATE_FILE, data)


def recover_ui_state() -> None:
    with LOCK:
        data = ui_state()
        changed = False
        for row in (data.get("accounts") or {}).values():
            if row.get("quota_refreshing"):
                row["quota_refreshing"] = False
                row["quota_status"] = "unavailable"
                row["quota_error"] = "quota_refresh_interrupted"
                row["quota_next_retry_at"] = 0
                changed = True
            if row.get("action") == "verifying":
                row["action"] = "verify_failed"
                row["message"] = "verification_interrupted"
                changed = True
        if changed:
            write_json(UI_STATE_FILE, data)


QUOTA_VOLUME_PREFIX = "agy-quota-home-"


def cleanup_orphan_quota_volumes() -> dict[str, int | bool]:
    """Remove leftover disposable quota credential volumes from crashed UI runs.

    Fail closed: if Docker cannot be queried, nothing is removed. A volume is
    removed only when no running container reports it as mounted.
    """
    try:
        listing = subprocess.run(
            ["docker", "volume", "ls", "--format", "{{.Name}}"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
        )
    except Exception:
        return {"ok": False, "found": 0, "removed": 0, "skipped": 0}
    if listing.returncode != 0:
        return {"ok": False, "found": 0, "removed": 0, "skipped": 0}

    candidates = [
        line.strip() for line in (listing.stdout or "").splitlines()
        if line.strip().startswith(QUOTA_VOLUME_PREFIX)
    ]
    removed = 0
    skipped = 0
    for volume in candidates:
        try:
            mounted = subprocess.run(
                ["docker", "ps", "--filter", f"volume={volume}", "--format", "{{.ID}}"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
            )
        except Exception:
            skipped += 1
            continue
        if mounted.returncode != 0 or (mounted.stdout or "").strip():
            skipped += 1
            continue
        try:
            core.remove_volume(volume)
            removed += 1
        except Exception:
            skipped += 1
    return {"ok": True, "found": len(candidates), "removed": removed, "skipped": skipped}


def docker_snapshot() -> dict:
    command = [
        "docker", "ps", "--no-trunc",
        "--format", '{{.ID}}\t{{.Names}}\t{{.Label "agy.multiplex.account"}}\t{{.Label "agy.multiplex.project"}}\t{{.Label "agy.multiplex.run"}}\t{{.Label "agy.multiplex.kind"}}\t{{.Mounts}}',
    ]
    try:
        p = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=5)
    except Exception:
        return {"ok": False, "rows": [], "accounts": set(), "mounts": ""}
    if p.returncode != 0:
        return {"ok": False, "rows": [], "accounts": set(), "mounts": ""}
    rows = []
    accounts: set[str] = set()
    mounts: list[str] = []
    for line in (p.stdout or "").splitlines():
        parts = line.split("\t", 6)
        if len(parts) != 7:
            continue
        cid, name, account, project, run_id, kind, mounted = parts
        if account:
            accounts.add(account)
        if mounted:
            mounts.append(mounted)
        rows.append({"container": cid, "name": name, "account": account or None,
                     "project": project or None, "run_id": run_id or None,
                     "kind": kind or None, "mounts": mounted})
    return {"ok": True, "rows": rows, "accounts": accounts, "mounts": "\n".join(mounts)}


def account_busy_from_snapshot(account: str, item: dict, snapshot: dict) -> bool:
    if not snapshot.get("ok"):
        return True
    if account in snapshot.get("accounts", set()):
        return True
    volume = str(item.get("volume") or core.master_volume(account))
    return bool(volume and volume in str(snapshot.get("mounts") or ""))


def account_in_use(account: str) -> bool:
    item = (core.account_db().get("accounts") or {}).get(account) or {}
    return account_busy_from_snapshot(account, item, docker_snapshot())


def quota_worker(account: str) -> None:
    clone_volume = f"agy-quota-home-{core.slug(account)}-{secrets.token_hex(6)}"
    try:
        if account_in_use(account):
            patch_ui(account, quota_refreshing=False, quota_status="busy",
                     quota_error="quota_busy", quota_next_retry_at=time.time() + QUOTA_RETRY_SECONDS)
            return
        try:
            lock = core.account_operation_lock(account, blocking=False)
            with lock:
                # Capture binding provenance and clone credentials atomically with respect
                # to login/bind/lane startup. The provider probe then runs on the clone.
                binding = core.require_registered_account(account)
                binding_hash = binding.get("identity_sha256")
                binding_id = binding.get("binding_id")
                binding_generation = int(binding.get("credential_generation") or 0)
                if not binding_hash or not binding_id:
                    raise RuntimeError("quota_requires_reverify")
                core.clone_volume(IMAGE, core.master_volume(account), clone_volume)
        except BlockingIOError:
            patch_ui(account, quota_refreshing=False, quota_status="busy",
                     quota_error="quota_busy", quota_next_retry_at=time.time() + QUOTA_RETRY_SECONDS)
            return
        groups = quota_usage.probe_account_usage(
            account=account, account_volume=clone_volume, image=IMAGE,
            timeout=QUOTA_TIMEOUT_SECONDS,
        )
        current = (core.account_db().get("accounts") or {}).get(account) or {}
        if (current.get("identity_sha256") != binding_hash or
                int(current.get("credential_generation") or 0) != binding_generation):
            patch_ui(account, quota=[], quota_refreshing=False, quota_status="unavailable",
                     quota_error="quota_identity_changed", quota_updated_at=None,
                     quota_identity_sha256=None, quota_generation=None,
                     quota_next_retry_at=time.time() + QUOTA_RETRY_SECONDS)
            return
        patch_ui(account, quota=groups, quota_refreshing=False, quota_status="ok",
                 quota_updated_at=time.time(), quota_error=None, quota_next_retry_at=None,
                 quota_identity_sha256=binding_hash, quota_generation=binding_generation)
    except subprocess.TimeoutExpired:
        patch_ui(account, quota_refreshing=False, quota_status="unavailable",
                 quota_error="quota_timeout", quota_next_retry_at=time.time() + QUOTA_RETRY_SECONDS)
    except Exception as exc:
        text = str(exc)
        known = {"quota_payload_unavailable", "quota_requires_verified_account"}
        reason = text if text in known or text.startswith("agy_usage_exit_") else "quota_unavailable"
        patch_ui(account, quota_refreshing=False, quota_status="unavailable",
                 quota_error=reason, quota_next_retry_at=time.time() + QUOTA_RETRY_SECONDS)
    finally:
        try:
            core.remove_volume(clone_volume)
        except Exception:
            pass
        finally:
            QUOTA_SUBMIT_SLOTS.release()


def maybe_refresh_quota(account: str, *, force: bool = False) -> bool:
    now = time.time()
    if not QUOTA_SUBMIT_SLOTS.acquire(blocking=False):
        if force:
            patch_ui(account, quota_status="deferred", quota_error="quota_queue_busy")
        return False
    should_submit = False
    try:
        with LOCK:
            data = ui_state()
            row = data.setdefault("accounts", {}).setdefault(account, {})
            if row.get("quota_refreshing"):
                return False
            last = float(row.get("quota_updated_at") or 0)
            retry_at = float(row.get("quota_next_retry_at") or 0)
            if not force and ((last and now - last < QUOTA_TTL_SECONDS) or retry_at > now):
                return False
            row["quota_refreshing"] = True
            row["quota_status"] = "loading"
            row["quota_error"] = None
            row["quota_attempted_at"] = now
            write_json(UI_STATE_FILE, data)
            should_submit = True
        try:
            QUOTA_EXECUTOR.submit(quota_worker, account)
        except Exception:
            with LOCK:
                data = ui_state(); row = data.setdefault("accounts", {}).setdefault(account, {})
                row["quota_refreshing"] = False; row["quota_status"] = "unavailable"
                row["quota_error"] = "quota_submit_failed"
                write_json(UI_STATE_FILE, data)
            should_submit = False
            return False
        return True
    finally:
        if not should_submit:
            QUOTA_SUBMIT_SLOTS.release()


def start_quota_refresh(account: str) -> tuple[bool, str]:
    try:
        item = core.require_registered_account(account)
    except Exception as exc:
        return False, str(exc)
    if not item.get("identity_sha256"):
        return False, "quota_requires_verified_account"
    if account_in_use(account):
        return False, "quota_busy"
    if not maybe_refresh_quota(account, force=True):
        current = ui_state().get("accounts", {}).get(account, {})
        if current.get("quota_error") == "quota_queue_busy":
            return False, "quota_refresh_deferred"
        return False, "quota_refresh_in_progress"
    return True, "quota_refresh_started"


def account_rows(snapshot: dict | None = None) -> list[dict]:
    snapshot = snapshot if snapshot is not None else docker_snapshot()
    db = core.account_db().get("accounts", {})
    transient = ui_state().get("accounts", {})
    rows = []
    for account in sorted(db, key=str.casefold):
        item = db.get(account, {})
        tmp = transient.get(account, {})
        enabled = item.get("enabled", True) is not False
        identity_hash = item.get("identity_sha256")
        generation = int(item.get("credential_generation") or 0)
        bound = bool(identity_hash)
        busy = account_busy_from_snapshot(account, item, snapshot)
        matching = [row for row in snapshot.get("rows", []) if row.get("account") == account]
        kinds = {str(row.get("kind") or "busy") for row in matching}
        priority = ("login", "verify", "lane", "quota", "busy")
        busy_kind = next((kind for kind in priority if kind in kinds), None)
        if not snapshot.get("ok"):
            busy_kind = "runtime_unavailable"
        cached_quota = tmp.get("quota") or []
        provenance_ok = bool(cached_quota and bound and
                             tmp.get("quota_identity_sha256") == identity_hash and
                             int(tmp.get("quota_generation") or -1) == generation)
        quota = cached_quota if provenance_ok else []
        provenance_error = bool(cached_quota and not provenance_ok)
        if QUOTA_AUTO_REFRESH and enabled and bound and not busy:
            maybe_refresh_quota(account)
        quota_error = "quota_identity_changed" if provenance_error else tmp.get("quota_error")
        quota_status = ("unavailable" if provenance_error else
                        (tmp.get("quota_status") or ("not_bound" if not bound else "idle")))
        quota_stale = provenance_error or bool(tmp.get("quota_updated_at") and
                            (time.time() - float(tmp.get("quota_updated_at")) > QUOTA_STALE_SECONDS
                             or (quota_error and float(tmp.get("quota_attempted_at") or 0) > float(tmp.get("quota_updated_at") or 0))))
        rows.append({
            "account": account,
            "enabled": enabled,
            "bound": bound,
            "identity": item.get("masked_identity"),
            "verified_at": item.get("verified_at"),
            "busy": busy,
            "busy_kind": busy_kind,
            "runtime_available": bool(snapshot.get("ok")),
            "action": tmp.get("action"),
            "message": tmp.get("message"),
            "quota": quota,
            "quota_status": quota_status,
            "quota_refreshing": bool(tmp.get("quota_refreshing")),
            "quota_updated_at": tmp.get("quota_updated_at") if provenance_ok else None,
            "quota_attempted_at": tmp.get("quota_attempted_at"),
            "quota_error": quota_error,
            "quota_stale": quota_stale,
        })
    return rows


def login_script(account: str) -> Path:
    scripts = STATE / "login_scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    path = scripts / f"login-{core.slug(account)}.sh"
    cmd = [sys.executable, str(CODE_ROOT / "multiplex.py"),
           "--image", IMAGE, "login", account]
    quoted = " ".join(shlex.quote(x) for x in cmd)
    text = (
        "#!/bin/sh\n"
        f"printf '%s\\n' {shlex.quote('AGY isolated login — ' + account)}\n"
        "printf '%s\\n' 'Complete Google authentication, then exit agy.'\n"
        f"exec {quoted}\n"
    )
    path.write_text(text, encoding="utf-8")
    path.chmod(0o700)
    return path


def open_terminal(script: Path) -> tuple[bool, str]:
    system = platform.system()
    if system == "Darwin":
        subprocess.Popen(["open", "-a", "Terminal", str(script)])
        return True, "terminal_opened"
    if system == "Linux":
        choices = [
            ("x-terminal-emulator", ["x-terminal-emulator", "-e", "sh", str(script)]),
            ("gnome-terminal", ["gnome-terminal", "--", "sh", str(script)]),
            ("konsole", ["konsole", "-e", "sh", str(script)]),
            ("xfce4-terminal", ["xfce4-terminal", "--command", f"sh {shlex.quote(str(script))}"]),
        ]
        for exe, command in choices:
            if shutil.which(exe):
                subprocess.Popen(command)
                return True, "terminal_opened"
    return False, f"terminal_unavailable:{script}"

def add_account(account: str) -> tuple[bool, str]:
    try:
        core.add_account(account)
        patch_ui(account, action="added", message="account_added")
        return True, "account_added"
    except Exception as exc:
        return False, str(exc)


def toggle_account(account: str) -> tuple[bool, str]:
    try:
        core.require_registered_account(account)
    except Exception as exc:
        return False, str(exc)
    if account_in_use(account):
        return False, "account_in_use"
    try:
        enabled = core.toggle_account_enabled(account)
    except Exception as exc:
        return False, str(exc)
    patch_ui(account, action="enabled" if enabled else "disabled",
             message="account_enabled" if enabled else "account_disabled")
    return True, "account_enabled" if enabled else "account_disabled"


def start_login(account: str) -> tuple[bool, str]:
    try:
        core.require_registered_account(account)
    except Exception as exc:
        return False, str(exc)
    if account_in_use(account):
        return False, "account_in_use"
    script = login_script(account)
    ok, code = open_terminal(script)
    if ok:
        patch_ui(account, action="login_opened", message="login_opened", quota=[],
                 quota_status="not_bound", quota_refreshing=False, quota_updated_at=None,
                 quota_error=None, quota_identity_sha256=None, quota_generation=None,
                 quota_next_retry_at=None)
    return ok, code


def verify_worker(account: str) -> None:
    patch_ui(account, action="verifying", message="verifying")
    try:
        command = [sys.executable, str(CODE_ROOT / "multiplex.py"),
                   "--image", IMAGE, "bind", account]
        p = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT)
        output = (p.stdout or "").strip()
        if p.returncode == 0:
            patch_ui(account, action="verified", message="verified", quota=[],
                     quota_status="idle", quota_refreshing=False, quota_updated_at=None,
                     quota_error=None, quota_identity_sha256=None, quota_generation=None,
                     quota_next_retry_at=None)
            if QUOTA_AUTO_REFRESH:
                maybe_refresh_quota(account, force=True)
            return
        reason = "verify_failed"
        try:
            data = json.loads(output); reason = data.get("reason") or data.get("error") or reason
        except Exception:
            pass
        patch_ui(account, action="verify_failed", message=reason)
    except Exception as exc:
        patch_ui(account, action="verify_failed", message=f"verify_error:{exc}")

def start_verify(account: str) -> tuple[bool, str]:
    try:
        core.require_registered_account(account)
    except Exception as exc:
        return False, str(exc)
    if account_in_use(account):
        return False, "account_in_use"
    current = ui_state().get("accounts", {}).get(account, {})
    if current.get("action") == "verifying":
        return False, "verification_in_progress"
    threading.Thread(target=verify_worker, args=(account,), daemon=True).start()
    return True, "verification_started"


def active_lane_snapshot(snapshot: dict | None = None) -> list[dict]:
    snapshot = snapshot if snapshot is not None else docker_snapshot()
    if not snapshot.get("ok"):
        return []
    rows = []
    for item in snapshot.get("rows", []):
        if not item.get("account"):
            continue
        if item.get("kind") != "lane" and not (item.get("project") and item.get("run_id")):
            continue
        rows.append({"container": item.get("container"), "account": item.get("account"),
                     "project": item.get("project"), "run_id": item.get("run_id")})
    return rows


def plan_task_count(raw_path: object) -> int | None:
    try:
        path = Path(str(raw_path)).expanduser().resolve()
        mtime = path.stat().st_mtime_ns
    except Exception:
        return None
    key = str(path)
    cached = PLAN_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        count = len(core.load_plan(path))
    except Exception:
        count = None
    PLAN_CACHE[key] = (mtime, count)
    return count


def project_rows(lanes: list[dict] | None = None) -> list[dict]:
    lanes = lanes if lanes is not None else active_lane_snapshot()
    active_by_project: dict[str, list[dict]] = {}
    for lane in lanes:
        project = lane.get("project")
        if project:
            active_by_project.setdefault(str(project), []).append(lane)
    rows = []
    db = core.project_db().get("projects") or {}
    for name in sorted(db, key=str.casefold):
        item = db[name]
        enabled = item.get("enabled", True) is not False
        running = active_by_project.get(name, [])
        tasks = item.get("task_count")
        if tasks is None:
            tasks = plan_task_count(item.get("plan"))
        rows.append({
            "name": name, "enabled": enabled, "repo": item.get("repo"),
            "plan": item.get("plan"), "goal": item.get("goal") or "",
            "model": item.get("model"), "created_at": item.get("created_at"),
            "task_count": tasks, "active_lanes": len(running),
            "accounts": sorted({str(x.get("account")) for x in running if x.get("account")}, key=str.casefold),
            "status": "running" if running else ("ready" if enabled else "disabled"),
        })
    return rows


def active_run_config(lanes: list[dict], *, fallback_capacity: int, default_slots: int) -> dict:
    run_ids = sorted({str(x.get("run_id")) for x in lanes if x.get("run_id")})
    configs = []
    for run_id in run_ids:
        path = core.RUNS / run_id / "manifest.effective.json"
        try:
            data = core.read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        try:
            capacity = max(1, int(data.get("max_workers")))
            slots = max(1, int(data.get("per_account_slots")))
        except (TypeError, ValueError):
            continue
        configs.append({"run_id": run_id, "capacity": capacity, "slots": slots})
    if not configs:
        return {"capacity": fallback_capacity, "per_account_slots": default_slots,
                "capacity_kind": "verified_default", "active_runs": []}
    slot_values = {x["slots"] for x in configs}
    return {"capacity": sum(x["capacity"] for x in configs),
            "per_account_slots": next(iter(slot_values)) if len(slot_values) == 1 else None,
            "capacity_kind": "active_run",
            "active_runs": [x["run_id"] for x in configs]}


def dashboard_payload() -> dict:
    snapshot = docker_snapshot()
    accounts = account_rows(snapshot)
    lanes = active_lane_snapshot(snapshot)
    projects = project_rows(lanes)
    enabled_accounts = [x for x in accounts if x.get("enabled")]
    verified = [x for x in enabled_accounts if x.get("bound")]
    enabled_projects = [x for x in projects if x.get("enabled")]
    default_slots = core.DEFAULT_SLOTS
    verified_default_capacity = len(verified) * default_slots
    config = active_run_config(
        lanes, fallback_capacity=verified_default_capacity, default_slots=default_slots
    )
    lane_counts: dict[str, int] = {}
    for lane in lanes:
        account = str(lane.get("account") or "")
        if account:
            lane_counts[account] = lane_counts.get(account, 0) + 1
    runtime_ok = bool(snapshot.get("ok"))
    logical_capacity = int(config["capacity"])
    return {
        "accounts": accounts,
        "projects": projects,
        "stats": {
            "total_accounts": len(accounts),
            "verified_accounts": len(verified),
            "active_projects": len(enabled_projects),
            "enabled_projects": len(enabled_projects),
            "logical_capacity": logical_capacity,
            "verified_default_capacity": verified_default_capacity,
        },
        "scheduler": {
            "status": "unavailable" if not runtime_ok else ("running" if lanes else "idle"),
            "runtime_available": runtime_ok,
            "mode": "round_robin",
            "per_account_slots": config["per_account_slots"],
            "capacity_kind": config["capacity_kind"],
            "capacity": logical_capacity,
            "active_runs": config["active_runs"],
            "active_accounts": len(lane_counts),
            "enabled_accounts": len(enabled_accounts),
            "verified_accounts": len(verified),
            "active_projects": len({x.get("project") for x in lanes if x.get("project")}),
            "enabled_projects": len(enabled_projects),
            "active_lanes": len(lanes),
            "account_lanes": lane_counts,
        },
        "image": IMAGE,
        "data_root": str(core.DATA_ROOT),
        "quota_auto_refresh": QUOTA_AUTO_REFRESH,
    }


def add_project_from_ui(data: dict) -> tuple[bool, str]:
    try:
        name = str(data.get("name") or "").strip()
        repo = str(data.get("repo") or "").strip()
        plan = str(data.get("plan") or "").strip()
        goal = str(data.get("goal") or "").strip()
        model = str(data.get("model") or "").strip() or None
        project = core.add_project(name, repo, plan, goal=goal, model=model)
        return True, f"project_added:{project.name}"
    except Exception as exc:
        return False, str(exc)


def toggle_project(name: str) -> tuple[bool, str]:
    try:
        enabled = core.toggle_project_enabled(name)
    except Exception as exc:
        return False, str(exc)
    return True, "project_enabled" if enabled else "project_disabled"


class Handler(BaseHTTPRequestHandler):
    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def _token_ok(self) -> bool:
        supplied = self.headers.get("X-UI-Token", "")
        return secrets.compare_digest(supplied, TOKEN)

    def _body_json(self) -> dict:
        length = min(int(self.headers.get("Content-Length", "0") or 0), 4096)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            body = HTML_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'; connect-src 'self'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if parsed.path == "/api/status" and self._token_ok():
            self._json(dashboard_payload()); return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._token_ok():
            self._json({"ok": False, "message": "unauthorized"}, 403); return
        parsed = urllib.parse.urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if parts == ["api", "add"]:
            try:
                account = str(self._body_json().get("account") or "").strip()
                ok, msg = add_account(account)
            except Exception as exc:
                ok, msg = False, str(exc)
            self._json({"ok": ok, "message": msg}, 200 if ok else 409); return
        if parts == ["api", "project-add"]:
            try:
                ok, msg = add_project_from_ui(self._body_json())
            except Exception as exc:
                ok, msg = False, str(exc)
            self._json({"ok": ok, "message": msg}, 200 if ok else 409); return
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "project-toggle":
            ok, msg = toggle_project(urllib.parse.unquote(parts[2]))
            self._json({"ok": ok, "message": msg}, 200 if ok else 409); return
        if len(parts) != 3 or parts[0] != "api":
            self._json({"ok": False, "message": "not_found"}, 404); return
        action, account = parts[1], urllib.parse.unquote(parts[2])
        if action == "login":
            ok, msg = start_login(account)
        elif action == "verify":
            ok, msg = start_verify(account)
        elif action == "quota":
            ok, msg = start_quota_refresh(account)
        elif action == "toggle":
            ok, msg = toggle_account(account)
        else:
            self._json({"ok": False, "message": "unknown_action"}, 404); return
        self._json({"ok": ok, "message": msg}, 200 if ok else 409)

    def log_message(self, *_):
        return

def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    recover_ui_state()
    cleanup_orphan_quota_volumes()
    base_url = f"http://{HOST}:{PORT}/"
    url = f"{base_url}#token={urllib.parse.quote(TOKEN)}"
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"AGY Account Login UI: {base_url} (local token opened in browser fragment)", flush=True)
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        QUOTA_EXECUTOR.shutdown(wait=False, cancel_futures=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
