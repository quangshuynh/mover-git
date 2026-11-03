"""Recoverable, verified copies; source removal follows verified publication."""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import uuid
from contextlib import closing
from pathlib import Path

from mover_git.core import MAX_BATCH_SIZE_BYTES, MAX_FILE_SIZE_BYTES, human_size
from mover_git.git import (
    GIT_CONFIG,
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


def stable_identity(stat):
    """File identity and content metadata; excludes atime, which hashing may update."""
    return (stat.st_dev, stat.st_ino, stat.st_mode, stat.st_nlink, stat.st_size,
            stat.st_mtime_ns, stat.st_ctime_ns)


def partial_path(repo: Path, journal: Path, record) -> Path:
    """Journal-owned temporary copy; one name per destination directory and batch."""
    return (repo / record["path"]).with_name(f".{journal.stem}.partial")


# Attributes whose presence can make stored blobs differ from the verified file bytes.
CONTENT_ATTRIBUTES = ("text", "eol", "crlf", "filter", "ident", "working-tree-encoding")


def check_content_filters(repo: Path, state, journal: Path, checkpoint=lambda: None) -> None:
    """Refuse to commit when Git conversion would store bytes other than the manifest's."""
    with DiskSet() as flagged, tempfile.TemporaryFile() as names, \
            tempfile.TemporaryFile() as output:
        for record in records(state, journal):
            names.write(record["path"].encode("utf-8") + b"\0")
        names.seek(0)
        result = subprocess.run(["git", *GIT_CONFIG, "check-attr", "-z", "--stdin",
                                 *CONTENT_ATTRIBUTES], cwd=repo, stdin=names, stdout=output,
                                stderr=subprocess.PIPE)
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace").strip())
        output.seek(0)
        tokens, pending = [], b""
        while chunk := output.read(65536):
            parts = (pending + chunk).split(b"\0")
            pending = parts.pop()
            tokens.extend(parts)
            while len(tokens) >= 3:
                path, attribute, value = tokens[:3]
                del tokens[:3]
                if value != b"unspecified" and not (
                        attribute in (b"text", b"crlf") and value == b"unset"):
                    flagged.add(path.decode("utf-8"))
        for record in records(state, journal):
            if record["path"] not in flagged:
                continue
            checkpoint()
            relative = record["path"]
            if (_git(repo, "hash-object", "--", relative)
                    != _git(repo, "hash-object", "--no-filters", "--", relative)):
                raise RuntimeError(
                    f"Git attributes or filters would alter {relative} when committed "
                    "(.gitattributes text/eol/filter/ident/encoding), so the commit would not "
                    "match the verified source. Nothing was committed and sources are preserved; "
                    "mark these paths -text/-filter in .gitattributes or choose another "
                    "destination, then resume or cancel and clean up.")


def verify_committed(repo: Path, commit: str, state, journal: Path,
                     checkpoint=lambda: None) -> None:
    """Hash every committed blob (streamed) against the source manifest before publication."""
    process = subprocess.Popen(["git", *GIT_CONFIG, "cat-file", "--batch"], cwd=repo,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    try:
        for record in records(state, journal):
            checkpoint()
            relative = record["path"]
            name = f"{commit}:{relative}"
            if "\n" in relative:
                name = _git(repo, "rev-parse", "--verify", "--end-of-options",
                            name).decode().strip()
            process.stdin.write(name.encode("utf-8") + b"\n")
            process.stdin.flush()
            header = process.stdout.readline().split()
            if len(header) != 3 or header[1] != b"blob":
                raise RuntimeError(f"Committed file is missing from {commit[:8]}: {relative}; "
                                   "nothing was pushed and the source is preserved.")
            remaining, checksum = int(header[2]), hashlib.sha256()
            while remaining:
                chunk = process.stdout.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise RuntimeError("Git object stream ended unexpectedly; source preserved")
                checksum.update(chunk)
                remaining -= len(chunk)
            process.stdout.read(1)
            if checksum.hexdigest() != record["hash"]:
                raise RuntimeError(
                    f"Committed content in {commit[:8]} does not match the verified source "
                    f"manifest: {relative}. Git attributes or filters may have altered it. "
                    "Nothing was pushed and the source is preserved; review the local commit.")
    finally:
        process.stdin.close()
        process.stdout.close()
        process.wait()
    head = local_head(repo)
    if head == commit:
        return
    # Later local commits must not have changed or removed transferred files.
    with DiskSet() as changed, tempfile.TemporaryFile() as names:
        result = subprocess.run(["git", *GIT_CONFIG, "diff-tree", "-r", "-z", "--no-renames",
                                 "--name-only", commit, head], cwd=repo, stdout=names,
                                stderr=subprocess.PIPE)
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace").strip())
        names.seek(0)
        pending = b""
        while chunk := names.read(65536):
            parts = (pending + chunk).split(b"\0")
            pending = parts.pop()
            for part in parts:
                changed.add(part.decode("utf-8"))
        for record in records(state, journal):
            if record["path"] in changed:
                raise RuntimeError(f"Committed content differs from transfer: {record['path']}; "
                                   "source preserved")


def prepare_transfer(repo: Path, pairs, message: str, branch: str,
                     journal: Path | None = None, checkpoint=lambda: None) -> Path:
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
            checkpoint()
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
                           "message": f"{message}\n\nGit-Mover: {token}", "committed": False,
                           "base_head": head})
    return journal


