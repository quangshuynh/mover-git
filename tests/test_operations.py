import json
from pathlib import Path

import pytest
from test_staging import run

import mover_git.git as git
import mover_git.transfer as transfer
from mover_git.core import MAX_BATCH_SIZE_BYTES, MAX_FILE_SIZE_BYTES


def create_operation(repo, count=5, maximum=2):
    pairs = []
    for index in range(count):
        source = repo.parent / f"source{index}"
        source.write_text(f"payload {index}")
        pairs.append((source, repo / f"target{index}"))
    return transfer.BatchOperation.create(repo, iter(pairs), "main", max_files=maximum), pairs


@pytest.mark.parametrize("new_limit,counts", [(1, [2, 1, 1, 1]), (None, [2, 3]), (10000, [2, 3])])
def test_setting_change_freezes_active_batch(repo, monkeypatch, new_limit, counts):
    operation, pairs = create_operation(repo)
    observed = []
    original = transfer.finish_transfer

    def finish(repository, journal, *args, **kwargs):
        state = json.loads(journal.read_text())
        observed.append(len(list(transfer.records(state, journal))))
        if len(observed) == 1:
            operation.set_limit(new_limit)
        original(repository, journal, *args, **kwargs)

    monkeypatch.setattr(transfer, "finish_transfer", finish)
    operation.run()
    assert observed == counts
    assert operation.state["completed"] == 5
    assert operation.state["status"] == "Complete"
    assert run(repo, "rev-list", "--count", "HEAD") == str(len(counts)).encode()
    assert all(not source.exists() and destination.exists() for source, destination in pairs)
    operation.close()


@pytest.mark.parametrize("point", ["copy", "before_commit", "after_commit", "push"])
def test_pause_finishes_batch_and_restart_does_not_repeat(repo, monkeypatch, point):
    operation, pairs = create_operation(repo, count=4)
    statuses = []
    original_commit = transfer.stage_commit_push
    original_push = transfer.push_unpublished
    original_copy = transfer.shutil.copyfileobj

    def commit(*args, **kwargs):
        if point == "before_commit":
            operation.pause.set()
        original_commit(*args, **kwargs)
        if point == "after_commit":
            operation.pause.set()

    def push(*args, **kwargs):
        if point == "push":
            operation.pause.set()
        original_push(*args, **kwargs)

    def copy(*args, **kwargs):
        if point == "copy":
            operation.pause.set()
        original_copy(*args, **kwargs)

    monkeypatch.setattr(transfer, "stage_commit_push", commit)
    monkeypatch.setattr(transfer, "push_unpublished", push)
    monkeypatch.setattr(transfer.shutil, "copyfileobj", copy)
    operation.run(status=statuses.append)
    assert operation.state["status"] == "Paused"
    assert operation.state["completed"] == 2
    first_head = run(repo, "rev-parse", "HEAD").decode()
    assert git.remote_head(repo, "main") == first_head
    assert not pairs[0][0].exists() and pairs[2][0].exists()
    operation.close()
    monkeypatch.setattr(transfer, "stage_commit_push", original_commit)
    monkeypatch.setattr(transfer, "push_unpublished", original_push)
    copied = []

    def record_copy(incoming, outgoing, length):
        copied.append(Path(incoming.name).name)
        original_copy(incoming, outgoing, length)

    monkeypatch.setattr(transfer.shutil, "copyfileobj", record_copy)
    reopened = transfer.BatchOperation(repo)
    reopened.run(status=statuses.append)
    assert copied == ["source2", "source3"]
    assert run(repo, "rev-list", "--count", "HEAD") == b"2"
    assert "Resuming" in statuses and statuses[-1] == "Complete"
    reopened.run()
    assert run(repo, "rev-list", "--count", "HEAD") == b"2"
    reopened.close()


def test_pause_before_first_batch_and_limit_persists(repo):
    operation, pairs = create_operation(repo)
    operation.pause.set()
    operation.set_limit(10000)
    operation.run()
    assert operation.state["active"] is None
    assert operation.state["status"] == "Paused"
    assert all(source.exists() and not destination.exists() for source, destination in pairs)
    operation.close()
    reopened = transfer.BatchOperation(repo)
    assert reopened.max_files == 10000
    reopened.run()
    assert run(repo, "rev-list", "--count", "HEAD") == b"1"
    reopened.close()


def test_push_failure_with_pause_is_error_and_resume_preserves_active(repo, monkeypatch):
    operation, pairs = create_operation(repo, count=3)
    original = transfer.push_unpublished

    def fail(*args, **kwargs):
        operation.pause.set()
        raise RuntimeError("offline")

    monkeypatch.setattr(transfer, "push_unpublished", fail)
    with pytest.raises(RuntimeError, match="offline"):
        operation.run()
    assert operation.state["status"] == "Error"
    assert operation.state["completed"] == 0
    assert all(source.exists() for source, _ in pairs)
    operation.set_limit(1)
    frozen = operation.state["active"].copy()
    operation.close()
    monkeypatch.setattr(transfer, "push_unpublished", original)
    reopened = transfer.BatchOperation(repo)
    assert reopened.state["active"] == frozen
    reopened.run()
    assert run(repo, "rev-list", "--count", "HEAD") == b"2"
    assert all(not source.exists() for source, _ in pairs)
    reopened.close()


