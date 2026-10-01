from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ui import login_ui


class UiStateRecoveryTests(unittest.TestCase):
    def test_abandoned_quota_and_verification_are_recovered(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "ui.json"
            state.write_text(json.dumps({"accounts": {"A1": {
                "quota_refreshing": True,
                "quota_status": "loading",
                "action": "verifying",
                "quota": [{"id": "gemini"}],
            }}}))
            with mock.patch.object(login_ui, "UI_STATE_FILE", state):
                login_ui.recover_ui_state()
                data = json.loads(state.read_text())
            row = data["accounts"]["A1"]
            self.assertFalse(row["quota_refreshing"])
            self.assertEqual(row["quota_status"], "unavailable")
            self.assertEqual(row["quota_error"], "quota_refresh_interrupted")
            self.assertEqual(row["action"], "verify_failed")
            self.assertEqual(row["message"], "verification_interrupted")
            self.assertEqual(row["quota"], [{"id": "gemini"}])


class ApiTokenTests(unittest.TestCase):
    def test_api_token_is_header_only(self):
        handler = object.__new__(login_ui.Handler)
        handler.headers = {"X-UI-Token": login_ui.TOKEN}
        self.assertTrue(handler._token_ok())
        handler.headers = {"X-UI-Token": "wrong"}
        self.assertFalse(handler._token_ok())

    def test_dashboard_does_not_fetch_status_with_query_token(self):
        html = (ROOT / "ui" / "login_ui.html").read_text(encoding="utf-8")
        self.assertNotIn("/api/status?token=", html)
        self.assertIn("'X-UI-Token':TOKEN", html)
        self.assertIn("sessionStorage.setItem('agy-ui-token'", html)

    def test_dashboard_marks_unavailable_runtime_and_unknown_mixed_slots(self):
        html = (ROOT / "ui" / "login_ui.html").read_text(encoding="utf-8")
        self.assertIn("runtimeUnavailable", html)
        self.assertIn("knownSlots", html)
        self.assertIn("knownSlots?slots:'—'", html)
        self.assertNotIn("Math.max(1,Number(s.per_account_slots)||1)", html)
        self.assertIn("a.busy_kind==='lane'?'laneBusy'", html)


class QuotaIsolationTests(unittest.TestCase):
    def test_quota_worker_clones_master_with_binding_provenance(self):
        binding = {"identity_sha256": "hash-a", "binding_id": "bind-a", "credential_generation": 3, "enabled": True}
        current = {"accounts": {"A1": dict(binding)}}
        with mock.patch.object(login_ui, "account_in_use", return_value=False), \
             mock.patch.object(login_ui.core, "require_registered_account", return_value=binding), \
             mock.patch.object(login_ui.core, "account_db", return_value=current), \
             mock.patch.object(login_ui.core, "master_volume", return_value="master-A1"), \
             mock.patch.object(login_ui.core, "clone_volume") as clone, \
             mock.patch.object(login_ui.core, "remove_volume") as remove, \
             mock.patch.object(login_ui.core, "account_operation_lock") as lock_factory, \
             mock.patch.object(login_ui.quota_usage, "probe_account_usage", return_value=[{"id": "gemini"}]) as probe, \
             mock.patch.object(login_ui, "patch_ui") as patch:
            lock_factory.return_value.__enter__.return_value = None
            lock_factory.return_value.__exit__.return_value = False
            login_ui.QUOTA_SUBMIT_SLOTS.acquire()
            login_ui.quota_worker("A1")
            source, target = clone.call_args.args[1:3]
            self.assertEqual(source, "master-A1")
            self.assertNotEqual(target, "master-A1")
            self.assertEqual(probe.call_args.kwargs["account_volume"], target)
            remove.assert_called_with(target)
            self.assertEqual(patch.call_args.kwargs["quota_identity_sha256"], "hash-a")
            self.assertEqual(patch.call_args.kwargs["quota_generation"], 3)

    def test_quota_worker_discards_result_if_identity_generation_changes(self):
        binding = {"identity_sha256": "old", "binding_id": "bind-old", "credential_generation": 1, "enabled": True}
        changed = {"accounts": {"A1": {"identity_sha256": "new", "credential_generation": 2}}}
        with mock.patch.object(login_ui, "account_in_use", return_value=False), \
             mock.patch.object(login_ui.core, "require_registered_account", return_value=binding), \
             mock.patch.object(login_ui.core, "account_db", return_value=changed), \
             mock.patch.object(login_ui.core, "clone_volume"), \
             mock.patch.object(login_ui.core, "remove_volume"), \
             mock.patch.object(login_ui.core, "account_operation_lock") as lock_factory, \
             mock.patch.object(login_ui.quota_usage, "probe_account_usage", return_value=[{"id": "gemini"}]), \
             mock.patch.object(login_ui, "patch_ui") as patch:
            lock_factory.return_value.__enter__.return_value = None
            lock_factory.return_value.__exit__.return_value = False
            login_ui.QUOTA_SUBMIT_SLOTS.acquire()
            login_ui.quota_worker("A1")
            self.assertEqual(patch.call_args.kwargs["quota"], [])
            self.assertEqual(patch.call_args.kwargs["quota_error"], "quota_identity_changed")

    def test_cleanup_failure_still_releases_worker_slot(self):
        binding = {"identity_sha256": "hash", "credential_generation": 1, "enabled": True}
        with mock.patch.object(login_ui, "account_in_use", return_value=False), \
             mock.patch.object(login_ui.core, "require_registered_account", return_value=binding), \
             mock.patch.object(login_ui.core, "account_db", return_value={"accounts": {"A1": binding}}), \
             mock.patch.object(login_ui.core, "clone_volume"), \
             mock.patch.object(login_ui.core, "remove_volume", side_effect=RuntimeError("cleanup")), \
             mock.patch.object(login_ui.core, "account_operation_lock") as lock_factory, \
             mock.patch.object(login_ui.quota_usage, "probe_account_usage", return_value=[]), \
             mock.patch.object(login_ui, "patch_ui"):
            lock_factory.return_value.__enter__.return_value = None
            lock_factory.return_value.__exit__.return_value = False
            login_ui.QUOTA_SUBMIT_SLOTS.acquire()
            login_ui.quota_worker("A1")
            self.assertTrue(login_ui.QUOTA_SUBMIT_SLOTS.acquire(blocking=False))
            login_ui.QUOTA_SUBMIT_SLOTS.release()

    def test_account_rows_hides_quota_from_old_binding(self):
        db = {"accounts": {"A1": {"enabled": True, "identity_sha256": "new", "credential_generation": 2}}}
        state = {"accounts": {"A1": {"quota": [{"id": "gemini"}], "quota_status": "ok",
                                      "quota_identity_sha256": "old", "quota_generation": 1,
                                      "quota_updated_at": 1}}}
        snapshot = {"ok": True, "rows": [], "accounts": set(), "mounts": ""}
        with mock.patch.object(login_ui.core, "account_db", return_value=db), \
             mock.patch.object(login_ui, "ui_state", return_value=state):
            row = login_ui.account_rows(snapshot)[0]
        self.assertEqual(row["quota"], [])
        self.assertEqual(row["quota_error"], "quota_identity_changed")
        self.assertTrue(row["quota_stale"])

    def test_auto_quota_refresh_is_off_by_default(self):
        self.assertFalse(login_ui.QUOTA_AUTO_REFRESH)

    def test_verify_worker_does_not_impose_outer_timeout_over_bounded_core(self):
        completed = __import__('subprocess').CompletedProcess([], 4, stdout='{"reason":"busy"}', stderr="")
        with mock.patch.object(login_ui.subprocess, "run", return_value=completed) as run, \
             mock.patch.object(login_ui, "patch_ui"):
            login_ui.verify_worker("A1")
        self.assertNotIn("timeout", run.call_args.kwargs)

    def test_toggle_fails_closed_when_account_is_in_use(self):
        with mock.patch.object(login_ui.core, "require_registered_account", return_value={"enabled": True}), \
             mock.patch.object(login_ui, "account_in_use", return_value=True), \
             mock.patch.object(login_ui.core, "toggle_account_enabled") as setter:
            ok, message = login_ui.toggle_account("A1")
        self.assertFalse(ok)
        self.assertEqual(message, "account_in_use")
        setter.assert_not_called()


if __name__ == "__main__":
    unittest.main()

class OrphanQuotaVolumeCleanupTests(unittest.TestCase):
    def test_cleanup_removes_only_unmounted_quota_volumes(self):
        listed = subprocess.CompletedProcess([], 0, stdout=(
            "agy-quota-home-a1-dead\n"
            "agy-quota-home-a2-live\n"
            "unrelated-volume\n"
        ), stderr="")
        dead_check = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        live_check = subprocess.CompletedProcess([], 0, stdout="container123\n", stderr="")
        with mock.patch.object(login_ui.subprocess, "run", side_effect=[listed, dead_check, live_check]), \
             mock.patch.object(login_ui.core, "remove_volume") as remove:
            result = login_ui.cleanup_orphan_quota_volumes()
        remove.assert_called_once_with("agy-quota-home-a1-dead")
        self.assertEqual(result, {"ok": True, "found": 2, "removed": 1, "skipped": 1})

    def test_cleanup_fails_closed_when_docker_unavailable(self):
        with mock.patch.object(login_ui.subprocess, "run", side_effect=OSError("docker down")), \
             mock.patch.object(login_ui.core, "remove_volume") as remove:
            result = login_ui.cleanup_orphan_quota_volumes()
        remove.assert_not_called()
        self.assertFalse(result["ok"])

