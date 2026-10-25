"""Recoverable, verified copies; source removal follows verified publication."""

import json
import os
import shutil
import sqlite3
import threading
import uuid
from contextlib import closing
from pathlib import Path

from mover_git.core import MAX_BATCH_SIZE_BYTES, MAX_FILE_SIZE_BYTES, human_size
from mover_git.git import (
    _git,
    file_digest,
    finish_pending_git,
    pending_git_file,
    push_unpublished,
    remote_head,
    stage_commit_push,
    write_journal,
)
from mover_git.storage import DiskSet, OperationLease, records, remove_journal


def digest(path: Path) -> str:
    if path.is_symlink():
        raise RuntimeError(f"Symlink is not a transferable regular file: {path}")
    return file_digest(path)


def transfer_directory(repo: Path) -> Path:
    return pending_git_file(repo).parent / "mover-transfers"


def prepare_transfer(repo: Path, pairs, message: str, branch: str,
                     journal: Path | None = None) -> Path:
    """Persist exact destinations and hashes before touching any source/destination."""
    if _git(repo, "symbolic-ref", "--short", "HEAD").decode().strip() != branch:
        raise RuntimeError(f"Switch to branch {branch} before transferring files")
    directory = transfer_directory(repo)
    directory.mkdir(exist_ok=True)
    token = journal.stem if journal else uuid.uuid4().hex
    journal = journal or directory / f"{token}.json"
    manifest = journal.with_suffix(".jsonl")
    temporary = manifest.with_suffix(".building")
    total = count = 0
    try:
        head = _git(repo, "rev-parse", "--verify", "HEAD").decode().strip()
    except RuntimeError:
        head = ""
    with DiskSet() as reserved, temporary.open("w", encoding="utf-8") as output:
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
            output.write(json.dumps({"source": str(source), "path": relative, "size": size,
                                     "hash": checksum}) + "\n")
            count += 1
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(manifest)
    write_journal(journal, {"count": count, "branch": branch,
                           "message": f"{message}\n\nGit-Mover: {token}", "committed": False})
    return journal


def finish_transfer(repo: Path, journal: Path, progress=lambda message: None,
                    push: bool = True, retain: bool = False) -> None:
    state = json.loads(journal.read_text(encoding="utf-8"))
    if _git(repo, "symbolic-ref", "--short", "HEAD").decode().strip() != state["branch"]:
        raise RuntimeError(f"Switch back to {state['branch']} before resuming")
    processed = count = 0
    for record in records(state, journal):
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
        count += 1
        processed += record["size"]
    progress(f"Batch copied and verified: {count} files, {human_size(processed)}")
    if not state["committed"]:
        stage_commit_push(repo, (repo / r["path"] for r in records(state, journal)), state["message"], state["branch"], progress, push=False)
        state["committed"] = True
        state["commit"] = _git(repo, "rev-parse", "HEAD").decode().strip()
        write_journal(journal, state)
    if not push:
        return
    _git(repo, "merge-base", "--is-ancestor", state["commit"], "HEAD")
    # A later commit may have removed/replaced a copied file. Do not delete its source.
    for record in records(state, journal):
        relative = record["path"]
        blob = _git(repo, "hash-object", "--", relative).strip()
        committed = _git(repo, "rev-parse", f"HEAD:{relative}").strip()
        if blob != committed:
            raise RuntimeError(f"Committed content differs from transfer: {relative}; source preserved")
    push_unpublished(repo, state["branch"], progress)
    for record in (() if state.get("complete") else records(state, journal)):
        source, destination = Path(record["source"]), repo / record["path"]
        if source.exists():
            if digest(source) != record["hash"] or digest(destination) != record["hash"]:
                raise RuntimeError(f"File changed; source preserved: {source}. Review before retrying.")
            source.unlink()
    if retain:
        state["complete"] = True
        write_journal(journal, state)
    else:
        remove_journal(journal)


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