def finish_transfer(repo: Path, journal: Path, progress=lambda message: None,
                    push: bool = True, retain: bool = False, checkpoint=lambda: None) -> None:
    state = json.loads(journal.read_text(encoding="utf-8"))
    if _git(repo, "symbolic-ref", "--short", "HEAD").decode().strip() != state["branch"]:
        raise RuntimeError(f"Switch back to {state['branch']} before resuming")
    processed = count = 0
    for record in records(state, journal):
        checkpoint()
        source, destination = Path(record["source"]), repo / record["path"]
        if not destination.resolve().is_relative_to(repo.resolve()):
            raise RuntimeError(f"Destination escaped repository: {destination}")
        expected = record["hash"]
        temporary = partial_path(repo, journal, record)
        if destination.exists():
            # Crash after hard-link publication: the temporary name is only a second link.
            if (temporary.exists() and not temporary.is_symlink()
                    and os.path.samefile(temporary, destination)):
                temporary.unlink()
            if digest(destination) != expected:
                raise RuntimeError(f"Destination differs from verified transfer: {destination}. "
                                   "Preserve both files and repair manually before retrying.")
        else:
            if not source.exists() or digest(source) != expected:
                raise RuntimeError(f"Source missing or changed: {source}; restore before retrying")
            checkpoint()
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Only the journal-owned temporary copy can be replaced on recovery.
            if temporary.is_symlink():
                raise RuntimeError(f"Unexpected temporary symlink: {temporary}")
            # Unlink, never truncate: a stale temporary may share a published file's inode.
            temporary.unlink(missing_ok=True)
            with source.open("rb") as incoming, temporary.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            shutil.copystat(source, temporary)
            if digest(temporary) != expected:
                raise RuntimeError(f"Copy verification failed: {source}; source preserved")
            # Persist identity before publication, including a crash immediately after link/rename.
            with closing(sqlite3.connect(journal.with_suffix(".owned"))) as owned, owned:
                owned.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, dev, ino)")
                identity = temporary.stat()
                owned.execute("INSERT OR REPLACE INTO files VALUES(?,?,?)",
                              (record["path"], identity.st_dev, identity.st_ino))
            # Windows rename and POSIX hard-link publication both refuse replacement.
            if os.name == "nt":
                os.rename(temporary, destination)
            else:
                os.link(temporary, destination)
                temporary.unlink()
            # Post-publication ctime also detects an inode reused or changed since copying.
            with closing(sqlite3.connect(journal.with_suffix(".owned"))) as owned, owned:
                owned.execute("CREATE TABLE IF NOT EXISTS published (path TEXT PRIMARY KEY, ctime)")
                owned.execute("INSERT OR REPLACE INTO published VALUES(?,?)",
                              (record["path"], destination.stat().st_ctime_ns))
        count += 1
        processed += record["size"]
    progress(f"Batch copied and verified: {count} files, {human_size(processed)}")
    checkpoint()
    if not state["committed"]:
        # Reconcile a crash after commit and after the Git journal was removed.
        head = local_head(repo)
        if (head and _git(repo, "log", "-1", "--format=%B").decode().strip() == state["message"].strip()
                and ("base_head" not in state or
                     _git(repo, "log", "-1", "--format=%P").decode().strip() == state["base_head"])):
            if pending_git_file(repo).exists():
                pending = json.loads(pending_git_file(repo).read_text())
                if pending["message"] != state["message"]:
                    raise RuntimeError("Unrelated pending Git work requires review before recovery")
                finish_pending_git(repo, progress, push=False, checkpoint=checkpoint)
            state.update(committed=True, commit=head)
            write_journal(journal, state)
    if not state["committed"]:
        check_content_filters(repo, state, journal, checkpoint)
        stage_commit_push(repo, (repo / r["path"] for r in records(state, journal)), state["message"], state["branch"], progress, push=False, checkpoint=checkpoint)
        state["committed"] = True
        state["commit"] = _git(repo, "rev-parse", "HEAD").decode().strip()
        write_journal(journal, state)
    checkpoint()
    if not push:
        return
    _git(repo, "merge-base", "--is-ancestor", state["commit"], "HEAD")
    # Publish only commits whose stored blobs hash to the verified source manifest.
    verify_committed(repo, state["commit"], state, journal, checkpoint)
    checkpoint()
    push_unpublished(repo, state["branch"], progress, check_cancel=checkpoint)
    checkpoint()
    for record in (() if state.get("complete") else records(state, journal)):
        checkpoint()
        source, destination = Path(record["source"]), repo / record["path"]
        if source.exists():
            if digest(source) != record["hash"] or digest(destination) != record["hash"]:
                raise RuntimeError(f"File changed; source preserved: {source}. Review before retrying.")
            checkpoint()
            source.unlink()
    if retain:
        state["complete"] = True
        write_journal(journal, state)
    else:
        remove_journal(journal)


