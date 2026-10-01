#!/usr/bin/env python3
from __future__ import annotations

import argparse, hashlib, json, os, re, shutil, signal, subprocess, sys, time, uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import routing as rt

try:
    import fcntl
except ImportError:  # pragma: no cover - public alpha currently targets macOS/Linux
    fcntl = None

CODE_ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("AGY_MULTIPLEX_HOME", str(Path.home() / ".agy-multiplex-isolated"))).expanduser().resolve()
STATE = DATA_ROOT / "state"
RUNS = DATA_ROOT / "runs"
DEFAULT_IMAGE = os.environ.get("AGY_MULTIPLEX_IMAGE", "agy-multiplex-isolated:1.2.14")
DEFAULT_SLOTS = max(1, int(os.environ.get("AGY_MULTIPLEX_SLOTS", "5")))
DOCKER_OP_TIMEOUT = max(10, int(os.environ.get("AGY_DOCKER_OP_TIMEOUT", "60")))
IDENTITY_RE = re.compile(r"authenticated successfully as (\S+)", re.I)
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
DENY_CMDS = ["git","gh","rm","mv","cp","sudo","su","security","curl","wget","ssh","scp","rsync","docker","podman","kubectl","helm","terraform","vercel","firebase","supabase","gcloud","aws","az"]


def sh(*args: str, check: bool = True, input_text: str | None = None,
       capture: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, text=True, input=input_text,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None,
                          timeout=timeout)