def operation_file(repo: Path) -> Path:
    return pending_git_file(repo).parent / "mover-operation.sqlite"


def local_head(repo: Path) -> str:
    branch = _git(repo, "symbolic-ref", "HEAD").decode().strip()
    return _git(repo, "for-each-ref", "--format=%(objectname)", branch).decode().strip()


class BatchOperation:
    """Disk-backed queue. Only the worker changes the active range/checkpoint.

    The UI communicates through a lock and Event, never through the worker's DB.
    A retained transfer journal bridges completion and queue checkpoint writes.
    """

    def __init__(self, repo: Path):
        self.repo = repo.resolve()
        self.db = sqlite3.connect(operation_file(repo))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.lock = threading.Lock()
        self.pause = threading.Event()
        self.state = json.loads(self.db.execute("SELECT value FROM state").fetchone()[0])
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT)")
        self.db.execute("INSERT OR IGNORE INTO settings VALUES(1,?)",
                        (json.dumps(self.state["max_files"]),))
        self.db.commit()
        self.max_files = json.loads(self.db.execute("SELECT value FROM settings").fetchone()[0])

    @classmethod
    def create(cls, repo, pairs, branch, prefix="update", max_files=None):
        with OperationLease(operation_file(repo).with_suffix(".lock")):
            return cls._create(repo, pairs, branch, prefix, max_files)

    @classmethod
    def _create(cls, repo, pairs, branch, prefix, max_files):
        cls.validate_limit(max_files)
        path = operation_file(repo)
        if path.exists():
            raise RuntimeError("Resume the existing operation before starting another")
        temporary = path.with_suffix(".building")
        if temporary.exists():
            temporary.unlink()
        db = sqlite3.connect(temporary)
        try:
            db.execute("CREATE TABLE queue (id INTEGER PRIMARY KEY, source TEXT, "
                       "destination TEXT UNIQUE, size INTEGER)")
            db.execute("CREATE TABLE state (value TEXT)")
            for source, destination in pairs:
                db.execute("INSERT INTO queue(source,destination,size) VALUES(?,?,?)",
                           (str(source), str(destination), source.stat().st_size))
            state = dict(branch=branch, prefix=prefix, max_files=max_files,
                         head=local_head(repo), remote=_git(repo, "remote", "get-url", "origin")
                         .decode().strip(), remote_commit=remote_head(repo, branch),
                         completed=0, bytes=0, batch=0, active=None,
                         status="Paused")
            db.execute("INSERT INTO state VALUES(?)", (json.dumps(state),))
            db.commit()
        finally:
            db.close()
        temporary.replace(path)
        return cls(repo)

    @staticmethod
    def validate_limit(value):
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError("Maximum files must be a positive integer or blank for unlimited")

    def set_limit(self, value):
        self.validate_limit(value)
        with self.lock:
            # Separate connection: this method may be called by Tk while run() owns its DB.
            with closing(sqlite3.connect(operation_file(self.repo))) as db, db:
                db.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(value),))
            self.max_files = value

    def save(self, status=None):
        if status:
            self.state["status"] = status
        with self.lock:
            self.state["max_files"] = self.max_files
        self.db.execute("UPDATE state SET value=?", (json.dumps(self.state),))
        self.db.commit()

    def verify(self):
        branch = _git(self.repo, "symbolic-ref", "--short", "HEAD").decode().strip()
        if branch != self.state["branch"]:
            raise RuntimeError(f"Switch back to {self.state['branch']} before resuming")
        if _git(self.repo, "remote", "get-url", "origin").decode().strip() != self.state["remote"]:
            raise RuntimeError("Origin changed; restore the saved remote before resuming")
        head = local_head(self.repo)
        active = self.state["active"]
        if head != self.state["head"]:
            journal = transfer_directory(self.repo) / active["journal"] if active else None
            if not journal or not journal.exists():
                raise RuntimeError("HEAD changed outside the saved operation")
            transfer = json.loads(journal.read_text(encoding="utf-8"))
            message = _git(self.repo, "log", "-1", "--format=%B").decode().strip()
            parents = _git(self.repo, "log", "-1", "--format=%P").decode().strip()
            if message != transfer["message"].strip() or parents != self.state["head"]:
                raise RuntimeError("HEAD changed outside the active batch")
        remote = remote_head(self.repo, branch)
        if self.state.get("remote_commit") and not remote:
            raise RuntimeError("Remote branch was deleted; review before resuming")
        if remote and remote != head:
            try:
                _git(self.repo, "merge-base", "--is-ancestor", remote, head)
            except RuntimeError as exc:
                raise RuntimeError("Remote diverged; fetch and review before resuming") from exc

    def select_batch(self):
        with self.lock:
            maximum = self.max_files
        count = size = last = 0
        for ident, length in self.db.execute(
            "SELECT id,size FROM queue WHERE id>? ORDER BY id", (self.state["completed"],)
        ):
            if count and (size + length > MAX_BATCH_SIZE_BYTES or size == MAX_BATCH_SIZE_BYTES
                          or (maximum is not None and count >= maximum)):
                break
            if length > MAX_FILE_SIZE_BYTES:
                raise ValueError("Queued file exceeds the 100 MiB per-file limit")
            count, size, last = count + 1, size + length, ident
        if not count:
            return None
        return dict(last=last, count=count, size=size, journal=f"{uuid.uuid4().hex}.json")

    def run(self, progress=lambda message: None, status=lambda value: None):
        with OperationLease(operation_file(self.repo).with_suffix(".lock")):
            self.state = json.loads(self.db.execute("SELECT value FROM state").fetchone()[0])
            return self._run(progress, status)

    def _run(self, progress, status):
        def report(value):
            if value == "Running" and self.pause.is_set():
                value = "Finishing Batch"
            self.save(value)
            status(value)

        def batch_progress(message):
            if self.pause.is_set() and self.state["status"] != "Finishing Batch":
                report("Finishing Batch")
            progress(message)

        try:
            report("Resuming")
            self.verify()
            if self.state.get("garbage"):
                old = transfer_directory(self.repo) / self.state["garbage"]
                if old.exists():
                    remove_journal(old)
                self.state["garbage"] = None
                self.save()
            while True:
                active = self.state["active"]
                if not active:
                    if not self.db.execute("SELECT 1 FROM queue WHERE id>? LIMIT 1",
                                           (self.state["completed"],)).fetchone():
                        report("Complete")
                        return
                    if self.pause.is_set():
                        report("Paused")
                        return
                    active = self.select_batch()
                    if not active:
                        report("Complete")
                        return
                    self.state["active"] = active
                    report("Running")
                journal = transfer_directory(self.repo) / active["journal"]
                if not journal.exists():
                    rows = self.db.execute(
                        "SELECT source,destination FROM queue WHERE id>? AND id<=? ORDER BY id",
                        (self.state["completed"], active["last"]))
                    prepare_transfer(self.repo, ((Path(s), Path(d)) for s, d in rows),
                                     f"{self.state['prefix']} - batch {self.state['batch'] + 1}",
                                     self.state["branch"], journal=journal)
                if self.pause.is_set():
                    report("Finishing Batch")
                finish_transfer(self.repo, journal, batch_progress, retain=True)
                self.state.update(completed=active["last"],
                                  bytes=self.state["bytes"] + active["size"],
                                  batch=self.state["batch"] + 1, active=None,
                                  garbage=active["journal"],
                                  head=local_head(self.repo),
                                  remote_commit=remote_head(self.repo, self.state["branch"]))
                self.save()
                remove_journal(journal)
                if not self.db.execute("SELECT 1 FROM queue WHERE id>? LIMIT 1",
                                       (self.state["completed"],)).fetchone():
                    report("Complete")
                    return
        except Exception:
            report("Error")
            raise

    def close(self):
        self.db.close()
