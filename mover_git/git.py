import json
import subprocess
from pathlib import Path
from urllib.parse import urlparse


def normalize_github_url(remote_url: str) -> str | None:
    """
    convert a Git remote URL into a GitHub web address
    :param remote_url: git remote URL
    :returns: normalized GitHub URL or None
    """
    remote_url = remote_url.strip()
    if remote_url.startswith("git@github.com:"):
        path = remote_url.split(":", 1)[1]
    elif remote_url.startswith(("https://", "http://", "ssh://")):
        parsed = urlparse(remote_url)
        if parsed.hostname != "github.com":
            return None
        path = parsed.path.lstrip("/")
    else:
        return None
    return f"https://github.com/{path.removesuffix('.git').rstrip('/')}"


def parse_status_paths(status: str) -> list[str]:
    """
    parse paths from Git porcelain status output
    :param status: porcelain version one status output
    :returns: changed repository relative paths
    """
    paths = []
    for line in status.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path.strip('"'))
    return paths


def repo_relative_paths(repo: Path, paths: list[Path]) -> list[str]:
    """
    convert paths into repository relative pathspecs
    :param repo: repository root path
    :param paths: paths inside the repository
    :returns: sorted unique Git pathspecs
    """
    resolved_repo = repo.resolve()
    result = {path.resolve().relative_to(resolved_repo).as_posix() for path in paths}
    return sorted(result)


def _git(repo: Path, *args: str, data: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        input=data,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def check_staging_support(repo: Path) -> None:
    for command in ("add", "commit"):
        result = subprocess.run(["git", command, "-h"], cwd=repo, capture_output=True)
        if b"--pathspec-from-file" not in result.stdout + result.stderr:
            raise RuntimeError("Upgrade Git: staging requires --pathspec-from-file support.")


def pending_git_file(repo: Path) -> Path:
    git_dir = _git(repo, "rev-parse", "--absolute-git-dir").decode("utf-8").strip()
    return Path(git_dir) / "mover-pending.json"


def finish_pending_git(repo: Path) -> bool:
    """Resume durable Git work without moving files or committing twice."""
    journal = pending_git_file(repo)
    if not journal.exists():
        return False
    state = json.loads(journal.read_text(encoding="utf-8"))
    result = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=repo, capture_output=True)
    head = result.stdout if result.returncode == 0 else b""
    # A successful commit changes HEAD even if the process stopped before push.
    if head.decode().strip() == state["head"]:
        payload = b"".join(p.encode("utf-8") + b"\0" for p in state["paths"])
        _git(
            repo,
            "--literal-pathspecs",
            "add",
            "--pathspec-from-file=-",
            "--pathspec-file-nul",
            data=payload,
        )
        changed = _git(repo, "diff", "--cached", "--name-only", "-z")
        if not set(changed.split(b"\0")) & {p.encode("utf-8") for p in state["paths"]}:
            journal.unlink()
            return True
        if changed:
            _git(
                repo,
                "--literal-pathspecs",
                "commit",
                "-m",
                state["message"],
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
                data=payload,
            )
    else:
        # Refuse to treat an unrelated commit as completion of our batch.
        message = _git(repo, "log", "-1", "--format=%B").decode("utf-8").strip()
        parents = _git(repo, "log", "-1", "--format=%P").decode().strip()
        if message != state["message"].strip() or parents != state["head"]:
            raise RuntimeError(
                "HEAD changed outside the pending transfer; inspect before retrying."
            )
    _git(repo, "push", "-u", "origin", state["branch"])
    journal.unlink()
    return True


def stage_commit_push(repo: Path, paths: list[Path], message: str, branch: str) -> None:
    pathspecs = repo_relative_paths(repo, paths)
    if not pathspecs:
        return
    journal = pending_git_file(repo)
    if not journal.exists():
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"], cwd=repo, capture_output=True
        )
        state = {
            "paths": pathspecs,
            "message": message,
            "branch": branch,
            "head": head.stdout.decode().strip() if head.returncode == 0 else "",
        }
        temporary = journal.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=True), encoding="utf-8")
        temporary.replace(journal)
    finish_pending_git(repo)
