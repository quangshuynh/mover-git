from pathlib import Path

import pytest
from test_operations import create_operation
from test_staging import run

import mover_git.git as git
import mover_git.transfer as transfer


@pytest.mark.parametrize('mode', ['batch', 'transfer'])
def test_cancel_before_first_batch_persists(repo, mode):
    operation, pairs = create_operation(repo)
    operation.request_cancel(mode)
    statuses = []
    operation.run(status=statuses.append)
    assert statuses == ['Cancel Requested', 'Finalizing', 'Canceled']
    assert all(s.exists() and not d.exists() for s, d in pairs)
    operation.close()
    operation = transfer.BatchOperation(repo)
    operation.run()
    assert operation.state['status'] == 'Canceled'
    assert operation.state['completed'] == 0
    operation.resume()
    operation.run()
    assert operation.state['completed'] == (2 if mode == 'batch' else 5)
    operation.close()


def cancel_during_copy(repo, monkeypatch, mode='transfer'):
    operation, pairs = create_operation(repo, count=4)
    original = transfer.shutil.copyfileobj

    def copy(incoming, outgoing, length):
        outgoing.write(incoming.read(2))
        operation.request_cancel(mode)
        original(incoming, outgoing, length)

    monkeypatch.setattr(transfer.shutil, 'copyfileobj', copy)
    operation.run()
    monkeypatch.setattr(transfer.shutil, 'copyfileobj', original)
    return operation, pairs


def test_cancel_mid_copy_is_atomic_and_resume_stops_after_batch(repo, monkeypatch):
    operation, pairs = cancel_during_copy(repo, monkeypatch, 'batch')
    assert operation.state['status'] == 'Cleanup Available'
    assert pairs[0][1].read_bytes() == pairs[0][0].read_bytes()
    assert all(s.exists() for s, _ in pairs)
    assert not pairs[1][1].exists()
    operation.close()
    operation = transfer.BatchOperation(repo)
    operation.resume()
    operation.run()
    assert operation.state['completed'] == 2
    assert operation.state['status'] == 'Paused'
    assert all(s.exists() and not d.exists() for s, d in pairs[2:])
    assert run(repo, 'rev-list', '--count', 'HEAD') == b'1'
    operation.close()


@pytest.mark.parametrize('point', ['hash', 'commit', 'push', 'remote_accepted', 'lost_response'])
def test_cancel_during_git_or_hash_preserves_sources_and_no_duplicate_commit(repo, monkeypatch, point):
    operation, pairs = create_operation(repo, count=4)
    original_git, original_digest = git._git, transfer.digest

    def intercept(repository, *args, **kwargs):
        if point == 'push' and 'push' in args:
            operation.request_cancel('transfer')
        result = original_git(repository, *args, **kwargs)
        if ((point == 'commit' and 'commit' in args) or
                (point in ('remote_accepted', 'lost_response') and 'push' in args)):
            operation.request_cancel('transfer')
            if point == 'lost_response':
                raise RuntimeError('lost response after acceptance')
        return result

    def digest(path):
        result = original_digest(path)
        if point == 'hash':
            operation.request_cancel('transfer')
        return result

    monkeypatch.setattr(git, '_git', intercept)
    monkeypatch.setattr(transfer, 'digest', digest)
    operation.run()
    assert operation.cancel_mode() == 'transfer'
    assert operation.state['completed'] == 0
    assert all(s.exists() for s, _ in pairs)
    assert all(not d.exists() for _, d in pairs[2:])
    if point in ('push', 'remote_accepted', 'lost_response'):
        assert git.remote_head(repo, 'main') == transfer.local_head(repo)
    operation.close()
    monkeypatch.setattr(git, '_git', original_git)
    monkeypatch.setattr(transfer, 'digest', original_digest)
    operation = transfer.BatchOperation(repo)
    operation.run()  # startup does not silently publish or continue
    assert operation.state['completed'] == 0
    operation.resume()
    operation.run()
    assert run(repo, 'rev-list', '--count', 'HEAD') == b'2'
    assert all(not s.exists() for s, _ in pairs)
    operation.close()


@pytest.mark.parametrize('source_change', ['modified', 'missing', 'symlink', 'unchanged'])
def test_cleanup_checks_source_and_preserves_unrelated_index(repo, monkeypatch, source_change):
    unrelated = repo / 'unrelated'
    unrelated.write_text('staged')
    run(repo, 'add', 'unrelated')
    unrelated.write_text('working')
    staged = run(repo, 'ls-files', '--stage')
    operation, pairs = cancel_during_copy(repo, monkeypatch)
    source, destination = pairs[0]
    if source_change == 'modified':
        source.write_text('edited')
    elif source_change == 'missing':
        source.unlink()
    elif source_change == 'symlink':
        source.unlink()
        source.symlink_to(destination)
    messages = []
    operation.cleanup(messages.append)
    assert destination.exists() == (source_change != 'unchanged')
    assert run(repo, 'ls-files', '--stage') == staged
    assert unrelated.read_text() == 'working'
    if source_change != 'unchanged':
        assert any('Preserved' in message for message in messages)
    else:
        assert source.exists()
        operation.resume()
        operation.run()
        assert operation.state['status'] == 'Complete'
    operation.close()


