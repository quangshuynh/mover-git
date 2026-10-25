"""Small disk-backed containers for file inventories and batch manifests."""

import json
import os
import sqlite3
import tempfile
from pathlib import Path

from mover_git.core import FileEntry


def walk_entries(directory, bottom_up=False):
    """Stream directory entries instead of collecting a wide directory's file list."""
    with os.scandir(directory) as entries:
        for entry in entries:
            path = Path(entry.path)
            is_directory = entry.is_dir(follow_symlinks=False)
            if not bottom_up or not is_directory:
                yield path, is_directory
            if is_directory:
                yield from walk_entries(path, bottom_up)
                if bottom_up:
                    yield path, True


class OperationLease:
    """OS-released lock: a killed process cannot leave an operation permanently locked."""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.stream = self.path.open("a+b")
        self.stream.write(b"\0")
        self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            raise RuntimeError("Another Git-Mover operation is active in this repository") from exc
        return self

    def __exit__(self, *args):
        self.stream.close()


class DiskSet:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mover-")
        self.db = sqlite3.connect(Path(self.directory.name) / "paths.sqlite")
        self.db.execute("CREATE TABLE paths (path TEXT PRIMARY KEY)")

    def __contains__(self, path):
        return self.db.execute("SELECT 1 FROM paths WHERE path=?",
                               (os.path.normcase(str(path)),)).fetchone() is not None

    def add(self, path):
        self.db.execute("INSERT OR IGNORE INTO paths VALUES(?)", (os.path.normcase(str(path)),))

    def close(self):
        self.db.close()
        self.directory.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FileInventory:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mover-scan-")
        self.db = sqlite3.connect(Path(self.directory.name) / "files.sqlite",
                                  check_same_thread=False)
        self.db.execute("CREATE TABLE files (source TEXT, relative TEXT, size INTEGER, sort TEXT)")
        self.count = 0

    def append(self, entry):
        self.db.execute("INSERT INTO files VALUES(?,?,?,?)", (
            str(entry.src_path), str(entry.rel_path), entry.size, str(entry.rel_path).lower()))
        self.count += 1

    def sort(self, **kwargs):
        self.db.execute("CREATE INDEX IF NOT EXISTS ordering ON files(sort)")
        self.db.commit()

    def __len__(self):
        return self.count

    def __iter__(self):
        for source, relative, size in self.db.execute(
            "SELECT source,relative,size FROM files ORDER BY sort"
        ):
            yield FileEntry(Path(source), Path(relative), size)

    def close(self):
        self.db.close()
        self.directory.cleanup()


def records(state, journal):
    """Read old inline journals as well as streaming manifests."""
    if "files" in state:
        yield from state["files"]
    else:
        with journal.with_suffix(".jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                yield json.loads(line)


def remove_journal(journal):
    journal.unlink()
    journal.with_suffix(".jsonl").unlink(missing_ok=True)