def resume_transfers(repo: Path, branch: str, progress=lambda message: None) -> bool:
    from mover_git.discard import recover_discard

    recover_discard(repo)
    if operation_file(repo).exists():
        operation = BatchOperation(repo)
        try:
            if operation.cancel_mode():
                raise RuntimeError("Canceled transfer preserved; use its explicit Resume or Cleanup controls")
        finally:
            operation.close()
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


class CancellationRequested(Exception):
    pass


class BatchOperation:
    """Disk-backed queue. Only the worker changes the active range/checkpoint.

    The UI communicates through a lock and Event, never through the worker's DB.
    A retained transfer journal bridges completion and queue checkpoint writes.
    """

    def __init__(self, repo: Path):
        self.repo = repo.resolve()
        if (pending_git_file(repo).parent / "mover-discard.json").exists():
            raise RuntimeError("Confirmed discard needs startup recovery before opening this job")
        self.db = sqlite3.connect(operation_file(repo))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.lock = threading.Lock()
        self.pause = threading.Event()
        self.state = json.loads(self.db.execute("SELECT value FROM state").fetchone()[0])
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, value TEXT)")
        self.db.execute("INSERT OR IGNORE INTO settings VALUES(1,?)",
                        (json.dumps(self.state["max_files"]),))
        self.db.execute("CREATE TABLE IF NOT EXISTS cancellation (id INTEGER PRIMARY KEY, mode TEXT)")
        self.db.execute("INSERT OR IGNORE INTO cancellation VALUES(1,NULL)")
        self.db.commit()
        self.max_files = json.loads(self.db.execute("SELECT value FROM settings").fetchone()[0])

    @classmethod
    def create(cls, repo, pairs, branch, prefix="update", max_files=None):
        from mover_git.discard import recover_discard

        recover_discard(repo)
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

    def cancel_mode(self):
        with closing(sqlite3.connect(operation_file(self.repo))) as db:
            return db.execute("SELECT mode FROM cancellation WHERE id=1").fetchone()[0]

    def request_cancel(self, mode):
        if mode not in ("batch", "transfer"):
            raise ValueError("Unknown cancellation mode")
        # Independent durable row: worker state saves cannot overwrite a UI request.
        with closing(sqlite3.connect(operation_file(self.repo))) as db, db:
            db.execute("UPDATE cancellation SET mode=CASE WHEN mode='transfer' THEN mode ELSE ? END",
                       (mode,))

    def resume(self):
        """Explicit user action; retrying a canceled batch still stops before the next batch."""
        with OperationLease(operation_file(self.repo).with_suffix(".lock")):
            self.state["retry_batch_only"] = self.cancel_mode() == "batch"
            self.save("Paused")
            with closing(sqlite3.connect(operation_file(self.repo))) as db, db:
                db.execute("UPDATE cancellation SET mode=NULL")

    def cancellation_summary(self):
        # Safe from the UI thread, with paths from the saved queue, never current widgets.
        with closing(sqlite3.connect(operation_file(self.repo))) as db:
            state = json.loads(db.execute("SELECT value FROM state").fetchone()[0])
            total = db.execute("SELECT count(*) FROM queue").fetchone()[0]
            first = db.execute("SELECT source,destination FROM queue ORDER BY id LIMIT 1").fetchone()
        return (f"Source (first queued file): {first[0] if first else '(empty)'}\n"
                f"Destination: {self.repo}\n"
                f"First destination file: {first[1] if first else '(empty)'}\n"
                f"Completed/pushed batches: {state['batch']}\n"
                f"Pending files: {total - state['completed']} (including active batch).\n"
                "An in-flight push may finish. Published and committed files will be preserved.")

    def cleanup(self, progress=lambda message: None):
        """Conservative, repeatable cleanup; never unstage, commit, push or delete sources."""
        with OperationLease(operation_file(self.repo).with_suffix(".lock")):
            self.state = json.loads(self.db.execute("SELECT value FROM state").fetchone()[0])
            if not self.cancel_mode() or self.state["status"] not in ("Canceled", "Cleanup Available"):
                raise RuntimeError("Finalize cancellation before cleanup")
            self.verify()
            active = self.state["active"]
            if not active:
                return
            journal = transfer_directory(self.repo) / active["journal"]
            if not journal.exists():
                return
            state = json.loads(journal.read_text())
            # Pending staging/commit and commit crash windows require preservation.
            if state["committed"] or pending_git_file(self.repo).exists() or local_head(self.repo) != self.state["head"]:
                progress("Preserved: committed or pending Git work requires recovery; no rollback performed.")
                return
            preserved = self.remove_partials(journal, state, progress)
            ownership = journal.with_suffix(".owned")
            if not ownership.exists():
                progress("Preserved: no durable file ownership evidence.")
                self.save("Cleanup Available" if preserved else "Canceled")
                return
            with closing(sqlite3.connect(ownership)) as owned:
                if not owned.execute("SELECT 1 FROM sqlite_master WHERE name='published'").fetchone():
                    progress("Preserved: publication ownership was interrupted; resume to recover.")
                    return
                for record in records(state, journal):
                    destination = self.repo / record["path"]
                    source = Path(record["source"])
                    try:
                        if not destination.exists() and not destination.is_symlink():
                            continue
                        identity = owned.execute("SELECT dev,ino FROM files WHERE path=?",
                                                 (record["path"],)).fetchone()
                        published = owned.execute("SELECT ctime FROM published WHERE path=?",
                                                  (record["path"],)).fetchone()
                        stat = destination.lstat()
                        source_stat = source.stat()
                        if (not destination.resolve().is_relative_to(self.repo)
                                or identity != (stat.st_dev, stat.st_ino)
                                or published != (stat.st_ctime_ns,)
                                or source.resolve() == destination.resolve()
                                or digest(source) != record["hash"]
                                or digest(destination) != record["hash"]
                                or _git(self.repo, "--literal-pathspecs", "ls-files", "--", record["path"])
                                or (local_head(self.repo) and _git(self.repo, "--literal-pathspecs",
                                    "ls-tree", "HEAD", "--", record["path"]))):
                            raise RuntimeError("ownership, source integrity, or uncommitted status not proven")
                        if (stable_identity(destination.lstat()) != stable_identity(stat) or
                                stable_identity(source.stat()) != stable_identity(source_stat)):
                            raise RuntimeError("file changed during cleanup verification")
                        destination.unlink()
                        progress(f"Removed verified uncommitted copy: {destination}")
                    except (OSError, RuntimeError) as exc:
                        preserved = True
                        progress(f"Preserved {destination}: {exc}")
            progress("Unverified partial files and journals remain available for recovery.")
            self.save("Cleanup Available" if preserved else "Canceled")

    def remove_partials(self, journal, state, progress):
        """Delete journal-owned temporary copies only when no data can depend on them.

        A temporary is removable when it is merely a second link to a published copy, or
        when every unpublished record in its directory still has an intact source.
        """
        preserved = False
        with DiskSet() as found, DiskSet() as unsafe, DiskSet() as handled:
            for record in records(state, journal):
                temporary = partial_path(self.repo, journal, record)
                if not (temporary.exists() or temporary.is_symlink()):
                    continue
                found.add(temporary)
                destination = self.repo / record["path"]
                if destination.exists() or destination.is_symlink():
                    continue
                source = Path(record["source"])
                try:
                    intact = (source.is_file() and not source.is_symlink()
                              and digest(source) == record["hash"])
                except OSError:
                    intact = False
                if not intact:
                    unsafe.add(temporary)
            for record in records(state, journal):
                temporary = partial_path(self.repo, journal, record)
                if temporary not in found or temporary in handled:
                    continue
                handled.add(temporary)
                if (temporary in unsafe or temporary.is_symlink() or not temporary.is_file()
                        or not temporary.resolve().is_relative_to(self.repo)
                        or _git(self.repo, "--literal-pathspecs", "ls-files", "--",
                                temporary.relative_to(self.repo).as_posix())):
                    preserved = True
                    progress(f"Preserved temporary copy {temporary}: an unpublished source is "
                             "missing or changed, or the file is not a plain untracked file.")
                    continue
                temporary.unlink()
                progress(f"Removed interrupted temporary copy: {temporary}")
        return preserved

    def partials_remain(self, journal, state):
        return any(partial_path(self.repo, journal, record).exists()
                   or partial_path(self.repo, journal, record).is_symlink()
                   for record in records(state, journal))

    def retire(self):
        """Archive a canceled, untouched/cleaned queue before planning corrected paths."""
        with OperationLease(operation_file(self.repo).with_suffix(".lock")):
            if not self.cancel_mode() or self.state["status"] not in ("Canceled", "Cleanup Available"):
                raise RuntimeError("Finalize cancellation before replacing its queue")
            self.verify()
            if pending_git_file(self.repo).exists() or local_head(self.repo) != self.state["head"]:
                raise RuntimeError("Saved Git work requires Resume before replanning in this repository")
            active = self.state["active"]
            journal = transfer_directory(self.repo) / active["journal"] if active else None
            if journal and journal.exists():
                state = json.loads(journal.read_text())
                if state["committed"] or any((self.repo / record["path"]).exists() or
                                             (self.repo / record["path"]).is_symlink()
                                             for record in records(state, journal)):
                    raise RuntimeError("Resume or safely clean up the saved copies before replanning")
                if self.partials_remain(journal, state):
                    raise RuntimeError("Interrupted temporary copies remain; run Cleanup or review "
                                       "them before replanning")
            archive = operation_file(self.repo).parent / "mover-canceled" / uuid.uuid4().hex
            archive.mkdir(parents=True)
            with closing(sqlite3.connect(archive / "operation.sqlite")) as backup:
                self.db.backup(backup)
            if journal:
                for artifact in journal.parent.glob(f"{journal.stem}.*"):
                    artifact.replace(archive / artifact.name)
            self.close()
            operation_file(self.repo).unlink()
            return archive

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
        saved = self.state.get("remote_commit")
        if saved and not remote:
            raise RuntimeError("Remote branch was deleted; review before resuming")
        if saved and remote != saved:
            # Pushing again would silently undo an external rewind or force-push.
            try:
                _git(self.repo, "merge-base", "--is-ancestor", saved, remote)
            except RuntimeError as exc:
                raise RuntimeError(
                    f"Remote branch was rewound or rewritten since the last verified batch "
                    f"(expected {saved[:8]} or a descendant, found {remote[:8]}). Nothing was "
                    "pushed. Fetch and review origin before resuming; local commits are "
                    "preserved.") from exc
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
            if self.pause.is_set() and not self.cancel_mode() and self.state["status"] != "Finishing Batch":
                report("Finishing Batch")
            progress(message)

        def checkpoint():
            if self.cancel_mode():
                raise CancellationRequested()

        def finalize():
            report("Cancel Requested")
            report("Finalizing")
            # Read/reconcile only. Uncertain remote state must never trigger publication or cleanup.
            try:
                self.verify()
            except Exception as exc:
                progress(f"Cancellation preserved all work; Git reconciliation requires review: {exc}")
            active = self.state["active"]
            journal = transfer_directory(self.repo) / active["journal"] if active else None
            report("Canceled")
            if journal and journal.exists():
                state = json.loads(journal.read_text())
                if not state["committed"]:
                    report("Cleanup Available")

        try:
            checkpoint()
            report("Resuming")
            self.verify()
            if self.state.get("garbage"):
                old = transfer_directory(self.repo) / self.state["garbage"]
                if old.exists():
                    remove_journal(old)
                self.state["garbage"] = None
                self.save()
            while True:
                checkpoint()
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
                                     self.state["branch"], journal=journal, checkpoint=checkpoint)
                if self.pause.is_set():
                    report("Finishing Batch")
                finish_transfer(self.repo, journal, batch_progress, retain=True, checkpoint=checkpoint)
                self.state.update(completed=active["last"],
                                  bytes=self.state["bytes"] + active["size"],
                                  batch=self.state["batch"] + 1, active=None,
                                  garbage=active["journal"],
                                  head=local_head(self.repo),
                                  remote_commit=remote_head(self.repo, self.state["branch"]))
                self.save()
                remove_journal(journal)
                checkpoint()
                if self.state.pop("retry_batch_only", False):
                    report("Paused")
                    return
                if not self.db.execute("SELECT 1 FROM queue WHERE id>? LIMIT 1",
                                       (self.state["completed"],)).fetchone():
                    report("Complete")
                    return
        except CancellationRequested:
            finalize()
        except Exception:
            if self.cancel_mode():
                finalize()
                return
            report("Error")
            raise

    def close(self):
        self.db.close()
