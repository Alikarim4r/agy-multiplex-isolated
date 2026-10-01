from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ui import login_ui


class DashboardPayloadTests(unittest.TestCase):
    def test_dashboard_stats_are_derived_not_hardcoded(self):
        accounts = [
            {"account": "A1", "enabled": True, "bound": True},
            {"account": "A2", "enabled": True, "bound": False},
            {"account": "A3", "enabled": False, "bound": True},
        ]
        projects = [
            {"name": "p1", "enabled": True},
            {"name": "p2", "enabled": False},
        ]
        lanes = [{"account": "A1", "project": "p1", "run_id": "r1"}]
        with mock.patch.object(login_ui, "docker_snapshot", return_value={"ok": True, "rows": []}), \
             mock.patch.object(login_ui, "account_rows", return_value=accounts), \
             mock.patch.object(login_ui, "active_lane_snapshot", return_value=lanes), \
             mock.patch.object(login_ui, "project_rows", return_value=projects):
            data = login_ui.dashboard_payload()
        self.assertEqual(data["stats"]["total_accounts"], 3)
        self.assertEqual(data["stats"]["verified_accounts"], 1)
        self.assertEqual(data["stats"]["enabled_projects"], 1)
        self.assertEqual(data["stats"]["logical_capacity"], login_ui.core.DEFAULT_SLOTS)
        self.assertEqual(data["stats"]["verified_default_capacity"], login_ui.core.DEFAULT_SLOTS)
        self.assertEqual(data["scheduler"]["capacity_kind"], "verified_default")

    def test_scheduler_counts_live_lanes(self):
        accounts = [
            {"account": "A1", "enabled": True, "bound": True},
            {"account": "A2", "enabled": True, "bound": True},
        ]
        projects = [{"name": "p1", "enabled": True}]
        lanes = [
            {"account": "A1", "project": "p1", "run_id": "r1"},
            {"account": "A1", "project": "p1", "run_id": "r1"},
            {"account": "A2", "project": "p1", "run_id": "r1"},
        ]
        with mock.patch.object(login_ui, "docker_snapshot", return_value={"ok": True, "rows": []}), \
             mock.patch.object(login_ui, "account_rows", return_value=accounts), \
             mock.patch.object(login_ui, "active_lane_snapshot", return_value=lanes), \
             mock.patch.object(login_ui, "project_rows", return_value=projects):
            data = login_ui.dashboard_payload()
        scheduler = data["scheduler"]
        self.assertEqual(scheduler["status"], "running")
        self.assertEqual(scheduler["active_accounts"], 2)
        self.assertEqual(scheduler["active_lanes"], 3)
        self.assertEqual(scheduler["account_lanes"], {"A1": 2, "A2": 1})

    def test_active_lane_snapshot_filters_non_lane_account_containers(self):
        snapshot = {"ok": True, "rows": [
            {"container": "abc123", "account": "A1", "project": "proj-one", "run_id": "run-1", "kind": "lane"},
            {"container": "def456", "account": "A2", "project": "proj-two", "run_id": "run-1", "kind": "lane"},
            {"container": "login1", "account": "A1", "project": None, "run_id": None, "kind": "login"},
        ]}
        rows = login_ui.active_lane_snapshot(snapshot)
        self.assertEqual(rows[0]["account"], "A1")
        self.assertEqual(rows[1]["project"], "proj-two")
        self.assertEqual(len(rows), 2)

    def test_dashboard_reports_runtime_unavailable_not_idle(self):
        with mock.patch.object(login_ui, "docker_snapshot", return_value={"ok": False, "rows": []}), \
             mock.patch.object(login_ui, "account_rows", return_value=[]), \
             mock.patch.object(login_ui, "active_lane_snapshot", return_value=[]), \
             mock.patch.object(login_ui, "project_rows", return_value=[]):
            data = login_ui.dashboard_payload()
        self.assertEqual(data["scheduler"]["status"], "unavailable")
        self.assertFalse(data["scheduler"]["runtime_available"])

    def test_account_rows_with_snapshot_does_not_spawn_docker_per_account(self):
        db = {"accounts": {f"A{i}": {"enabled": True, "identity_sha256": None,
                                      "volume": f"vol-{i}"} for i in range(100)}}
        snapshot = {"ok": True, "rows": [], "accounts": set(), "mounts": ""}
        with mock.patch.object(login_ui.core, "account_db", return_value=db), \
             mock.patch.object(login_ui, "ui_state", return_value={"accounts": {}}), \
             mock.patch.object(login_ui.subprocess, "run") as run:
            rows = login_ui.account_rows(snapshot)
        self.assertEqual(len(rows), 100)
        run.assert_not_called()


class DashboardReferenceMarkupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "ui" / "login_ui.html").read_text(encoding="utf-8")

    def test_reference_sections_exist(self):
        for marker in ('id="dashboard"', 'id="accounts"', 'id="projects"', 'id="scheduler"'):
            self.assertIn(marker, self.html)

    def test_reference_quota_labels_exist(self):
        self.assertIn("5h Rolling / الخمس ساعات", self.html)
        self.assertIn("7d Weekly / الأسبوع", self.html)

    def test_bilingual_switch_exists(self):
        self.assertIn("العربية", self.html)
        self.assertIn("English", self.html)
        self.assertIn("setLang('ar')", self.html)
        self.assertIn("setLang('en')", self.html)

    def test_dashboard_is_local_only(self):
        self.assertIn("127.0.0.1", self.html)
        self.assertNotIn("https://cdn.", self.html)
        self.assertNotIn("http://cdn.", self.html)


if __name__ == "__main__":
    unittest.main()


class ActiveRunConfigTests(unittest.TestCase):
    def test_active_run_manifest_controls_reported_capacity(self):
        import json, tempfile
        old_runs = login_ui.core.RUNS
        try:
            with tempfile.TemporaryDirectory() as td:
                login_ui.core.RUNS = Path(td)
                run = Path(td) / "run-1"
                run.mkdir()
                (run / "manifest.effective.json").write_text(json.dumps({
                    "max_workers": 7,
                    "per_account_slots": 3
                }))
                cfg = login_ui.active_run_config(
                    [{"run_id": "run-1"}], fallback_capacity=20, default_slots=5
                )
            self.assertEqual(cfg["capacity"], 7)
            self.assertEqual(cfg["per_account_slots"], 3)
            self.assertEqual(cfg["capacity_kind"], "active_run")
        finally:
            login_ui.core.RUNS = old_runs

    def test_idle_capacity_uses_verified_fallback(self):
        cfg = login_ui.active_run_config([], fallback_capacity=10, default_slots=5)
        self.assertEqual(cfg["capacity"], 10)
        self.assertEqual(cfg["capacity_kind"], "verified_default")
