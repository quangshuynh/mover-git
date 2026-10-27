import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from mover_git.core import MAX_BATCH_SIZE_BYTES, MAX_FILE_SIZE_BYTES, human_size
from mover_git.storage import DiskSet, records, remove_journal

PUSH_BUDGET_BYTES = 1024**3
GIT_CONFIG = ["-c", "maintenance.auto=false", "-c", "gc.auto=0",
              "-c", "core.bigFileThreshold=8m", "-c", "pack.windowMemory=64m",
              "-c", "pack.threads=2"]


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
        ["git", *GIT_CONFIG, *args],
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


def write_journal(path: Path, state: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def file_digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def remote_head(repo: Path, branch: str) -> str:
    output = _git(repo, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    return output.split()[0].decode("ascii") if output.strip() else ""


def object_bytes(repo: Path, commit: str, previous: str) -> int:
    """Stream object metadata through disk, never collect blobs or object lists in RAM."""
    args = ["rev-list", "--objects", "--no-object-names", commit]
    if previous:
        args += ["--not", previous]
    with tempfile.TemporaryFile() as objects, tempfile.TemporaryFile() as sizes:
        for command, source, destination in (
            (args, None, objects),
            (["cat-file", "--batch-check=%(objecttype) %(objectsize)"], objects, sizes),
        ):
            result = subprocess.run(["git", *GIT_CONFIG, *command], cwd=repo,
                                    stdin=source, stdout=destination, stderr=subprocess.PIPE)
            if result.returncode:
                raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
            destination.seek(0)
        total = 0
        for line in sizes:
            kind, size = line.split()
            size = int(size)
            if kind == b"blob" and size > MAX_FILE_SIZE_BYTES:
                raise RuntimeError(
                    f"Commit {commit[:8]} contains a blob exceeding GitHub's 100 MiB "
                    "standard Git limit. Review unpublished history manually; no LFS was enabled."
                )
            # Include conservative per-object packing overhead, even for empty blobs.
            total += size + 64
        return total


def push_unpublished(repo: Path, branch: str, progress=lambda message: None,
                     budget: int = PUSH_BUDGET_BYTES, max_attempts: int = 4,
                     check_cancel=lambda: None) -> None:
    """Publish fast-forward checkpoints, using remote refs as durable acknowledgements."""
    if not 0 < budget <= PUSH_BUDGET_BYTES or max_attempts < 1:
        raise ValueError("Push budget must be at most 1 GiB and attempts must be positive")
    _git(repo, "check-ref-format", f"refs/heads/{branch}")
    head_result = subprocess.run(["git", "rev-parse", "--verify", "HEAD"],
                                 cwd=repo, capture_output=True)
    if head_result.returncode:
        return
    if _git(repo, "symbolic-ref", "--short", "HEAD").decode().strip() != branch:
        raise RuntimeError(f"Switch to branch {branch} before publishing its commits")
    target = head_result.stdout.decode().strip()
    failures = 0
    step_limit = None
    while True:
        check_cancel()
        previous = remote_head(repo, branch)
        if previous == target:
            return
        if previous:
            try:
                _git(repo, "merge-base", "--is-ancestor", previous, target)
            except RuntimeError as exc:
                raise RuntimeError("Remote branch diverged or is unknown locally. Fetch and "
                                   "review it before retrying; local commits are preserved.") from exc
        revision = f"{previous}..{target}" if previous else target
        checkpoint, count, total = "", 0, 0
        # First-parent checkpoints include all side-branch objects at a merge.
        with tempfile.TemporaryFile() as commits:
            ancestry = ["--ancestry-path"] if previous else []
            result = subprocess.run(
                ["git", *GIT_CONFIG, "rev-list", "--reverse", "--first-parent", *ancestry, revision],
                cwd=repo, stdout=commits, stderr=subprocess.PIPE,
            )
            if result.returncode:
                raise RuntimeError(result.stderr.decode(errors="replace"))
            commits.seek(0)
            base = previous
            for line in commits:
                commit = line.decode().strip()
                check_cancel()
                size = object_bytes(repo, commit, base)
                if total + size > budget:
                    if not count:
                        raise RuntimeError(f"Commit {commit[:8]} needs {human_size(size)} of "
                                           "objects, exceeding the 1 GiB push budget. Review "
                                           "this commit manually; history was not rewritten.")
                    break
                total += size
                checkpoint, base = commit, commit
                count += 1
                if step_limit and count >= step_limit:
                    break
        if not checkpoint:
            raise RuntimeError("No publishable checkpoint; inspect local and remote history")
        progress(f"Push: {count} commit(s), {human_size(total)} objects -> {checkpoint[:8]}")
        check_cancel()
        error = "Remote verification did not match the pushed checkpoint"
        try:
            _git(repo, "-c", "push.followTags=false", "push", "--no-follow-tags",
                 "origin", f"{checkpoint}:refs/heads/{branch}")
        except RuntimeError as exc:
            error = str(exc)
        # A lost response can still mean the server accepted the push.
        verified = remote_head(repo, branch)
        check_cancel()
        if verified == checkpoint:
            progress(f"Verified origin/{branch} at {checkpoint[:8]}")
            failures = 0
            continue
        if verified != previous:
            raise RuntimeError("Remote changed during push. Fetch and review before retrying.")
        failures += 1
        if failures >= max_attempts:
            raise RuntimeError(f"Push failed after {failures} attempts: {error}. "
                               "Check access/network or remote hooks, then retry; commits are saved.")
        step_limit = max(1, count // 2)
        progress(f"Push retry {failures}/{max_attempts}: at most {step_limit} commit(s). {error}")


def finish_pending_git(repo: Path, progress=lambda message: None, push: bool = True,
                       checkpoint=lambda: None) -> bool:
    """Resume durable Git work without moving files or committing twice."""
    journal = pending_git_file(repo)
    if not journal.exists():
        return False
    state = json.loads(journal.read_text(encoding="utf-8"))
    if _git(repo, "symbolic-ref", "--short", "HEAD").decode().strip() != state["branch"]:
        raise RuntimeError(f"Switch back to {state['branch']} before resuming this transfer")
    result = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=repo, capture_output=True)
    head = result.stdout if result.returncode == 0 else b""
    # A successful commit changes HEAD even if the process stopped before push.
    if head.decode().strip() == state["head"]:
        def selected_records():
            if "paths" in state:
                for path in state["paths"]:
                    yield {"path": path, "hash": state.get("hashes", {}).get(path)}
            else:
                yield from records(state, journal)

        with DiskSet() as selected, tempfile.TemporaryDirectory(prefix="mover-git-") as folder:
            pathspec = Path(folder) / "paths"
            with pathspec.open("wb") as stream:
                for record in selected_records():
                    checkpoint()
                    path, expected = record["path"], record["hash"]
                    actual = file_digest(repo / path)
                    if expected and (actual != expected or
                                     (repo / path).stat().st_size > MAX_FILE_SIZE_BYTES):
                        raise RuntimeError(f"Pending file changed: {path}; restore it before retrying")
                    selected.add(path)
                    stream.write(path.encode("utf-8") + b"\0")
            path_args = (f"--pathspec-from-file={pathspec}", "--pathspec-file-nul")
            checkpoint()
            _git(repo, "--literal-pathspecs", "add", *path_args)
            checkpoint()
            changed = False
            with tempfile.TemporaryFile() as names:
                result = subprocess.run(["git", *GIT_CONFIG, "diff", "--cached", "--name-only", "-z"],
                                        cwd=repo, stdout=names, stderr=subprocess.PIPE)
                if result.returncode:
                    raise RuntimeError(result.stderr.decode(errors="replace"))
                names.seek(0)
                pending = b""
                while chunk := names.read(65536):
                    parts = (pending + chunk).split(b"\0")
                    pending = parts.pop()
                    if any(path.decode("utf-8") in selected for path in parts):
                        changed = True
                        break
            if changed:
                checkpoint()
                _git(repo, "--literal-pathspecs", "commit", "--quiet", "-m", state["message"], *path_args)

    else:
        # Refuse to treat an unrelated commit as completion of our batch.
        message = _git(repo, "log", "-1", "--format=%B").decode("utf-8").strip()
        parents = _git(repo, "log", "-1", "--format=%P").decode().strip()
        if message != state["message"].strip() or parents != state["head"]:
            raise RuntimeError(
                "HEAD changed outside the pending transfer; inspect before retrying."
            )
    checkpoint()
    if push:
        push_unpublished(repo, state["branch"], progress, check_cancel=checkpoint)
    remove_journal(journal)
    return True


def stage_commit_push(repo: Path, paths: list[Path], message: str, branch: str,
                      progress=lambda message: None, push: bool = True,
                      checkpoint=lambda: None) -> None:
    journal = pending_git_file(repo)
    if journal.exists():
        pending = json.loads(journal.read_text(encoding="utf-8"))
        finish_pending_git(repo, progress, push, checkpoint=checkpoint)
        if (pending["message"], pending["branch"]) == (message, branch):
            return
    total = count = 0
    manifest = journal.with_suffix(".jsonl")
    temporary = manifest.with_suffix(".building")
    with DiskSet() as selected, temporary.open("w", encoding="utf-8") as stream:
        for candidate in paths:
            checkpoint()
            path = candidate.resolve().relative_to(repo.resolve()).as_posix()
            if path in selected:
                continue
            selected.add(path)
            size = (repo / path).stat().st_size
            if size > MAX_FILE_SIZE_BYTES:
                raise ValueError(f"{path}: exceeds GitHub's 100 MiB standard Git limit; no LFS enabled")
            total += size
            if total > MAX_BATCH_SIZE_BYTES:
                raise ValueError("Commit exceeds 200 MiB; split the transfer into size-based batches")
            stream.write(json.dumps({"path": path, "hash": file_digest(repo / path)}) + "\n")
            count += 1
        stream.flush()
        os.fsync(stream.fileno())
    if not count:
        temporary.unlink()
        return
    temporary.replace(manifest)
    head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=repo, capture_output=True)
    state = {"message": message, "branch": branch,
             "head": head.stdout.decode().strip() if head.returncode == 0 else "", "count": count}
    write_journal(journal, state)
    finish_pending_git(repo, progress, push, checkpoint=checkpoint)