def wait_for_container_running(name: str, proc: subprocess.Popen, timeout: float = 15.0) -> None:
    """Keep the account startup lock until Docker reports the lane container running."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"lane container exited during startup: {name} (rc={proc.returncode})")
        status = sh("docker", "inspect", "-f", "{{.State.Running}}", name,
                    check=False, timeout=5)
        if status.returncode == 0 and (status.stdout or "").strip().lower() == "true":
            return
        time.sleep(0.1)
    raise TimeoutError(f"lane container did not reach running state: {name}")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def state_lock(name: str, *, blocking: bool = True):
    lock_dir = STATE / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    handle = (lock_dir / f"{slug(name)}.lock").open("a+")
    if fcntl is None:
        try:
            yield
        finally:
            handle.close()
        return
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    acquired = False
    try:
        fcntl.flock(handle.fileno(), flags)
        acquired = True
        yield
    finally:
        try:
            if acquired:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def account_operation_lock(account: str, *, blocking: bool = True):
    account = validate_id(account, "account id")
    digest = hashlib.sha256(account.casefold().encode()).hexdigest()[:16]
    return state_lock(f"account-op-{digest}", blocking=blocking)


def slug(text: str) -> str:
    out = re.sub(r"[^A-Za-z0-9_-]+", "-", text.strip()).strip("-").lower()
    return out[:60] or "item"


def validate_id(value: str, kind: str = "id") -> str:
    value = str(value).strip()
    if not SAFE_ID.fullmatch(value):
        raise ValueError(f"invalid {kind}: {value!r}; use letters, numbers, underscore or hyphen")
    return value


def master_volume(account: str) -> str:
    account = validate_id(account, "account id")
    path = account_db_path()
    if path.exists():
        try:
            stored = ((read_json(path).get("accounts") or {}).get(account) or {}).get("volume")
            if stored:
                return str(stored)
        except (OSError, json.JSONDecodeError):
            pass
    digest = hashlib.sha256(account.casefold().encode()).hexdigest()[:8]
    return f"agy-multiplex-master-{slug(account)}-{digest}"

def lane_volume(run_id: str, account: str, project: str, task_id: str, slot: int) -> str:
    return f"agy-mx-{slug(run_id)}-{slug(account)}-{slug(project)}-{slug(task_id)}-{slot}"


def account_db_path() -> Path:
    return STATE / "accounts.json"


def account_db() -> dict[str, Any]:
    path = account_db_path()
    if path.exists():
        data = read_json(path)
        data["schema_version"] = 2
        data.setdefault("accounts", {})
        for item in data["accounts"].values():
            item.setdefault("enabled", True)
            item.setdefault("credential_generation", 0)
        return data
    return {"schema_version": 2, "accounts": {}}


def project_db_path() -> Path:
    return STATE / "projects.json"


def project_db() -> dict[str, Any]:
    path = project_db_path()
    if path.exists():
        data = read_json(path)
        data.setdefault("schema_version", 1)
        data.setdefault("projects", {})
        return data
    return {"schema_version": 1, "projects": {}}


def configured_account_ids(*, enabled_only: bool = True) -> list[str]:
    rows = account_db().get("accounts") or {}
    out = []
    for account, item in rows.items():
        if enabled_only and item.get("enabled", True) is False:
            continue
        out.append(account)
    return sorted(out, key=str.casefold)


def resolve_accounts(data: dict[str, Any]) -> list[str]:
    raw = data.get("accounts", None)
    if raw is None or raw == "all":
        accounts = configured_account_ids(enabled_only=True)
    elif isinstance(raw, list):
        accounts = [validate_id(str(x), "account id") for x in raw]
    else:
        raise ValueError("manifest.accounts must be a list, 'all', or omitted")
    if not accounts:
        raise ValueError("no accounts selected; add accounts first or list them in the manifest")
    lowered = [x.casefold() for x in accounts]
    if len(set(lowered)) != len(lowered):
        raise ValueError("account IDs must be unique (case-insensitive)")
    return accounts


def resolve_slots(data: dict[str, Any]) -> int:
    slots = int(data.get("per_account_slots", DEFAULT_SLOTS))
    if slots < 1:
        raise ValueError("per_account_slots must be >= 1")
    return slots


def email_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return ((local[:2] + "***") if local else "***") + ("@" + domain if domain else "")


def docker_ok() -> bool:
    return sh("docker", "info", check=False).returncode == 0


def image_version(image: str) -> str | None:
    p = sh("docker", "run", "--rm", image, "agy", "--version", check=False)
    return p.stdout.strip() if p.returncode == 0 else None

def ensure_volume(name: str) -> None:
    if sh("docker", "volume", "inspect", name, check=False, timeout=20).returncode != 0:
        sh("docker", "volume", "create", name, timeout=20)


def clone_volume(image: str, source: str, target: str) -> None:
    ensure_volume(source); ensure_volume(target)
    cmd = "set -e; uid=$(id -u agy); gid=$(id -g agy); rm -rf /dst/* /dst/.[!.]* /dst/..?* 2>/dev/null || true; cp -a /src/. /dst/; chown -R ${uid}:${gid} /dst"
    cname = f"agy-copy-{uuid.uuid4().hex[:12]}"
    try:
        sh("docker", "run", "--rm", "--name", cname, "--user", "0:0",
           "-v", f"{source}:/src:ro", "-v", f"{target}:/dst", image,
           "bash", "-lc", cmd, timeout=DOCKER_OP_TIMEOUT)
    except subprocess.TimeoutExpired:
        sh("docker", "rm", "-f", cname, check=False, timeout=20)
        raise


def remove_volume(name: str) -> None:
    sh("docker", "volume", "rm", "-f", name, check=False, timeout=20)


def configured_reference_roots() -> list[Path]:
    raw = os.environ.get("AGY_MULTIPLEX_REFERENCE_ROOTS", "").strip()
    if not raw:
        return []
    roots: list[Path] = []
    for item in raw.split(os.pathsep):
        if not item.strip():
            continue
        path = Path(item).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"read-only reference root does not exist: {path}")
        roots.append(path)
    return roots


def project_profile(project_id: str, read_only_roots: list[Path] | None = None) -> dict[str, Any]:
    allow = ["read_file(/workspace/)", "write_file(/workspace/)", "command(*)"]
    for root in read_only_roots or []:
        allow.append(f"read_file({str(root).rstrip('/')}/)")
    deny = [f"command({name})" for name in DENY_CMDS]
    return {
        "id": project_id,
        "name": f"AGY isolated lane {project_id}",
        "managedBy": "agy-multiplex-isolated",
        "projectResources": {"resources": [{"folderUri": "file:///workspace"}]},
        "permissionGrants": {"permissionGrants": {"allow": allow, "deny": deny}, "v2Migrated": True},
        "settings": {}, "isWorkspaceOnly": False,
    }


def install_profile(image: str, volume: str, profile_file: Path, project_id: str) -> None:
    cmd = f"uid=$(id -u agy); gid=$(id -g agy); mkdir -p /home/agy/.gemini/config/projects; cp /tmp/profile.json /home/agy/.gemini/config/projects/{project_id}.json; chown -R ${{uid}}:${{gid}} /home/agy/.gemini"
    cname = f"agy-profile-{uuid.uuid4().hex[:12]}"
    try:
        sh("docker", "run", "--rm", "--name", cname, "--user", "0:0", "-v", f"{volume}:/home/agy",
           "-v", f"{profile_file}:/tmp/profile.json:ro", image, "bash", "-lc", cmd,
           timeout=DOCKER_OP_TIMEOUT)
    except subprocess.TimeoutExpired:
        sh("docker", "rm", "-f", cname, check=False, timeout=20)
        raise


def git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return sh("git", "-C", str(cwd), *args, check=check)

def git_head(repo: Path) -> str:
    return git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def changed_paths(repo: Path, base: str) -> list[str]:
    committed = git("diff", "--name-only", f"{base}...HEAD", cwd=repo, check=False).stdout.splitlines()
    unstaged = git("diff", "--name-only", cwd=repo, check=False).stdout.splitlines()
    staged = git("diff", "--cached", "--name-only", cwd=repo, check=False).stdout.splitlines()
    untracked = git("ls-files", "--others", "--exclude-standard", cwd=repo, check=False).stdout.splitlines()
    return sorted({p.strip() for p in committed + unstaged + staged + untracked if p.strip()})


def scope_base(scope: str) -> str:
    s = scope.strip().replace("\\", "/")
    while s.endswith("*"):
        s = s[:-1]
    return s.rstrip("/").removeprefix("./")


def path_allowed(path: str, scopes: list[str]) -> bool:
    p = path.strip("/")
    for raw in scopes:
        s = scope_base(raw)
        if s in {"", "."} or p == s or p.startswith(s + "/"):
            return True
    return False


def scope_overlap(left: list[str], right: list[str]) -> bool:
    for a in left:
        aa = scope_base(a)
        for b in right:
            bb = scope_base(b)
            if aa in {"", "."} or bb in {"", "."}:
                return True
            if aa == bb or aa.startswith(bb + "/") or bb.startswith(aa + "/"):
                return True
    return False

@dataclass
class Project:
    name: str
    repo: Path
    goal: str
    tasks: dict[str, dict[str, Any]]
    model: str | None = None
    models: list[str] = field(default_factory=list)
    staging: Path | None = None
    branch: str | None = None
    base_sha: str | None = None
    completed: set[str] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)
    running: set[str] = field(default_factory=set)

    def ready(self) -> list[dict[str, Any]]:
        out = []
        for tid, task in self.tasks.items():
            if tid in self.completed or tid in self.failed or tid in self.running:
                continue
            deps = set(task.get("dependencies") or [])
            if deps <= self.completed:
                out.append(task)
        return out


@dataclass
class Job:
    project: Project
    task: dict[str, Any]
    account: str
    slot: int
    model: str | None
    attempt: int
    proc: subprocess.Popen[str]
    lane_dir: Path
    worktree: Path
    base_sha: str
    home_volume: str
    log_file: Path
    stdout_file: Any


def load_plan(path: Path) -> dict[str, dict[str, Any]]:
    data = read_json(path)
    tasks = data.get("tasks") or []
    out: dict[str, dict[str, Any]] = {}
    for task in tasks:
        tid = str(task.get("task_id") or "")
        if not SAFE_ID.fullmatch(tid):
            raise ValueError(f"invalid task_id: {tid!r}")
        if tid in out:
            raise ValueError(f"duplicate task_id: {tid}")
        if not task.get("write_scope"):
            raise ValueError(f"task {tid} has no write_scope")
        out[tid] = task
    if not out:
        raise ValueError(f"plan has no tasks: {path}")
    for tid, task in out.items():
        missing = set(task.get("dependencies") or []) - set(out)
        if missing:
            raise ValueError(f"task {tid} has unknown dependencies: {sorted(missing)}")
    return out

def registered_project_records(*, enabled_only: bool = True) -> list[dict[str, Any]]:
    rows = project_db().get("projects") or {}
    out: list[dict[str, Any]] = []
    for name, item in rows.items():
        if enabled_only and item.get("enabled", True) is False:
            continue
        record = dict(item)
        record["name"] = name
        out.append(record)
    return sorted(out, key=lambda x: str(x["name"]).casefold())


def project_from_record(raw: dict[str, Any]) -> Project:
    name = validate_id(str(raw.get("name") or ""), "project id")
    repo_value = raw.get("repo")
    plan_value = raw.get("plan")
    if not repo_value or not plan_value:
        raise ValueError(f"project {name} requires repo and plan")
    repo = Path(str(repo_value)).expanduser().resolve()
    if not repo.exists():
        raise ValueError(f"project repository does not exist: {repo}")
    if not (repo / ".git").exists() and git("rev-parse", "--git-dir", cwd=repo, check=False).returncode != 0:
        raise ValueError(f"not a git repository: {repo}")
    plan = Path(str(plan_value)).expanduser().resolve()
    if not plan.is_file():
        raise ValueError(f"project plan does not exist: {plan}")
    models = rt.normalize_models(raw.get("models"))
    legacy_model = str(raw.get("model") or "").strip() or None
    if legacy_model and legacy_model.casefold() not in {m.casefold() for m in models}:
        models.insert(0, legacy_model)
    primary_model = models[0] if models else legacy_model
    return Project(name=name, repo=repo, goal=str(raw.get("goal") or ""),
                   tasks=load_plan(plan), model=primary_model, models=models)


def load_manifest(path: Path | None) -> tuple[dict[str, Any], list[Project]]:
    data = read_json(path) if path is not None else {}
    raw_projects = data.get("projects", None)
    if raw_projects is None or raw_projects == "all":
        raw_projects = registered_project_records(enabled_only=True)
    if not isinstance(raw_projects, list):
        raise ValueError("manifest.projects must be a list, 'all', or omitted")
    projects = [project_from_record(dict(raw)) for raw in raw_projects]
    if not projects:
        raise ValueError("no projects selected; register projects first or list them in the manifest")
    names = [p.name.casefold() for p in projects]
    if len(set(names)) != len(names):
        raise ValueError("project IDs must be unique (case-insensitive)")
    return data, projects


def manifest_path_arg(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"manifest does not exist: {path}")
    return path

def validate_dag(project: Project) -> None:
    visiting: set[str] = set(); visited: set[str] = set()
    def visit(tid: str) -> None:
        if tid in visiting:
            raise ValueError(f"cycle in {project.name}: {tid}")
        if tid in visited: return
        visiting.add(tid)
        for dep in project.tasks[tid].get("dependencies") or []: visit(dep)
        visiting.remove(tid); visited.add(tid)
    for tid in project.tasks: visit(tid)

def setup_project(project: Project, run_root: Path, run_id: str) -> None:
    validate_dag(project)
    project.base_sha = git_head(project.repo)
    project.branch = f"agyiso/{slug(project.name)}-{slug(run_id)}"
    project.staging = run_root / "projects" / slug(project.name) / "staging"
    project.staging.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "-b", project.branch, str(project.staging), project.base_sha, cwd=project.repo)


def running_conflict(project: Project, task: dict[str, Any]) -> bool:
    scopes = list(task.get("write_scope") or [])
    for tid in project.running:
        if scope_overlap(scopes, list(project.tasks[tid].get("write_scope") or [])):
            return True
    return False


def create_lane_worktree(project: Project, task_id: str, lane_dir: Path) -> tuple[Path, str]:
    assert project.staging is not None
    base = git_head(project.staging)
    worktree = lane_dir / "worktree"
    branch = f"agyiso-lane/{slug(project.name)}-{slug(task_id)}-{uuid.uuid4().hex[:6]}"
    git("worktree", "add", "-b", branch, str(worktree), base, cwd=project.staging)
    return worktree, base


def task_prompt(project: Project, task: dict[str, Any]) -> str:
    scopes = ", ".join(task.get("write_scope") or [])
    acceptance = "\n".join(f"- {x}" for x in (task.get("acceptance") or [])) or "- satisfy the task goal"
    return f"""You are one isolated implementation lane.\nProject goal: {project.goal}\nTask: {task.get('goal','')}\nYou may modify ONLY: {scopes}\nDo not commit, push, merge, deploy, or change Git state.\nDo not touch files outside the allowed write scope.\nAcceptance:\n{acceptance}\nImplement the task completely, then report a concise summary."""

def launch_job(project: Project, task: dict[str, Any], account: str, slot: int,
               image: str, run_root: Path, run_id: str, *,
               model: str | None = None, attempt: int = 1) -> Job:
    tid = str(task["task_id"])
    lane_name = slug(tid) if attempt == 1 else f"{slug(tid)}-retry-{attempt}"
    lane_dir = run_root / "projects" / slug(project.name) / "lanes" / lane_name
    lane_dir.mkdir(parents=True, exist_ok=False)
    worktree, base = create_lane_worktree(project, tid, lane_dir)
    volume = lane_volume(run_id, account, project.name, tid, slot)
    project_id = f"agyiso-{slug(project.name)}-{slug(tid)}-{uuid.uuid4().hex[:6]}"
    profile_file = lane_dir / "project.json"
    reference_roots = configured_reference_roots()
    write_json(profile_file, project_profile(project_id, reference_roots))
    evidence = lane_dir / "evidence"; evidence.mkdir(exist_ok=True)
    log_file = evidence / "agy.log"
    stdout_file = (lane_dir / "stdout.log").open("w", encoding="utf-8")
    cname = f"agyiso-{slug(run_id)}-{slug(account)}-{slug(tid)}-{slot}"
    cmd = ["docker", "run", "--rm", "--name", cname, "-i",
           "--cap-drop=ALL", "--security-opt=no-new-privileges",
           "--label", f"agy.multiplex.run={run_id}",
           "--label", f"agy.multiplex.account={account}",
           "--label", f"agy.multiplex.project={project.name}",
           "--label", "agy.multiplex.kind=lane",
           "-v", f"{volume}:/home/agy",
           "-v", f"{worktree}:/workspace",
           "-v", f"{worktree / '.git'}:/workspace/.git:ro",
           "-v", f"{evidence}:/evidence"]
    for root in reference_roots:
        cmd += ["-v", f"{root}:{root}:ro"]
    cmd += ["-w", "/workspace", image, "agy",
           "--project", project_id, "--mode=accept-edits", "--sandbox",
           "--output-format", "json", "--print-timeout", "900s",
           "--log-file", "/evidence/agy.log"]
    if model:
        cmd += ["--model", model]
    proc = None
    try:
        # The lock spans clone + container startup. Once Docker reports Running,
        # docker_snapshot can see the lane label and future quota/login operations
        # fail closed as busy instead of racing the provider on the same account.
        with account_operation_lock(account):
            clone_volume(image, master_volume(account), volume)
            install_profile(image, volume, profile_file, project_id)
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=stdout_file,
                                    stderr=subprocess.STDOUT, text=True, start_new_session=True)
            wait_for_container_running(cname, proc)
        assert proc.stdin is not None
        proc.stdin.write(task_prompt(project, task)); proc.stdin.close()
    except Exception:
        stdout_file.close()
        sh("docker", "rm", "-f", cname, check=False, timeout=20)
        remove_volume(volume)
        raise
    project.running.add(tid)
    return Job(project, task, account, slot, model, attempt, proc, lane_dir, worktree, base,
               volume, log_file, stdout_file)


def log_identities(path: Path) -> list[str]:
    if not path.exists(): return []
    return [m.group(1).strip().lower() for m in IDENTITY_RE.finditer(path.read_text(errors="replace"))]

def finish_job(job: Job, accounts: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    job.stdout_file.close()
    tid = str(job.task["task_id"])
    job.project.running.discard(tid)
    meta: dict[str, Any] = {"task_id": tid, "account": job.account, "slot": job.slot,
                            "model": job.model, "model_family": rt.model_family(job.model),
                            "attempt": job.attempt, "exit_code": job.proc.returncode,
                            "worktree": str(job.worktree)}
    ids = log_identities(job.log_file)
    meta["identity_events"] = len(ids)
    expected = ((accounts.get("accounts") or {}).get(job.account) or {}).get("identity_sha256")
    if not expected:
        return False, "account has no bound identity hash", meta
    if not ids:
        return False, "no authenticated identity observed in agy log", meta
    if any(email_hash(x) != expected for x in ids):
        return False, "foreign account identity observed; lane rejected", meta
    if job.proc.returncode != 0:
        return False, f"agy container exited {job.proc.returncode}", meta
    if git_head(job.worktree) != job.base_sha:
        return False, "executor changed Git HEAD; lane rejected", meta
    paths = changed_paths(job.worktree, job.base_sha)
    meta["changed_paths"] = paths
    outside = [p for p in paths if not path_allowed(p, list(job.task.get("write_scope") or []))]
    if outside:
        meta["outside_write_scope"] = outside
        return False, "changes outside write_scope; lane rejected", meta
    if not paths:
        remove_volume(job.home_volume)
        return True, "success with no file changes", meta
    git("add", "-A", cwd=job.worktree)
    commit = git("-c", "user.name=AGY-MULTIPLEX-ISOLATED",
                 "-c", "user.email=agy-multiplex-isolated@local",
                 "commit", "-m", f"agyiso: {job.project.name} {tid}",
                 cwd=job.worktree, check=False)
    if commit.returncode != 0:
        return False, "could not commit lane changes", meta
    sha = git_head(job.worktree); meta["lane_commit"] = sha
    assert job.project.staging is not None
    pick = git("cherry-pick", sha, cwd=job.project.staging, check=False)
    if pick.returncode != 0:
        git("cherry-pick", "--abort", cwd=job.project.staging, check=False)
        meta["integration_error"] = (pick.stderr or pick.stdout).strip()
        return False, "cherry-pick conflict; project halted safely", meta
    meta["project_staging_head"] = git_head(job.project.staging)
    remove_volume(job.home_volume)
    return True, "integrated into isolated project staging", meta

def host_uid_gid() -> tuple[int, int]:
    uid = os.getuid() if hasattr(os, "getuid") else 1000
    gid = os.getgid() if hasattr(os, "getgid") else 1000
    return int(uid), int(gid)


def cmd_build(args: argparse.Namespace) -> int:
    dockerfile_dir = CODE_ROOT / "docker"
    uid, gid = host_uid_gid()
    sh("docker", "build",
       "--build-arg", f"AGY_VERSION={args.agy_version}",
       "--build-arg", f"AGY_UID={uid}",
       "--build-arg", f"AGY_GID={gid}",
       "-t", args.image, str(dockerfile_dir), capture=False)
    ver = image_version(args.image)
    print(json.dumps({"status": "built", "image": args.image, "agy_version": ver,
                      "host_uid": uid, "host_gid": gid}, indent=2))
    return 0 if ver == args.agy_version else 2


def ensure_unique_account_id(db: dict[str, Any], account: str) -> str:
    account = validate_id(account, "account id")
    for existing in (db.get("accounts") or {}):
        if existing.casefold() == account.casefold() and existing != account:
            raise ValueError(f"account id conflicts case-insensitively with {existing!r}")
    return account


def add_account(account: str) -> dict[str, Any]:
    account = validate_id(account, "account id")
    with account_operation_lock(account):
        with state_lock("accounts-registry"):
            db = account_db()
            account = ensure_unique_account_id(db, account)
            ensure_volume(master_volume(account))
            item = db["accounts"].setdefault(account, {})
            item.setdefault("bound", False)
            item.setdefault("credential_generation", 0)
            item["enabled"] = True
            item["volume"] = master_volume(account)
            item.setdefault("created_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            write_json(account_db_path(), db)
            return dict(item)


def cmd_init(args: argparse.Namespace) -> int:
    STATE.mkdir(parents=True, exist_ok=True); RUNS.mkdir(parents=True, exist_ok=True)
    added = []
    for account in args.accounts:
        add_account(account); added.append(account)
    if not account_db_path().exists():
        with state_lock("accounts-registry"):
            if not account_db_path().exists():
                write_json(account_db_path(), account_db())
    if not project_db_path().exists():
        with state_lock("projects-registry"):
            if not project_db_path().exists():
                write_json(project_db_path(), project_db())
    print(json.dumps({"status": "initialized", "data_root": str(DATA_ROOT),
                      "accounts_added": added, "configured_accounts": configured_account_ids(enabled_only=False)},
                     indent=2, ensure_ascii=False))
    return 0


def cmd_account_add(args: argparse.Namespace) -> int:
    item = add_account(args.account)
    print(json.dumps({"status": "account_added", "account": args.account,
                      "enabled": item.get("enabled", True), "volume": item.get("volume")}, indent=2))
    return 0


def set_account_enabled(account: str, enabled: bool) -> None:
    account = validate_id(account, "account id")
    with state_lock("accounts-registry"):
        db = account_db()
        if account not in (db.get("accounts") or {}):
            raise ValueError(f"unknown account: {account}")
        db["accounts"][account]["enabled"] = enabled
        write_json(account_db_path(), db)


def toggle_account_enabled(account: str) -> bool:
    account = validate_id(account, "account id")
    with state_lock("accounts-registry"):
        db = account_db()
        if account not in (db.get("accounts") or {}):
            raise ValueError(f"unknown account: {account}")
        enabled = db["accounts"][account].get("enabled", True) is not False
        new_value = not enabled
        db["accounts"][account]["enabled"] = new_value
        write_json(account_db_path(), db)
        return new_value


def cmd_account_enable(args: argparse.Namespace) -> int:
    set_account_enabled(args.account, True)
    print(json.dumps({"status": "account_enabled", "account": args.account}, indent=2)); return 0


def cmd_account_disable(args: argparse.Namespace) -> int:
    set_account_enabled(args.account, False)
    print(json.dumps({"status": "account_disabled", "account": args.account}, indent=2)); return 0


def cmd_account_remove(args: argparse.Namespace) -> int:
    account = validate_id(args.account, "account id")
    with account_operation_lock(account):
        with state_lock("accounts-registry"):
            db = account_db()
            item = (db.get("accounts") or {}).pop(account, None)
            if item is None:
                raise ValueError(f"unknown account: {account}")
            write_json(account_db_path(), db)
    print(json.dumps({"status": "account_removed", "account": account,
                      "credentials_deleted": False,
                      "preserved_volume": item.get("volume") or master_volume(account)}, indent=2))
    return 0


def cmd_accounts(_: argparse.Namespace) -> int:
    db = account_db(); rows = []
    for account in sorted((db.get("accounts") or {}), key=str.casefold):
        item = db["accounts"][account]
        exists = sh("docker", "volume", "inspect", master_volume(account), check=False).returncode == 0
        rows.append({"account": account, "enabled": item.get("enabled", True) is not False,
                     "bound": bool(item.get("identity_sha256")),
                     "masked_identity": item.get("masked_identity"), "volume_exists": exists,
                     "verified_at": item.get("verified_at")})
    print(json.dumps({"accounts": rows, "count": len(rows)}, indent=2, ensure_ascii=False)); return 0


def require_registered_account(account: str) -> dict[str, Any]:
    account = validate_id(account, "account id")
    item = (account_db().get("accounts") or {}).get(account)
    if item is None:
        raise ValueError(f"unknown account: {account}; add it first")
    if item.get("enabled", True) is False:
        raise ValueError(f"account is disabled: {account}")
    return item


def invalidate_account_binding(account: str) -> int:
    account = validate_id(account, "account id")
    with state_lock("accounts-registry"):
        db = account_db()
        item = (db.get("accounts") or {}).get(account)
        if item is None:
            raise ValueError(f"unknown account: {account}")
        generation = int(item.get("credential_generation") or 0) + 1
        item["credential_generation"] = generation
        item["bound"] = False
        for key in ("identity_sha256", "masked_identity", "verified_at", "binding_id"):
            item.pop(key, None)
        write_json(account_db_path(), db)
        return generation


def cmd_login(args: argparse.Namespace) -> int:
    require_registered_account(args.account)
    ensure_volume(master_volume(args.account))
    workspace = DATA_ROOT / "login-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    cname = f"agy-login-{slug(args.account)}-{uuid.uuid4().hex[:8]}"
    cmd = ["docker", "run", "--rm", "-it", "--name", cname,
           "--label", f"agy.multiplex.account={args.account}",
           "--label", "agy.multiplex.kind=login",
           "-v", f"{master_volume(args.account)}:/home/agy",
           "-v", f"{workspace}:/workspace", "-w", "/workspace", args.image, "agy"]
    print(f"Opening isolated login for {args.account}. Exit agy after authentication.")
    with account_operation_lock(args.account):
        invalidate_account_binding(args.account)
        try:
            return subprocess.call(cmd)
        finally:
            sh("docker", "rm", "-f", cname, check=False, timeout=20)


def cmd_bind(args: argparse.Namespace) -> int:
    try:
        with account_operation_lock(args.account, blocking=False):
            return _cmd_bind_locked(args)
    except BlockingIOError:
        print(json.dumps({"status": "blocked", "account": args.account,
                          "reason": "account operation already in progress"}, indent=2))
        return 4


def _cmd_bind_locked(args: argparse.Namespace) -> int:
    require_registered_account(args.account)
    ensure_volume(master_volume(args.account))
    probe = STATE / "probes" / args.account
    probe.mkdir(parents=True, exist_ok=True)
    profile_id = f"agyiso-bind-{slug(args.account)}"
    profile = probe / "project.json"; write_json(profile, project_profile(profile_id))
    install_profile(args.image, master_volume(args.account), profile, profile_id)
    log = probe / "identity.log"; out = probe / "stdout.json"
    log.unlink(missing_ok=True); out.unlink(missing_ok=True)
    workspace = probe / "workspace"; workspace.mkdir(exist_ok=True)
    cname = f"agy-verify-{slug(args.account)}-{uuid.uuid4().hex[:8]}"
    cmd = ["docker", "run", "--rm", "--name", cname, "-i",
           "--label", f"agy.multiplex.account={args.account}",
           "--label", "agy.multiplex.kind=verify",
           "-v", f"{master_volume(args.account)}:/home/agy",
           "-v", f"{workspace}:/workspace", "-v", f"{probe}:/evidence",
           "-w", "/workspace", args.image, "agy", "--project", profile_id,
           "--mode=plan", "--sandbox", "--output-format", "json",
           "--print-timeout", "60s", "--log-file", "/evidence/identity.log"]
    try:
        p = subprocess.run(cmd, input="Reply exactly: OK", text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
    except subprocess.TimeoutExpired:
        sh("docker", "rm", "-f", cname, check=False, timeout=20)
        print(json.dumps({"status": "blocked", "account": args.account,
                          "reason": "identity probe timed out"}, indent=2))
        return 2
    out.write_text(p.stdout or "", encoding="utf-8")
    ids = log_identities(log)
    if p.returncode != 0 or not ids:
        print(json.dumps({"status": "blocked", "account": args.account,
                          "reason": "identity probe failed; run login first",
                          "exit_code": p.returncode, "log": str(log)}, indent=2))
        return 2
    identity = ids[-1]
    if any(x != identity for x in ids):
        print(json.dumps({"status": "failed", "reason": "multiple identities observed"}, indent=2)); return 3
    with state_lock("accounts-registry"):
        db = account_db(); item = db["accounts"].setdefault(args.account, {})
        item.setdefault("enabled", True)
        generation = int(item.get("credential_generation") or 0) + 1
        item.update({"bound": True, "volume": master_volume(args.account),
                     "identity_sha256": email_hash(identity), "masked_identity": mask_email(identity),
                     "credential_generation": generation, "binding_id": uuid.uuid4().hex,
                     "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        write_json(account_db_path(), db)
    print(json.dumps({"status": "bound", "account": args.account,
                      "identity": mask_email(identity)}, indent=2))
    return 0


def add_project(name: str, repo: str | Path, plan: str | Path, *, goal: str = "",
                model: str | None = None, models: list[str] | None = None) -> Project:
    name = validate_id(name, "project id")
    repo_path = Path(repo).expanduser().resolve(); plan_path = Path(plan).expanduser().resolve()
    ordered_models = rt.normalize_models(models or [])
    if model and model.casefold() not in {m.casefold() for m in ordered_models}:
        ordered_models.insert(0, model)
    primary = ordered_models[0] if ordered_models else (model or None)
    record = {"name": name, "repo": str(repo_path), "plan": str(plan_path), "goal": goal or "",
              "model": primary, "models": ordered_models, "enabled": True,
              "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    project = project_from_record(record); validate_dag(project)
    record["task_count"] = len(project.tasks)
    with state_lock("projects-registry"):
        db = project_db()
        for existing in (db.get("projects") or {}):
            if existing.casefold() == name.casefold() and existing != name:
                raise ValueError(f"project id conflicts case-insensitively with {existing!r}")
        db["projects"][name] = {k: v for k, v in record.items() if k != "name"}
        write_json(project_db_path(), db)
    return project


def cmd_project_add(args: argparse.Namespace) -> int:
    fallbacks = list(args.fallback_model or [])
    models = ([args.model] if args.model else []) + fallbacks
    project = add_project(args.name, args.repo, args.plan, goal=args.goal or "",
                          model=args.model, models=models)
    print(json.dumps({"status": "project_added", "project": project.name,
                      "tasks": len(project.tasks), "repo": str(project.repo)}, indent=2)); return 0


def cmd_projects(_: argparse.Namespace) -> int:
    rows = []
    for raw in registered_project_records(enabled_only=False):
        rows.append({"name": raw["name"], "enabled": raw.get("enabled", True) is not False,
                     "repo": raw.get("repo"), "plan": raw.get("plan"),
                     "model": raw.get("model"), "models": raw.get("models") or []})
    print(json.dumps({"projects": rows, "count": len(rows)}, indent=2, ensure_ascii=False)); return 0


def set_project_enabled(name: str, enabled: bool) -> None:
    name = validate_id(name, "project id")
    with state_lock("projects-registry"):
        db = project_db()
        if name not in (db.get("projects") or {}): raise ValueError(f"unknown project: {name}")
        db["projects"][name]["enabled"] = enabled
        write_json(project_db_path(), db)


def toggle_project_enabled(name: str) -> bool:
    name = validate_id(name, "project id")
    with state_lock("projects-registry"):
        db = project_db()
        if name not in (db.get("projects") or {}): raise ValueError(f"unknown project: {name}")
        enabled = db["projects"][name].get("enabled", True) is not False
        new_value = not enabled
        db["projects"][name]["enabled"] = new_value
        write_json(project_db_path(), db)
        return new_value


def cmd_project_enable(args: argparse.Namespace) -> int:
    set_project_enabled(args.name, True)
    print(json.dumps({"status": "project_enabled", "project": args.name}, indent=2)); return 0


def cmd_project_disable(args: argparse.Namespace) -> int:
    set_project_enabled(args.name, False)
    print(json.dumps({"status": "project_disabled", "project": args.name}, indent=2)); return 0


def cmd_project_remove(args: argparse.Namespace) -> int:
    name = validate_id(args.name, "project id")
    with state_lock("projects-registry"):
        db = project_db()
        item = (db.get("projects") or {}).pop(name, None)
        if item is None: raise ValueError(f"unknown project: {name}")
        write_json(project_db_path(), db)
    print(json.dumps({"status": "project_removed", "project": name,
                      "repository_deleted": False}, indent=2)); return 0

def cmd_doctor(args: argparse.Namespace) -> int:
    adb = account_db(); pdb = project_db()
    account_rows = adb.get("accounts") or {}; project_rows = pdb.get("projects") or {}
    bound = sum(1 for x in account_rows.values() if x.get("identity_sha256"))
    enabled_accounts = sum(1 for x in account_rows.values() if x.get("enabled", True) is not False)
    enabled_projects = sum(1 for x in project_rows.values() if x.get("enabled", True) is not False)
    docker = docker_ok(); ver = image_version(args.image) if docker else None
    payload = {"tool": "AGY-MULTIPLEX-ISOLATED", "docker_ok": docker,
               "image": args.image, "image_agy_version": ver, "expected_agy_version": args.agy_version,
               "configured_accounts": len(account_rows), "enabled_accounts": enabled_accounts,
               "bound_accounts": bound, "configured_projects": len(project_rows),
               "enabled_projects": enabled_projects, "default_per_account_slots": DEFAULT_SLOTS,
               "data_root": str(DATA_ROOT), "live_enabled": (STATE / "LIVE_ENABLED").exists(),
               "topology": "dynamic accounts x configurable slots across dynamic projects"}
    print(json.dumps(payload, indent=2)); return 0 if docker and ver == args.agy_version else 1


def effective_capacity(data: dict[str, Any], accounts: list[str], slots: int, override: int | None = None) -> int:
    requested = int(override if override is not None else data.get("max_workers", len(accounts) * slots))
    if requested < 1:
        raise ValueError("max_workers must be >= 1")
    return min(requested, len(accounts) * slots)


def routing_policy(data: dict[str, Any]) -> dict[str, Any]:
    raw = data.get("routing") or {}
    if not isinstance(raw, dict):
        raise ValueError("manifest.routing must be an object")
    min_remaining = float(raw.get("min_remaining_percent", 1.0))
    if not 0.0 <= min_remaining < 100.0:
        raise ValueError("routing.min_remaining_percent must be between 0 and 100")
    max_attempts = int(raw.get("max_route_attempts", 12))
    if max_attempts < 1:
        raise ValueError("routing.max_route_attempts must be >= 1")
    return {
        "quota_aware": raw.get("quota_aware", True) is not False,
        "min_remaining_percent": min_remaining,
        "max_route_attempts": max_attempts,
        "quota_timeout_seconds": max(8, int(raw.get("quota_timeout_seconds", 25))),
        "quota_probe_workers": max(1, int(raw.get("quota_probe_workers", 2))),
    }


def project_task_models(project: Project, task: dict[str, Any]) -> list[str | None]:
    return rt.task_models(project.models or ([project.model] if project.model else []), task)


def probe_quota_matrix(accounts: list[str], image: str, *, timeout: int = 25, workers: int = 2) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    from concurrent.futures import ThreadPoolExecutor
    from ui import quota_usage

    def probe(account: str) -> tuple[str, list[dict[str, Any]], str | None]:
        clone = f"agy-route-probe-{slug(account)}-{uuid.uuid4().hex[:8]}"
        try:
            with account_operation_lock(account):
                clone_volume(image, master_volume(account), clone)
            groups = quota_usage.probe_account_usage(
                account=account, account_volume=clone, image=image, timeout=timeout
            )
            return account, groups, None
        except Exception as exc:
            return account, [], f"{type(exc).__name__}:{exc}"
        finally:
            remove_volume(clone)

    matrix: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(accounts)))) as executor:
        for account, groups, error in executor.map(probe, accounts):
            if groups:
                matrix[account] = groups
            if error:
                errors[account] = error
    return matrix, errors


def quota_summary(matrix: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for account, groups in matrix.items():
        row: dict[str, float] = {}
        for family in ("gemini", "claude_gpt"):
            remaining = rt.quota_remaining(groups, "gemini" if family == "gemini" else "claude")
            if remaining is not None:
                row[family] = remaining
        out[account] = row
    return out


def cmd_validate(args: argparse.Namespace) -> int:
    manifest_path = manifest_path_arg(args.manifest)
    data, projects = load_manifest(manifest_path)
    for project in projects: validate_dag(project)
    accounts = resolve_accounts(data); slots = resolve_slots(data)
    capacity = effective_capacity(data, accounts, slots)
    print(json.dumps({"ok": True, "source": str(manifest_path) if manifest_path else "registries",
                      "projects": [{"name": p.name, "tasks": len(p.tasks)} for p in projects],
                      "project_count": len(projects), "accounts": accounts, "account_count": len(accounts),
                      "per_account_slots": slots, "max_parallel_lanes": capacity}, indent=2, ensure_ascii=False))
    return 0

def projected_wave(projects: list[Project], accounts: list[str], slots: int, capacity: int) -> list[dict[str, Any]]:
    pool = [(a, s) for a in accounts for s in range(1, slots + 1)]
    wave: list[dict[str, Any]] = []; cursor = 0
    used: dict[str, list[dict[str, Any]]] = {p.name: [] for p in projects}
    while pool and len(wave) < capacity:
        progressed = False
        for _ in range(len(projects)):
            p = projects[cursor % len(projects)]; cursor += 1
            ready = [t for t in p.ready() if not any(scope_overlap(list(t.get("write_scope") or []), list(x.get("write_scope") or [])) for x in used[p.name])]
            if not ready: continue
            task = ready[0]; used[p.name].append(task); p.running.add(str(task["task_id"]))
            account, slot = pool.pop(0)
            models = project_task_models(p, task)
            wave.append({"project": p.name, "task_id": task["task_id"], "account": account,
                         "account_slot": slot, "model": models[0],
                         "write_scope": task.get("write_scope")})
            progressed = True
            if not pool or len(wave) >= capacity: break
        if not progressed: break
    for p in projects: p.running.clear()
    return wave

def cmd_dry_run(args: argparse.Namespace) -> int:
    manifest_path = manifest_path_arg(args.manifest)
    data, projects = load_manifest(manifest_path)
    accounts = resolve_accounts(data); slots = resolve_slots(data)
    capacity = effective_capacity(data, accounts, slots)
    wave = projected_wave(projects, accounts, slots, capacity)
    print(json.dumps({"status": "dry_run", "source": str(manifest_path) if manifest_path else "registries",
                      "projects": len(projects), "accounts": len(accounts),
                      "per_account_slots": slots, "capacity": capacity,
                      "first_wave": wave}, indent=2, ensure_ascii=False))
    return 0

def free_slot(accounts: list[str], slots: int, active: list[Job], cursor: int) -> tuple[str, int, int] | None:
    used = {(j.account, j.slot) for j in active}
    candidates = [(a, s) for a in accounts for s in range(1, slots + 1) if (a, s) not in used]
    if not candidates: return None
    idx = cursor % len(candidates)
    account, slot = candidates[idx]
    return account, slot, cursor + 1


def select_task(projects: list[Project], cursor: int) -> tuple[Project, dict[str, Any], int] | None:
    for step in range(len(projects)):
        p = projects[(cursor + step) % len(projects)]
        for task in p.ready():
            if not running_conflict(p, task):
                return p, task, (cursor + step + 1) % len(projects)
    return None


def select_routable_task(
    projects: list[Project],
    cursor: int,
    *,
    accounts: list[str],
    slots: int,
    active: list[Job],
    quota_matrix: dict[str, list[dict[str, Any]]],
    route_exclusions: dict[tuple[str, str], set[tuple[str, str]]],
    blocked_models: dict[str, set[str]],
    min_remaining_percent: float,
    route_cursor: int,
) -> tuple[Project, dict[str, Any], rt.RouteChoice, int] | None:
    active_pairs = {(job.account, job.slot) for job in active}
    for step in range(len(projects)):
        project = projects[(cursor + step) % len(projects)]
        for task in project.ready():
            if running_conflict(project, task):
                continue
            tid = str(task["task_id"])
            route = rt.choose_route(
                accounts=accounts,
                slots=slots,
                active_pairs=active_pairs,
                models=project_task_models(project, task),
                quota_matrix=quota_matrix,
                exclusions=route_exclusions.get((project.name, tid), set()),
                blocked_models=blocked_models.get(project.name, set()),
                min_remaining_percent=min_remaining_percent,
                cursor=route_cursor,
            )
            if route is not None:
                return project, task, route, (cursor + step + 1) % len(projects)
    return None


def job_route_failure_kind(job: Job) -> str | None:
    text_parts: list[str] = []
    for path in (job.lane_dir / "stdout.log", job.log_file):
        try:
            text_parts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return rt.route_failure_kind("\n".join(text_parts))


def event(run_root: Path, kind: str, **payload: Any) -> None:
    rec = {"time": time.time(), "event": kind, **payload}
    with (run_root / "events.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

def kill_run_containers(run_id: str) -> None:
    p = sh("docker", "ps", "-q", "--filter", f"label=agy.multiplex.run={run_id}", check=False)
    ids = [x for x in p.stdout.splitlines() if x.strip()]
    if ids:
        sh("docker", "rm", "-f", *ids, check=False)


def remove_run_volumes(run_id: str) -> None:
    p = sh("docker", "volume", "ls", "-q", "--filter", f"name=agy-mx-{slug(run_id)}", check=False)
    for volume in p.stdout.splitlines():
        remove_volume(volume)


def project_summary(p: Project) -> dict[str, Any]:
    all_ids = set(p.tasks)
    pending = sorted(all_ids - p.completed - p.failed)
    return {"name": p.name, "repository": str(p.repo), "base_sha": p.base_sha,
            "staging_worktree": str(p.staging) if p.staging else None,
            "staging_branch": p.branch, "completed": sorted(p.completed),
            "failed": sorted(p.failed), "pending": pending}


def cmd_enable_live(args: argparse.Namespace) -> int:
    phrase = "I ACCEPT LAB GATE REQUIREMENTS"
    if args.ack != phrase:
        print(json.dumps({"status": "blocked", "required_ack": phrase}, indent=2)); return 2
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "LIVE_ENABLED").write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n")
    print(json.dumps({"status": "live_enabled", "note": "Enablement does not prove L1-L7; use only after lab validation."}, indent=2))
    return 0


def cmd_disable_live(_: argparse.Namespace) -> int:
    (STATE / "LIVE_ENABLED").unlink(missing_ok=True)
    print(json.dumps({"status": "live_disabled"}, indent=2)); return 0

def cmd_run(args: argparse.Namespace) -> int:
    if not (STATE / "LIVE_ENABLED").exists() and not args.lab:
        print(json.dumps({"status": "blocked", "reason": "live gate is disabled; use --lab for controlled testing or enable-live after validation"}, indent=2))
        return 2
    manifest_path = manifest_path_arg(args.manifest)
    data, projects = load_manifest(manifest_path)
    accounts = resolve_accounts(data); slots = resolve_slots(data)
    capacity = effective_capacity(data, accounts, slots, args.max_workers)
    policy = routing_policy(data)
    image = str(data.get("image") or args.image)
    expected_version = str(data.get("agy_version") or args.agy_version)
    actual_version = image_version(image)
    if actual_version != expected_version:
        print(json.dumps({"status": "blocked", "reason": "agy image version drift", "expected": expected_version, "actual": actual_version}, indent=2)); return 2
    db = account_db(); rows = db.get("accounts") or {}
    unknown = [a for a in accounts if a not in rows]
    disabled = [a for a in accounts if a in rows and rows[a].get("enabled", True) is False]
    unbound = [a for a in accounts if a in rows and not rows[a].get("identity_sha256")]
    if unknown or disabled or unbound:
        print(json.dumps({"status": "blocked", "reason": "account pool is not ready",
                          "unknown": unknown, "disabled": disabled, "unbound": unbound}, indent=2)); return 2

    run_id = f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    run_root = RUNS / run_id; run_root.mkdir(parents=True, exist_ok=False)
    if manifest_path is not None:
        shutil.copy2(manifest_path, run_root / "manifest.input.json")

    quota_matrix: dict[str, list[dict[str, Any]]] = {}
    quota_errors: dict[str, str] = {}
    if policy["quota_aware"]:
        quota_matrix, quota_errors = probe_quota_matrix(
            accounts, image,
            timeout=policy["quota_timeout_seconds"],
            workers=policy["quota_probe_workers"],
        )
    write_json(run_root / "routing.quota.json", {
        "quota_aware": policy["quota_aware"],
        "min_remaining_percent": policy["min_remaining_percent"],
        "remaining_by_family": quota_summary(quota_matrix),
        "probe_errors": quota_errors,
    })
    write_json(run_root / "manifest.effective.json", {
        "accounts": accounts, "per_account_slots": slots, "max_workers": capacity,
        "image": image, "agy_version": expected_version, "routing": policy,
        "projects": [{"name": p.name, "repo": str(p.repo), "goal": p.goal,
                      "model": p.model, "models": p.models, "task_count": len(p.tasks)} for p in projects]})
    for project in projects:
        setup_project(project, run_root, run_id)
    event(run_root, "RUN_STARTED", capacity=capacity, accounts=accounts, slots=slots,
          project_count=len(projects), lab=bool(args.lab), routing=policy,
          quota_summary=quota_summary(quota_matrix))

    active: list[Job] = []
    project_cursor = 0
    route_cursor = 0
    route_exclusions: dict[tuple[str, str], set[tuple[str, str]]] = {}
    blocked_models: dict[str, set[str]] = {}
    attempts: dict[tuple[str, str], int] = {}
    try:
        while True:
            while len(active) < capacity:
                selected = select_routable_task(
                    projects, project_cursor, accounts=accounts, slots=slots, active=active,
                    quota_matrix=quota_matrix, route_exclusions=route_exclusions,
                    blocked_models=blocked_models,
                    min_remaining_percent=policy["min_remaining_percent"],
                    route_cursor=route_cursor,
                )
                if selected is None:
                    break
                project, task, route, project_cursor = selected
                route_cursor = route.cursor
                tid = str(task["task_id"]); key = (project.name, tid)
                attempts[key] = attempts.get(key, 0) + 1
                job = launch_job(
                    project, task, route.account, route.slot, image, run_root, run_id,
                    model=route.model, attempt=attempts[key],
                )
                active.append(job)
                event(run_root, "LANE_STARTED", project=project.name, task_id=tid,
                      account=route.account, slot=route.slot, model=route.model,
                      model_family=route.family, quota_remaining_percent=route.remaining_percent,
                      attempt=attempts[key], pid=job.proc.pid)

            if not active:
                ready = [(p, t) for p in projects for t in p.ready() if not running_conflict(p, t)]
                if ready:
                    project, task = ready[0]
                    tid = str(task["task_id"]); key = (project.name, tid)
                    project.failed.add(tid)
                    event(run_root, "LANE_FAILED", project=project.name, task_id=tid,
                          message="no eligible account/model route",
                          attempts=attempts.get(key, 0),
                          models=project_task_models(project, task))
                    continue
                break

            time.sleep(0.75)
            for job in list(active):
                if job.proc.poll() is None:
                    continue
                failure_kind = job_route_failure_kind(job) if job.proc.returncode else None
                ok, message, meta = finish_job(job, db)
                remove_volume(job.home_volume)
                tid = str(job.task["task_id"]); key = (job.project.name, tid)
                if ok:
                    job.project.completed.add(tid)
                    event(run_root, "LANE_COMPLETED", project=job.project.name, message=message, **meta)
                elif failure_kind and attempts.get(key, 0) < policy["max_route_attempts"]:
                    model_key = (job.model or "").casefold()
                    route_exclusions.setdefault(key, set()).add((job.account, model_key))
                    if failure_kind == "model_unavailable" and model_key:
                        blocked_models.setdefault(job.project.name, set()).add(model_key)
                    event(run_root, "LANE_ROUTE_RETRY", project=job.project.name,
                          message=message, failure_kind=failure_kind, **meta)
                else:
                    job.project.failed.add(tid)
                    if failure_kind:
                        meta["failure_kind"] = failure_kind
                    event(run_root, "LANE_FAILED", project=job.project.name, message=message, **meta)
                write_json(job.lane_dir / "result.json", {
                    "ok": ok, "message": message, "route_failure_kind": failure_kind, **meta
                })
                active.remove(job)
    except KeyboardInterrupt:
        event(run_root, "RUN_INTERRUPTED"); kill_run_containers(run_id); remove_run_volumes(run_id); raise
    except Exception:
        event(run_root, "RUN_ABORTED"); kill_run_containers(run_id); remove_run_volumes(run_id); raise

    summaries = [project_summary(p) for p in projects]
    success = all(not x["failed"] and not x["pending"] for x in summaries)
    result = {"status": "success" if success else "partial_failure", "run_id": run_id,
              "capacity": capacity, "accounts": accounts, "per_account_slots": slots,
              "routing": {"policy": policy, "attempts": {f"{p}/{t}": n for (p, t), n in attempts.items()}},
              "projects": summaries, "review_required": True,
              "pushed": False, "merged_to_main": False, "deployed": False}
    write_json(run_root / "result.json", result); event(run_root, "RUN_FINISHED", status=result["status"])
    print(json.dumps(result, indent=2, ensure_ascii=False)); return 0 if success else 3

def cmd_status(args: argparse.Namespace) -> int:
    run = RUNS / args.run_id
    result = run / "result.json"
    events = run / "events.jsonl"
    if result.exists():
        print(result.read_text(encoding="utf-8"), end=""); return 0
    if events.exists():
        print(events.read_text(encoding="utf-8"), end=""); return 0
    print(json.dumps({"status": "error", "reason": "unknown run"}, indent=2)); return 1


def cmd_cleanup(args: argparse.Namespace) -> int:
    kill_run_containers(args.run_id)
    remove_run_volumes(args.run_id)
    print(json.dumps({"status": "cleaned", "run_id": args.run_id}, indent=2)); return 0

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agy-multiplex-isolated",
        description="Container-isolated dynamic multi-account, multi-project Antigravity scheduler.")
    p.add_argument("--image", default=DEFAULT_IMAGE)
    p.add_argument("--agy-version", default="1.2.14")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build-image", help="Build the pinned Linux agy image"); b.set_defaults(func=cmd_build)
    i = sub.add_parser("init", help="Initialize user data and optionally add account IDs")
    i.add_argument("accounts", nargs="*", default=[]); i.set_defaults(func=cmd_init)

    a = sub.add_parser("accounts", help="List configured accounts"); a.set_defaults(func=cmd_accounts)
    aa = sub.add_parser("add-account", help="Add one account boundary"); aa.add_argument("account"); aa.set_defaults(func=cmd_account_add)
    ae = sub.add_parser("enable-account"); ae.add_argument("account"); ae.set_defaults(func=cmd_account_enable)
    ad = sub.add_parser("disable-account"); ad.add_argument("account"); ad.set_defaults(func=cmd_account_disable)
    ar = sub.add_parser("remove-account", help="Remove registry entry but preserve credential volume")
    ar.add_argument("account"); ar.set_defaults(func=cmd_account_remove)
    l = sub.add_parser("login"); l.add_argument("account"); l.set_defaults(func=cmd_login)
    bd = sub.add_parser("bind"); bd.add_argument("account"); bd.set_defaults(func=cmd_bind)

    ps = sub.add_parser("projects", help="List registered projects"); ps.set_defaults(func=cmd_projects)
    pa = sub.add_parser("add-project", help="Register or update a project")
    pa.add_argument("--name", required=True); pa.add_argument("--repo", required=True); pa.add_argument("--plan", required=True)
    pa.add_argument("--goal", default=""); pa.add_argument("--model")
    pa.add_argument("--fallback-model", action="append", default=[],
                    help="Fallback model ID; repeat to define priority order")
    pa.set_defaults(func=cmd_project_add)
    pe = sub.add_parser("enable-project"); pe.add_argument("name"); pe.set_defaults(func=cmd_project_enable)
    pd = sub.add_parser("disable-project"); pd.add_argument("name"); pd.set_defaults(func=cmd_project_disable)
    pr = sub.add_parser("remove-project", help="Remove registration without deleting repository")
    pr.add_argument("name"); pr.set_defaults(func=cmd_project_remove)

    d = sub.add_parser("doctor"); d.set_defaults(func=cmd_doctor)
    v = sub.add_parser("validate"); v.add_argument("--manifest"); v.set_defaults(func=cmd_validate)
    dr = sub.add_parser("dry-run"); dr.add_argument("--manifest"); dr.set_defaults(func=cmd_dry_run)
    r = sub.add_parser("run"); r.add_argument("--manifest"); r.add_argument("--max-workers", type=int); r.add_argument("--lab", action="store_true"); r.set_defaults(func=cmd_run)
    e = sub.add_parser("enable-live"); e.add_argument("--ack", required=True); e.set_defaults(func=cmd_enable_live)
    x = sub.add_parser("disable-live"); x.set_defaults(func=cmd_disable_live)
    st = sub.add_parser("status"); st.add_argument("run_id"); st.set_defaults(func=cmd_status)
    c = sub.add_parser("cleanup"); c.add_argument("run_id"); c.set_defaults(func=cmd_cleanup)
    return p

def main() -> int:
    args = parser().parse_args()
    try:
        return int(args.func(args))
    except subprocess.TimeoutExpired as exc:
        print(json.dumps({"status": "error", "type": "TimeoutExpired", "error": str(exc)}, indent=2), file=sys.stderr); return 4
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "type": type(exc).__name__, "error": str(exc)}, indent=2), file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