@pytest.mark.parametrize("point", ["planned", "committed", "verified", "checkpointed"])
def test_crash_windows_resume_exactly_once(repo, monkeypatch, point):
    class Crash(BaseException):
        pass

    operation, pairs = create_operation(repo, count=3)
    original_prepare = transfer.prepare_transfer
    original_commit = transfer.stage_commit_push
    original_finish = transfer.finish_transfer
    original_remove = transfer.remove_journal

    def prepare(*args, **kwargs):
        if point == "planned":
            raise Crash()
        return original_prepare(*args, **kwargs)

    def commit(*args, **kwargs):
        original_commit(*args, **kwargs)
        if point == "committed":
            raise Crash()

    def finish(*args, **kwargs):
        original_finish(*args, **kwargs)
        if point == "verified":
            raise Crash()

    def remove(*args, **kwargs):
        if point == "checkpointed":
            raise Crash()
        original_remove(*args, **kwargs)

    monkeypatch.setattr(transfer, "prepare_transfer", prepare)
    monkeypatch.setattr(transfer, "stage_commit_push", commit)
    monkeypatch.setattr(transfer, "finish_transfer", finish)
    monkeypatch.setattr(transfer, "remove_journal", remove)
    with pytest.raises(Crash):
        operation.run()
    operation.close()
    monkeypatch.setattr(transfer, "prepare_transfer", original_prepare)
    monkeypatch.setattr(transfer, "stage_commit_push", original_commit)
    monkeypatch.setattr(transfer, "finish_transfer", original_finish)
    monkeypatch.setattr(transfer, "remove_journal", original_remove)
    reopened = transfer.BatchOperation(repo)
    reopened.run()
    assert run(repo, "rev-list", "--count", "HEAD") == b"2"
    assert all(not source.exists() and destination.read_text() == f"payload {index}"
               for index, (source, destination) in enumerate(pairs))
    reopened.close()


@pytest.mark.parametrize("change", ["branch", "head", "remote", "remote_deleted"])
def test_recovery_blocks_changed_git_state(repo, change):
    operation, pairs = create_operation(repo, count=3)
    operation.run(progress=lambda _: operation.pause.set())
    operation.close()
    if change == "branch":
        run(repo, "checkout", "-b", "other")
    elif change == "head":
        run(repo, "commit", "--allow-empty", "-m", "unrelated")
    elif change == "remote":
        run(repo, "remote", "set-url", "origin", "changed")
    else:
        # The bare remote may use main as its default branch on this machine.
        run(repo.parent / "remote.git", "config", "receive.denyDeleteCurrent", "ignore")
        run(repo, "push", "origin", ":main")
    reopened = transfer.BatchOperation(repo)
    with pytest.raises(RuntimeError):
        reopened.run()
    assert pairs[-1][0].exists()
    assert not pairs[-1][1].exists()
    assert reopened.state["status"] == "Error"
    reopened.close()


def test_disk_queue_exact_byte_boundary(repo):
    operation, _ = create_operation(repo, count=4, maximum=None)
    operation.db.execute("UPDATE queue SET size=? WHERE id IN (1,2)", (MAX_FILE_SIZE_BYTES,))
    operation.db.execute("UPDATE queue SET size=0 WHERE id=3")
    operation.db.commit()
    batch = operation.select_batch()
    assert batch["count"] == 2
    assert batch["size"] == MAX_BATCH_SIZE_BYTES
    operation.close()


def test_parallel_resume_is_rejected(repo):
    from mover_git.storage import OperationLease

    operation, pairs = create_operation(repo)
    with OperationLease(transfer.operation_file(repo).with_suffix(".lock")):
        with pytest.raises(RuntimeError, match="Another Git-Mover"):
            operation.run()
    assert all(source.exists() for source, _ in pairs)
    operation.run()
    assert operation.state["status"] == "Complete"
    operation.close()


def test_limit_change_during_manifest_preparation_does_not_block_reader(repo, monkeypatch):
    operation, _ = create_operation(repo, count=4)
    original = transfer.prepare_transfer
    changed = False

    def prepare(repository, pairs, *args, **kwargs):
        def stream():
            nonlocal changed
            for pair in pairs:
                if not changed:
                    # The queue SELECT cursor is still open. UI writes must not wait for it.
                    operation.set_limit(1)
                    changed = True
                yield pair
        return original(repository, stream(), *args, **kwargs)

    monkeypatch.setattr(transfer, "prepare_transfer", prepare)
    operation.run()
    assert changed
    assert run(repo, "rev-list", "--count", "HEAD") == b"3"
    operation.close()


def test_large_unlimited_inventory_stays_on_disk(tmp_path):
    import tracemalloc

    from mover_git.core import FileEntry, batch_sizes
    from mover_git.storage import FileInventory

    tracemalloc.start()
    inventory = FileInventory()
    try:
        for index in range(30000):
            path = Path(str(index))
            inventory.append(FileEntry(tmp_path / path, path, 0))
        inventory.sort()
        assert list(batch_sizes(inventory)) == [(30000, 0)]
        assert tracemalloc.get_traced_memory()[1] < 8 * 1024**2
    finally:
        inventory.close()
        tracemalloc.stop()
