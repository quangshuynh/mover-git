"""Recoverable, verified copies; source removal follows verified publication."""

import json
import os
import shutil
import uuid
from pathlib import Path

from mover_git.core import MAX_BATCH_SIZE_BYTES, MAX_FILE_SIZE_BYTES, human_size
from mover_git.git import (
    _git,
    file_digest,
    finish_pending_git,
    pending_git_file,
    push_unpublished,
    stage_commit_push,
    write_journal,
)


def digest(path: Path) -> str:
    if path.is_symlink():
        raise RuntimeError(f"Symlink is not a transferable regular file: {path}")
    return file_digest(path)


def transfer_directory(repo: Path) -> Path:
    return pending_git_file(repo).parent / "mover-transfers"


def prepare_transfer(repo: Path, pairs, message: str, branch: str) -> Path:
    """Persist exact destinations and hashes before touching any source/destination."""
    if _git(repo, "symbolic-ref", "--short", "HEAD").decode().strip() != branch:
        raise RuntimeError(f"Switch to branch {branch} before transferring files")
    records = []
    total = 0
    reserved = set()
    try:
        head = _git(repo, "rev-parse", "--verify", "HEAD").decode().strip()
    except RuntimeError:
        head = ""
    for source, destination in pairs:
        source, destination = Path(source).absolute(), Path(destination).absolute()
        relative = destination.resolve().relative_to(repo.resolve()).as_posix()
        if relative.casefold() == ".git" or relative.casefold().startswith(".git/"):
            raise ValueError("Transfers cannot write Git metadata")
        key = os.path.normcase(str(destination))
        if destination.exists() or destination.is_symlink() or key in reserved:
            raise FileExistsError(f"Destination already exists or is reserved: {destination}")
        reserved.add(key)
        size = source.stat().st_size
        if size > MAX_FILE_SIZE_BYTES:
            raise ValueError(f"{source}: exceeds GitHub's 100 MiB standard Git limit; no LFS enabled")
        total += size
        if total > MAX_BATCH_SIZE_BYTES:
            raise ValueError("Transfer batch exceeds 200 MiB")
        # A staged deletion also owns a pathname even though it is absent on disk.
        staged = _git(repo, "--literal-pathspecs", "ls-files", "--stage", "--", relative)
        tracked = head and _git(repo, "--literal-pathspecs", "ls-tree", "-z", head, "--", relative)
        if staged or tracked:
            raise RuntimeError(f"Destination is already tracked/staged: {relative}")
        before = source.stat()
        checksum = digest(source)
        after = source.stat()
        if after.st_size != size or (before.st_size, before.st_mtime_ns) != (
            after.st_size, after.st_mtime_ns
        ):
            raise RuntimeError(f"Source changed during planning: {source}; scan again")
        records.append({"source": str(source), "path": relative, "size": size,
                        "hash": checksum})
    directory = transfer_directory(repo)
    directory.mkdir(exist_ok=True)
    token = uuid.uuid4().hex
    journal = directory / f"{token}.json"
    write_journal(journal, {"files": records, "branch": branch,
                           "message": f"{message}\n\nGit-Mover: {token}", "committed": False})
    return journal


def finish_transfer(repo: Path, journal: Path, progress=lambda message: None,
                    push: bool = True) -> None:
    state = json.loads(journal.read_text(encoding="utf-8"))
    if _git(repo, "symbolic-ref", "--short", "HEAD").decode().strip() != state["branch"]:
        raise RuntimeError(f"Switch back to {state['branch']} before resuming")
    paths = []
    processed = 0
    for record in state["files"]:
        source, destination = Path(record["source"]), repo / record["path"]
        if not destination.resolve().is_relative_to(repo.resolve()):
            raise RuntimeError(f"Destination escaped repository: {destination}")
        expected = record["hash"]
        if destination.exists():
            if digest(destination) != expected:
                raise RuntimeError(f"Destination differs from verified transfer: {destination}. "
                                   "Preserve both files and repair manually before retrying.")
        else:
            if not source.exists() or digest(source) != expected:
                raise RuntimeError(f"Source missing or changed: {source}; restore before retrying")
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{journal.stem}.partial")
            # Only the journal-owned temporary copy can be replaced on recovery.
            if temporary.is_symlink():
                raise RuntimeError(f"Unexpected temporary symlink: {temporary}")
            with source.open("rb") as incoming, temporary.open("wb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            shutil.copystat(source, temporary)
            if digest(temporary) != expected:
                raise RuntimeError(f"Copy verification failed: {source}; source preserved")
            # Windows rename and POSIX hard-link publication both refuse replacement.
            if os.name == "nt":
                os.rename(temporary, destination)
            else:
                os.link(temporary, destination)
                temporary.unlink()
        paths.append(destination)
        processed += record["size"]
    progress(f"Batch copied and verified: {len(paths)} files, {human_size(processed)}")
    if not state["committed"]:
        stage_commit_push(repo, paths, state["message"], state["branch"], progress, push=False)
        state["committed"] = True
        state["commit"] = _git(repo, "rev-parse", "HEAD").decode().strip()
        write_journal(journal, state)
    if not push:
        return
    _git(repo, "merge-base", "--is-ancestor", state["commit"], "HEAD")
    # A later commit may have removed/replaced a copied file. Do not delete its source.
    for path in paths:
        relative = path.relative_to(repo).as_posix()
        blob = _git(repo, "hash-object", "--", relative).strip()
        committed = _git(repo, "rev-parse", f"HEAD:{relative}").strip()
        if blob != committed:
            raise RuntimeError(f"Committed content differs from transfer: {relative}; source preserved")
    push_unpublished(repo, state["branch"], progress)
    for record in state["files"]:
        source, destination = Path(record["source"]), repo / record["path"]
        if source.exists():
            if digest(source) != record["hash"] or digest(destination) != record["hash"]:
                raise RuntimeError(f"File changed; source preserved: {source}. Review before retrying.")
            source.unlink()
    journal.unlink()


def resume_transfers(repo: Path, branch: str, progress=lambda message: None) -> bool:
    resumed = finish_pending_git(repo, progress)
    directory = transfer_directory(repo)
    if directory.exists():
        for journal in directory.glob("*.json"):
            finish_transfer(repo, journal, progress)
            resumed = True
    # Also handles unpublished commits without an application journal.
    push_unpublished(repo, branch, progress)
    return resumed