@pytest.mark.parametrize('change', ['replacement', 'modified', 'staged', 'committed'])
def test_cleanup_refuses_unproven_destination(repo, monkeypatch, change):
    operation, pairs = cancel_during_copy(repo, monkeypatch)
    source, destination = pairs[0]
    if change == 'replacement':
        # Keep the original inode alive to avoid filesystem inode reuse.
        destination.rename(repo / 'original-copy')
        destination.write_bytes(source.read_bytes())
    elif change == 'modified':
        destination.write_text('changed')
    else:
        run(repo, 'add', destination.name)
        if change == 'committed':
            run(repo, 'commit', '-m', 'external commit')
    try:
        operation.cleanup()
    except RuntimeError:
        assert change == 'committed'
    assert destination.exists() and source.exists()
    operation.close()


@pytest.mark.parametrize('point', ['copy', 'commit', 'push'])
def test_crash_with_pending_cancellation_is_recoverable(repo, monkeypatch, point):
    class Crash(BaseException):
        pass

    operation, pairs = create_operation(repo, count=4)
    original = git._git
    original_copy = transfer.shutil.copyfileobj

    def intercept(repository, *args, **kwargs):
        result = original(repository, *args, **kwargs)
        if point in args:
            operation.request_cancel('transfer')
            raise Crash()
        return result

    def copy(*args, **kwargs):
        original_copy(*args, **kwargs)
        operation.request_cancel('transfer')
        raise Crash()

    monkeypatch.setattr(git, '_git', intercept)
    if point == 'copy':
        monkeypatch.setattr(transfer.shutil, 'copyfileobj', copy)
    with pytest.raises(Crash):
        operation.run()
    operation.close()
    monkeypatch.setattr(git, '_git', original)
    monkeypatch.setattr(transfer.shutil, 'copyfileobj', original_copy)
    operation = transfer.BatchOperation(repo)
    operation.run()
    assert operation.state['status'] in ('Canceled', 'Cleanup Available')
    assert all(s.exists() for s, _ in pairs)
    operation.cleanup()
    operation.resume()
    operation.run()
    assert run(repo, 'rev-list', '--count', 'HEAD') == b'2'
    operation.close()


def test_cancel_preserves_completed_batches(repo, monkeypatch):
    operation, pairs = create_operation(repo, count=4)
    original = transfer.shutil.copyfileobj

    def copy(incoming, outgoing, length):
        if Path(incoming.name) == pairs[2][0]:
            operation.request_cancel('transfer')
        original(incoming, outgoing, length)

    monkeypatch.setattr(transfer.shutil, 'copyfileobj', copy)
    operation.run()
    assert operation.state['batch'] == 1
    head = transfer.local_head(repo)
    operation.cleanup()
    assert git.remote_head(repo, 'main') == head
    assert all(not s.exists() and d.exists() for s, d in pairs[:2])
    assert all(s.exists() for s, _ in pairs[2:])
    operation.close()


def test_cleanup_published_batch_never_deletes_destination(repo, monkeypatch):
    operation, pairs = create_operation(repo, count=2)
    original = transfer.push_unpublished

    def push(*args, **kwargs):
        original(*args, **kwargs)
        operation.request_cancel('transfer')

    monkeypatch.setattr(transfer, 'push_unpublished', push)
    operation.run()
    messages = []
    operation.cleanup(messages.append)
    assert all(s.exists() and d.exists() for s, d in pairs)
    assert any('committed' in message for message in messages)
    assert git.remote_head(repo, 'main') == transfer.local_head(repo)
    operation.close()


def test_legacy_resume_does_not_bypass_canceled_queue(repo):
    operation, _ = create_operation(repo)
    operation.request_cancel('transfer')
    with pytest.raises(RuntimeError, match='Canceled transfer'):
        transfer.resume_transfers(repo, 'main')
    operation.close()


def test_cancellation_is_not_overwritten_by_worker_save(repo):
    operation, _ = create_operation(repo)
    operation.request_cancel('transfer')
    operation.save('Running')
    operation.request_cancel('batch')
    assert operation.cancel_mode() == 'transfer'
    operation.close()


def test_cancel_after_staging_preserves_pending_git_and_unrelated_index(repo, monkeypatch):
    operation, pairs = create_operation(repo, count=3)
    unrelated = repo / 'unrelated'
    unrelated.write_text('staged content')
    run(repo, 'add', 'unrelated')
    index = run(repo, 'ls-files', '--stage', 'unrelated')
    original = git._git

    def intercept(repository, *args, **kwargs):
        result = original(repository, *args, **kwargs)
        if 'add' in args:
            operation.request_cancel('transfer')
        return result

    monkeypatch.setattr(git, '_git', intercept)
    operation.run()
    assert git.pending_git_file(repo).exists()
    assert transfer.local_head(repo) == ''
    operation.cleanup()
    assert all(s.exists() for s, _ in pairs)
    assert run(repo, 'ls-files', '--stage', 'unrelated') == index
    monkeypatch.setattr(git, '_git', original)
    operation.resume()
    operation.run()
    assert run(repo, 'rev-list', '--count', 'HEAD') == b'2'
    assert run(repo, 'ls-files', '--stage', 'unrelated') == index
    operation.close()


def test_corrected_paths_can_replace_canceled_queue_after_safe_cleanup(repo, monkeypatch):
    operation, pairs = cancel_during_copy(repo, monkeypatch)
    with pytest.raises(RuntimeError, match='clean up'):
        operation.retire()
    operation.cleanup()
    archive = operation.retire()
    assert (archive / 'operation.sqlite').exists()
    source = pairs[-1][0]
    replacement = transfer.BatchOperation.create(repo, [(source, repo / 'corrected')], 'main')
    replacement.run()
    assert (repo / 'corrected').exists()
    assert all(s.exists() for s, _ in pairs[:-1])
    replacement.close()
