from types import SimpleNamespace
from unittest.mock import patch

import multiplex


def _job(tmp_path, require_changes):
    repo = tmp_path / "repo"
    repo.mkdir()
    project = SimpleNamespace(running={"task"}, staging=repo)
    proc = SimpleNamespace(returncode=0)
    log = tmp_path / "agy.log"
    log.write_text("authenticated as masked@example.com\n")
    out = (tmp_path / "stdout.log").open("w")
    return SimpleNamespace(
        stdout_file=out,
        task={"task_id": "task", "write_scope": ["**"], "require_changes": require_changes},
        project=project,
        account="A1",
        slot=1,
        model="gemini-3.8-flash-high",
        attempt=1,
        proc=proc,
        worktree=repo,
        base_sha="base",
        log_file=log,
        home_volume="vol",
    )


def test_require_changes_rejects_success_without_diff(tmp_path):
    job = _job(tmp_path, True)
    identity_hash = multiplex.email_hash("masked@example.com")
    accounts = {"accounts": {"A1": {"identity_sha256": identity_hash}}}
    with (
        patch.object(multiplex, "git_head", return_value="base"),
        patch.object(multiplex, "changed_paths", return_value=[]),
        patch.object(multiplex, "remove_volume"),
    ):
        ok, message, _ = multiplex.finish_job(job, accounts)
    assert ok is False
    assert message == "task required file changes but produced none"


def test_no_change_allowed_when_not_required(tmp_path):
    job = _job(tmp_path, False)
    identity_hash = multiplex.email_hash("masked@example.com")
    accounts = {"accounts": {"A1": {"identity_sha256": identity_hash}}}
    with (
        patch.object(multiplex, "git_head", return_value="base"),
        patch.object(multiplex, "changed_paths", return_value=[]),
        patch.object(multiplex, "remove_volume"),
    ):
        ok, message, _ = multiplex.finish_job(job, accounts)
    assert ok is True
    assert message == "success with no file changes"
