#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = [
    "README.md", "README_AR.md", "LICENSE", ".gitignore", "SECURITY.md",
    "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "LAB_GATE.md",
    "docker/Dockerfile", "multiplex.py", "ui/login_ui.py", "ui/login_ui.html",
    ".github/workflows/ci.yml",
]

errors: list[str] = []
for rel in REQUIRED:
    if not (ROOT / rel).is_file():
        errors.append(f"missing required file: {rel}")

ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
for required in ("state/", "runs/", ".env"):
    if required not in ignore:
        errors.append(f".gitignore missing {required}")

text_extensions = {".py", ".md", ".json", ".yml", ".yaml", ".sh", ".html", ".txt", ""}
for path in ROOT.rglob("*"):
    if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts or any(x in path.parts for x in {"state", "runs", ".venv"}):
        continue
    if path.suffix.lower() not in text_extensions:
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        continue
    rel = path.relative_to(ROOT)
    bad_mac = "/Users/" + "ali-" + "laptop"
    bad_win = "C:" + "\\Users\\" + "user"
    if bad_mac in text or bad_win in text:
        errors.append(f"machine-specific path in {rel}")

core = (ROOT / "multiplex.py").read_text(encoding="utf-8")
for forbidden in ("DEFAULT_ACCOUNTS", "range(1, 6)"):
    if forbidden in core:
        errors.append(f"fixed account-pool marker remains: {forbidden}")

for rel in ("examples/manifest.example.json", "examples/plan.example.json"):
    try:
        json.loads((ROOT / rel).read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append(f"invalid JSON in {rel}: {exc}")

if errors:
    print("RELEASE CHECK FAILED")
    for error in errors:
        print(f"- {error}")
    raise SystemExit(1)

print("RELEASE CHECK OK")
print("- no fixed five-account fallback")
print("- runtime state excluded")
print("- no local machine path detected")
print("- required public repository files present")
