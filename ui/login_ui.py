#!/usr/bin/env python3
from __future__ import annotations

import json, os, platform, secrets, shlex, shutil, subprocess, sys, threading
import urllib.parse, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_ROOT))
import multiplex as core

STATE = core.STATE
UI_STATE_FILE = STATE / "login_ui_state.json"
HTML_FILE = Path(__file__).with_name("login_ui.html")
IMAGE = os.environ.get("AGY_MULTIPLEX_IMAGE", core.DEFAULT_IMAGE)
HOST = "127.0.0.1"
PORT = int(os.environ.get("AGY_LOGIN_UI_PORT", "8765"))
TOKEN = secrets.token_urlsafe(24)
LOCK = threading.Lock()

def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def ui_state() -> dict:
    return read_json(UI_STATE_FILE, {"accounts": {}})


def patch_ui(account: str, **values) -> None:
    with LOCK:
        data = ui_state()
        data.setdefault("accounts", {}).setdefault(account, {}).update(values)
        write_json(UI_STATE_FILE, data)

def volume_busy(account: str) -> bool:
    p = subprocess.run(
        ["docker", "ps", "-q", "--filter", f"volume={core.master_volume(account)}"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    return bool((p.stdout or "").strip())


def account_rows() -> list[dict]:
    db = core.account_db().get("accounts", {})
    transient = ui_state().get("accounts", {})
    rows = []
    for account in sorted(db, key=str.casefold):
        item = db.get(account, {})
        tmp = transient.get(account, {})
        rows.append({
            "account": account,
            "enabled": item.get("enabled", True) is not False,
            "bound": bool(item.get("identity_sha256")),
            "identity": item.get("masked_identity"),
            "verified_at": item.get("verified_at"),
            "busy": volume_busy(account),
            "action": tmp.get("action"),
            "message": tmp.get("message"),
        })
    return rows


def login_script(account: str) -> Path:
    scripts = STATE / "login_scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    path = scripts / f"login-{core.slug(account)}.sh"
    workspace = core.DATA_ROOT / "login-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    cmd = [
        "docker", "run", "--rm", "-it", "--name", f"agy-login-{core.slug(account)}",
        "-v", f"{core.master_volume(account)}:/home/agy",
        "-v", f"{workspace}:/workspace", "-w", "/workspace", IMAGE, "agy",
    ]
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
    db = core.account_db(); item = (db.get("accounts") or {}).get(account)
    if item is None:
        return False, "unknown_account"
    enabled = item.get("enabled", True) is not False
    core.set_account_enabled(account, not enabled)
    patch_ui(account, action="enabled" if not enabled else "disabled",
             message="account_enabled" if not enabled else "account_disabled")
    return True, "account_enabled" if not enabled else "account_disabled"


def start_login(account: str) -> tuple[bool, str]:
    try:
        core.require_registered_account(account)
    except Exception as exc:
        return False, str(exc)
    if volume_busy(account):
        return False, "login_already_open"
    script = login_script(account)
    ok, code = open_terminal(script)
    if ok:
        patch_ui(account, action="login_opened", message="login_opened")
    return ok, code


def verify_worker(account: str) -> None:
    patch_ui(account, action="verifying", message="verifying")
    try:
        command = [sys.executable, str(CODE_ROOT / "multiplex.py"),
                   "--image", IMAGE, "bind", account]
        p = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=110)
        output = (p.stdout or "").strip()
        if p.returncode == 0:
            patch_ui(account, action="verified", message="verified")
            return
        reason = "verify_failed"
        try:
            data = json.loads(output); reason = data.get("reason") or data.get("error") or reason
        except Exception:
            pass
        patch_ui(account, action="verify_failed", message=reason)
    except subprocess.TimeoutExpired:
        patch_ui(account, action="verify_failed", message="verify_timeout")
    except Exception as exc:
        patch_ui(account, action="verify_failed", message=f"verify_error:{exc}")

def start_verify(account: str) -> tuple[bool, str]:
    try:
        core.require_registered_account(account)
    except Exception as exc:
        return False, str(exc)
    if volume_busy(account):
        return False, "close_login_first"
    current = ui_state().get("accounts", {}).get(account, {})
    if current.get("action") == "verifying":
        return False, "verification_in_progress"
    threading.Thread(target=verify_worker, args=(account,), daemon=True).start()
    return True, "verification_started"


class Handler(BaseHTTPRequestHandler):
    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def _token_ok(self) -> bool:
        header = self.headers.get("X-UI-Token", "")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        supplied = header or (query.get("token", [""])[0])
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
            self._json({"accounts": account_rows(), "image": IMAGE,
                        "data_root": str(core.DATA_ROOT)}); return
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
        if len(parts) != 3 or parts[0] != "api":
            self._json({"ok": False, "message": "not_found"}, 404); return
        action, account = parts[1], urllib.parse.unquote(parts[2])
        if action == "login":
            ok, msg = start_login(account)
        elif action == "verify":
            ok, msg = start_verify(account)
        elif action == "toggle":
            ok, msg = toggle_account(account)
        else:
            self._json({"ok": False, "message": "unknown_action"}, 404); return
        self._json({"ok": ok, "message": msg}, 200 if ok else 409)

    def log_message(self, *_):
        return

def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    url = f"http://{HOST}:{PORT}/?token={urllib.parse.quote(TOKEN)}"
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"AGY Account Login UI: {url}", flush=True)
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
