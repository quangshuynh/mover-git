"""Explicit abandonment of canceled queues; only application metadata is removed."""

import json
import os
import shutil
import uuid
from pathlib import Path

from mover_git.git import _git, file_digest, pending_git_file, remote_head, write_journal
from mover_git.storage import OperationLease


def sync_directory(path):
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def metadata_path(root, relative):
    path = root / relative
    parts = Path(relative).parts
    allowed = (len(parts) == 1 and (relative == "mover-operation.sqlite" or
               relative in {f"mover-pending.{suffix}" for suffix in
                            ("json", "jsonl", "building", "tmp")})) or (
        len(parts) == 2 and parts[0] == "mover-transfers" and
        Path(parts[1]).suffix in (".json", ".jsonl", ".owned", ".building", ".tmp"))
    if not allowed or path.is_symlink() or path.resolve() != path.absolute():
        raise RuntimeError("Unsafe discard metadata path; records preserved")
    return path


def finish_discard(root):
    """Idempotently finish an already confirmed discard, under both operation leases."""
    marker = root / "mover-discard.json"
    state = json.loads(marker.read_text())
    archive = root / "mover-canceled" / state["archive"]
    if (Path(state["archive"]).name != state["archive"] or archive.is_symlink()
            or archive.resolve() != archive.absolute()):
        raise RuntimeError("Unsafe archive path; records preserved")
    # Validate the complete archive and remaining originals before removing anything.
    for record in state["files"]:
        saved = metadata_path(archive, record["path"])
        original = metadata_path(root, record["path"])
        if file_digest(saved) != record["hash"] or (
                original.exists() and file_digest(original) != record["hash"]):
            raise RuntimeError("Discard records changed; preserve archive and inspect manually")
    for record in state["files"]:
        path = metadata_path(root, record["path"])
        path.unlink(missing_ok=True)
        sync_directory(path.parent)
    marker.unlink()
    sync_directory(root)
    return archive


def recover_discard(repo):
    root = pending_git_file(repo).parent
    if not (root / "mover-discard.json").exists():
        return None
    with OperationLease(root / "mover-operation.lock"), OperationLease(root / "mover-push.lock"):
        return finish_discard(root)


def inspect_discard(operation):
    repo, state = operation.repo, operation.state
    if not operation.cancel_mode() or state["status"] not in ("Canceled", "Cleanup Available"):
        raise RuntimeError("Only a finalized canceled job can be discarded")
    root = pending_git_file(repo).parent
    for name in ("index.lock", "HEAD.lock", "packed-refs.lock", "MERGE_HEAD", "CHERRY_PICK_HEAD",
                 "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"):
        if (root / name).exists():
            raise RuntimeError("Git work is active or incomplete; finish it before discarding")
    branch = _git(repo, "symbolic-ref", "--short", "HEAD").decode().strip()
    origin = _git(repo, "remote", "get-url", "origin").decode().strip()
    if branch != state["branch"] or origin != state["remote"]:
        raise RuntimeError("Branch or origin changed; restore them before discarding")
    head = _git(repo, "for-each-ref", "--format=%(objectname)", f"refs/heads/{branch}").decode().strip()
    if state["head"]:
        try:
            _git(repo, "merge-base", "--is-ancestor", state["head"], head)
        except RuntimeError as exc:
            raise RuntimeError("Saved HEAD is no longer an ancestor; reconcile history before discard") from exc
    remote = remote_head(repo, branch)
    if remote:
        try:
            _git(repo, "merge-base", "--is-ancestor", remote, head)
        except RuntimeError as exc:
            raise RuntimeError("Remote diverged, advanced, or is unknown locally; reconcile it first") from exc
    elif state.get("remote_commit"):
        raise RuntimeError("Remote branch disappeared; reconcile it before discarding")
    remote_status = ("matches HEAD" if remote == head else
                     "behind local HEAD; unpublished commits remain" if remote else
                     "branch absent; local commits remain unpublished")
    revision = f"{state['head']}..{head}" if state["head"] else head
    changes = _git(repo, "log", "--format=%h %s", revision).decode() if head else ""
    working = _git(repo, "status", "--porcelain=v1", "--untracked-files=normal").decode()
    summary = (operation.cancellation_summary() +
               f"\nSaved HEAD: {state['head'] or '(unborn)'}\nCurrent HEAD: {head or '(unborn)'}"
               f"\nOrigin/{branch}: {remote or '(absent)'} — {remote_status}\n"
               f"Changed commits since saved HEAD:\n{changes or '(none)'}\n"
               f"Working tree/index:\n{working or '(clean)'}\n"
               "Discard abandons the old transfer; it does not recover it. Files and Git history "
               "stay unchanged. Future transfers may publish remaining local commits. "
               "Recovery records will remain in .git/mover-canceled for manual inspection.")
    return summary


