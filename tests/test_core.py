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
import multiplex as mx


class DynamicPoolTests(unittest.TestCase):
    def test_explicit_account_pool_has_no_fixed_five_limit(self):
        accounts = [f"acct-{i}" for i in range(37)]
        self.assertEqual(mx.resolve_accounts({"accounts": accounts}), accounts)

    def test_slots_have_no_hardcoded_upper_bound(self):
        self.assertEqual(mx.resolve_slots({"per_account_slots": 17}), 17)

    def test_case_insensitive_duplicate_accounts_are_rejected(self):
        with self.assertRaises(ValueError):
            mx.resolve_accounts({"accounts": ["Work", "work"]})

    def test_projected_wave_supports_many_projects(self):
        projects = []
        for i in range(60):
            task = {"task_id": f"t{i}", "goal": "x", "dependencies": [],
                    "write_scope": [f"src/{i}"]}
            projects.append(mx.Project(name=f"p{i}", repo=Path("."), goal="",
                                       tasks={task["task_id"]: task}))
        accounts = [f"acct-{i}" for i in range(12)]
        wave = mx.projected_wave(projects, accounts, slots=5, capacity=60)
        self.assertEqual(len(wave), 60)
        self.assertEqual(len({x["project"] for x in wave}), 60)

    def test_new_volume_names_do_not_collide_after_slugging(self):
        old_state = mx.STATE
        try:
            with tempfile.TemporaryDirectory() as td:
                mx.STATE = Path(td) / "state"
                self.assertNotEqual(mx.master_volume("team_one"), mx.master_volume("team-one"))
        finally:
            mx.STATE = old_state


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_state, self.old_runs = mx.STATE, mx.RUNS
        mx.STATE = Path(self.tmp.name) / "state"
        mx.RUNS = Path(self.tmp.name) / "runs"

    def tearDown(self):
        mx.STATE, mx.RUNS = self.old_state, self.old_runs
        self.tmp.cleanup()

    def _repo_and_plan(self, name: str):
        repo = Path(self.tmp.name) / name
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "README.md").write_text("test\n")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "init"], check=True)
        plan = Path(self.tmp.name) / f"{name}.json"
        plan.write_text(json.dumps({"tasks": [{"task_id": "one", "goal": "x",
            "dependencies": [], "write_scope": ["src/one"]}]}))
        return repo, plan

    def test_manifest_can_resolve_registered_projects_without_count_limit(self):
        records = {}
        for i in range(8):
            repo, plan = self._repo_and_plan(f"repo{i}")
            records[f"project-{i}"] = {"repo": str(repo), "plan": str(plan),
                                      "goal": "test", "enabled": True}
        mx.write_json(mx.project_db_path(), {"schema_version": 1, "projects": records})
        data, projects = mx.load_manifest(None)
        self.assertEqual(data, {})
        self.assertEqual(len(projects), 8)

    def test_concurrent_project_cli_updates_preserve_both_records(self):
        repo1, plan1 = self._repo_and_plan("parallel1")
        repo2, plan2 = self._repo_and_plan("parallel2")
        home = Path(self.tmp.name) / "cli-home"
        env = dict(__import__('os').environ, AGY_MULTIPLEX_HOME=str(home))
        base = [sys.executable, str(ROOT / "multiplex.py"), "add-project"]
        p1 = subprocess.Popen(base + ["--name", "p-one", "--repo", str(repo1), "--plan", str(plan1)],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        p2 = subprocess.Popen(base + ["--name", "p-two", "--repo", str(repo2), "--plan", str(plan2)],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out1, err1 = p1.communicate(timeout=10); out2, err2 = p2.communicate(timeout=10)
        self.assertEqual(p1.returncode, 0, err1 or out1)
        self.assertEqual(p2.returncode, 0, err2 or out2)
        data = json.loads((home / "state" / "projects.json").read_text())
        self.assertEqual(set(data["projects"]), {"p-one", "p-two"})

    @unittest.skipIf(mx.fcntl is None, "fcntl account locks require macOS/Linux")
    def test_account_operation_lock_is_cross_process(self):
        home = Path(self.tmp.name) / "lock-home"
        env = dict(__import__('os').environ, AGY_MULTIPLEX_HOME=str(home), PYTHONPATH=str(ROOT))
        holder_code = (
            "import time,multiplex as m; "
            "ctx=m.account_operation_lock('A1'); ctx.__enter__(); "
            "print('LOCKED', flush=True); time.sleep(2); ctx.__exit__(None,None,None)"
        )
        holder = subprocess.Popen([sys.executable, "-c", holder_code], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "LOCKED")
            contender_code = (
                "import multiplex as m; "
                "\ntry:\n with m.account_operation_lock('A1', blocking=False): print('ACQUIRED')"
                "\nexcept BlockingIOError: print('BLOCKED')"
            )
            contender = subprocess.run([sys.executable, "-c", contender_code], env=env,
                                       capture_output=True, text=True, timeout=5)
            self.assertEqual(contender.returncode, 0, contender.stderr)
            self.assertEqual(contender.stdout.strip(), "BLOCKED")
        finally:
            holder.terminate(); holder.communicate(timeout=5)

    def test_binding_invalidation_increments_generation_and_clears_identity(self):
        mx.write_json(mx.account_db_path(), {"schema_version": 2, "accounts": {"A1": {
            "enabled": True, "bound": True, "credential_generation": 4,
            "identity_sha256": "hash", "masked_identity": "a***@x", "verified_at": "now",
        }}})
        generation = mx.invalidate_account_binding("A1")
        item = mx.account_db()["accounts"]["A1"]
        self.assertEqual(generation, 5)
        self.assertEqual(item["credential_generation"], 5)
        self.assertFalse(item["bound"])
        self.assertNotIn("identity_sha256", item)
        self.assertNotIn("masked_identity", item)
        self.assertNotIn("verified_at", item)

    def test_clone_and_remove_volume_use_bounded_timeouts(self):
        with mock.patch.object(mx, "ensure_volume"), mock.patch.object(mx, "sh") as sh:
            mx.clone_volume("image", "src", "dst")
            self.assertEqual(sh.call_args.kwargs["timeout"], mx.DOCKER_OP_TIMEOUT)
            self.assertIn("--name", sh.call_args.args)
        with mock.patch.object(mx, "sh") as sh:
            mx.remove_volume("dst")
            self.assertEqual(sh.call_args.kwargs["timeout"], 20)

    def test_clone_timeout_removes_named_helper_container(self):
        timeout = subprocess.TimeoutExpired(cmd=["docker"], timeout=mx.DOCKER_OP_TIMEOUT)
        cleanup = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(mx, "ensure_volume"), \
             mock.patch.object(mx, "sh", side_effect=[timeout, cleanup]) as sh:
            with self.assertRaises(subprocess.TimeoutExpired):
                mx.clone_volume("image", "src", "dst")
        cleanup_call = sh.call_args_list[1]
        self.assertEqual(cleanup_call.args[:3], ("docker", "rm", "-f"))
        self.assertEqual(cleanup_call.kwargs["timeout"], 20)

    def test_bind_fails_fast_when_account_operation_lock_is_busy(self):
        ctx = mock.MagicMock()
        ctx.__enter__.side_effect = BlockingIOError()
        args = __import__('argparse').Namespace(account="A1", image="img")
        with mock.patch.object(mx, "account_operation_lock", return_value=ctx), \
             mock.patch.object(mx, "_cmd_bind_locked") as inner:
            rc = mx.cmd_bind(args)
        self.assertEqual(rc, 4)
        inner.assert_not_called()

    def test_launch_job_holds_account_lock_until_container_running(self):
        task = {"task_id": "t1", "goal": "x", "dependencies": [], "write_scope": ["src/t1"]}
        project = mx.Project(name="p1", repo=Path(self.tmp.name), goal="", tasks={"t1": task})
        run_root = Path(self.tmp.name) / "run"
        run_root.mkdir()
        worktree = Path(self.tmp.name) / "worktree"
        worktree.mkdir()
        (worktree / ".git").mkdir()
        active = {"value": False}

        class Guard:
            def __enter__(self):
                active["value"] = True
            def __exit__(self, *_):
                active["value"] = False

        proc = mock.MagicMock()
        proc.stdin = mock.MagicMock()

        def assert_locked(*_args, **_kwargs):
            self.assertTrue(active["value"])

        with mock.patch.object(mx, "create_lane_worktree", return_value=(worktree, "base")), \
             mock.patch.object(mx, "account_operation_lock", return_value=Guard()), \
             mock.patch.object(mx, "clone_volume", side_effect=assert_locked), \
             mock.patch.object(mx, "install_profile", side_effect=assert_locked), \
             mock.patch.object(mx.subprocess, "Popen", side_effect=lambda *a, **k: (assert_locked(), proc)[1]), \
             mock.patch.object(mx, "wait_for_container_running", side_effect=assert_locked), \
             mock.patch.object(mx, "master_volume", return_value="master-A1"), \
             mock.patch.object(mx, "lane_volume", return_value="lane-A1"):
            job = mx.launch_job(project, task, "A1", 1, "image", run_root, "run1")

        self.assertFalse(active["value"])
        self.assertIs(job.proc, proc)
        proc.stdin.write.assert_called_once()
        job.stdout_file.close()



if __name__ == "__main__":
    unittest.main()
