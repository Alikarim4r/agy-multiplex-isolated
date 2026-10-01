from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