def discard_job(repo, confirm):
    """Hold leases through review and confirmation; recheck before archiving/clearing."""
    from mover_git.transfer import BatchOperation

    recover_discard(repo)
    root = pending_git_file(repo).parent
    with OperationLease(root / "mover-operation.lock"), OperationLease(root / "mover-push.lock"):
        operation = BatchOperation(repo)
        try:
            review = inspect_discard(operation)
            active = operation.state["active"]
            names = {name for name in (active["journal"] if active else None,
                                      operation.state.get("garbage")) if name}
            journals = root / "mover-transfers"
            if any(path.name not in names for path in journals.glob("*.json")):
                raise RuntimeError("Unrelated transfer journals require review before discard")
            for name in names:
                if Path(name).name != name:
                    raise RuntimeError("Unsafe journal path")
                journal = journals / name
                if journal.exists():
                    transfer = json.loads(journal.read_text())
                    if (transfer["branch"] != operation.state["branch"] or
                            ("files" not in transfer and not journal.with_suffix(".jsonl").is_file())):
                        raise RuntimeError("Transfer manifest is missing or branch differs; inspect records")
            pending = pending_git_file(repo)
            pending_artifacts = list(root.glob("mover-pending.*"))
            if pending_artifacts and not pending.exists():
                raise RuntimeError("Pending Git artifacts have no ownership journal; inspect them manually")
            if pending.exists():
                saved = json.loads(pending.read_text())
                if (saved["branch"] != operation.state["branch"] or
                        not active or not (journals / active["journal"]).exists() or
                        saved["message"] != json.loads((journals / active["journal"]).read_text())["message"]):
                    raise RuntimeError("Unrelated pending Git work requires review before discard")
            artifacts = [root / "mover-operation.sqlite"]
            for name in names:
                artifacts.extend(journals.glob(f"{Path(name).stem}.*"))
            artifacts.extend(pending_artifacts)
            # Sidecars never include repository files or Git's own metadata.
            artifacts = list(dict.fromkeys(artifacts))
            for path in artifacts:
                metadata_path(root, path.relative_to(root).as_posix())
            if not confirm(review + "\n\nAbandon this canceled job and discard its active state?"):
                return None
            if inspect_discard(operation) != review:
                raise RuntimeError("Git state changed during confirmation; review and confirm again")
            if operation.db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                raise RuntimeError("Operation database is busy; try again when idle")
            operation.close()
            if any((root / f"mover-operation.sqlite{suffix}").exists() for suffix in ("-wal", "-shm")):
                raise RuntimeError("Operation database still has another connection; close it before discard")
            archive = root / "mover-canceled" / uuid.uuid4().hex
            archive.mkdir(parents=True)
            entries = []
            for original in artifacts:
                relative = original.relative_to(root).as_posix()
                saved = archive / relative
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, saved)
                with saved.open("rb") as stream:
                    os.fsync(stream.fileno())
                checksum = file_digest(original)
                if file_digest(saved) != checksum:
                    raise RuntimeError("Archive verification failed; active records preserved")
                entries.append({"path": relative, "hash": checksum})
                sync_directory(saved.parent)
            entries.sort(key=lambda entry: entry["path"] == "mover-operation.sqlite")
            state = {"archive": archive.name, "files": entries, "review": review}
            write_journal(archive / "discard.json", state)
            sync_directory(archive)
            sync_directory(archive.parent)
            # Durable intent precedes any active metadata deletion; startup can finish it.
            write_journal(root / "mover-discard.json", state)
            sync_directory(root)
            return finish_discard(root)
        finally:
            operation.close()
