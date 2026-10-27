import json
import sqlite3
from pathlib import Path

import pytest
from test_operations import create_operation
from test_staging import run

import mover_git.discard as discard
import mover_git.git as git
import mover_git.transfer as transfer
from mover_git.storage import OperationLease


def canceled_commit(repo, monkeypatch, published=False):
    operation, pairs = create_operation(repo, count=5, maximum=1)
    original = transfer.push_unpublished if published else transfer.stage_commit_push

    def cancel(*args, **kwargs):
        original(*args, **kwargs)
        if operation.state['batch'] == 3:
            operation.request_cancel('transfer')

    name = 'push_unpublished' if published else 'stage_commit_push'
    monkeypatch.setattr(transfer, name, cancel)
    operation.run()
    monkeypatch.setattr(transfer, name, original)
    assert operation.state['batch'] == 3
    assert operation.state['status'] == 'Canceled'
    operation.close()
    return pairs


@pytest.mark.parametrize('published', [False, True])
def test_discard_committed_batch_preserves_files_and_history(repo, monkeypatch, published):
    pairs = canceled_commit(repo, monkeypatch, published)
    before = run(repo, 'status', '--porcelain'), transfer.local_head(repo), git.remote_head(repo, 'main')
    contents = {p: p.read_bytes() if p.exists() else None for pair in pairs for p in pair}
    reviews = []
    archive = discard.discard_job(repo, lambda summary: reviews.append(summary) or True)
    assert 'batch 4' in reviews[0] and 'abandons the old transfer' in reviews[0]
    assert not transfer.operation_file(repo).exists()
    assert (archive / 'mover-operation.sqlite').exists()
    assert list((archive / 'mover-transfers').glob('*.json'))
    assert contents == {p: p.read_bytes() if p.exists() else None for pair in pairs for p in pair}
    assert before == (run(repo, 'status', '--porcelain'), transfer.local_head(repo), git.remote_head(repo, 'main'))
    with sqlite3.connect(archive / 'mover-operation.sqlite') as db:
        assert db.execute('SELECT count(*) FROM queue').fetchone()[0] == 5


def test_intentional_deletion_commit_then_discard_and_restart(repo, monkeypatch):
    pairs = canceled_commit(repo, monkeypatch)
    run(repo, 'rm', 'target3')
    run(repo, 'commit', '-m', 'Remove incorrectly placed batch 4 files')
    run(repo, 'push', 'origin', 'main')
    head = transfer.local_head(repo)
    assert run(repo, 'status', '--porcelain') == b''
    reviews = []
    archive = discard.discard_job(repo, lambda text: reviews.append(text) or True)
    assert 'Remove incorrectly placed batch 4 files' in reviews[0]
    assert 'matches HEAD' in reviews[0]
    assert discard.recover_discard(repo) is None
    assert not git.pending_git_file(repo).exists()
    assert not list(transfer.transfer_directory(repo).glob('*.json'))
    assert transfer.local_head(repo) == git.remote_head(repo, 'main') == head
    assert not pairs[3][1].exists() and pairs[3][0].exists()
    # A new process can scan/create a corrected queue with no old journal recovery.
    new = transfer.BatchOperation.create(repo, [(pairs[4][0], repo / 'corrected')], 'main')
    new.run()
    new.close()
    assert (repo / 'corrected').exists()
    assert pairs[3][0].exists() and not pairs[3][1].exists()
    assert (archive / 'discard.json').exists()


@pytest.mark.parametrize('status', ['Paused', 'Running', 'Error', 'Cancel Requested', 'Finalizing'])
def test_discard_rejects_incomplete_cancellation(repo, status):
    operation, _ = create_operation(repo)
    operation.request_cancel('transfer')
    operation.save(status)
    operation.close()
    with pytest.raises(RuntimeError, match='finalized'):
        discard.discard_job(repo, lambda _: pytest.fail('must not ask for unsafe discard'))
    assert transfer.operation_file(repo).exists()


@pytest.mark.parametrize('lock', ['mover-operation.lock', 'mover-push.lock'])
def test_discard_blocks_running_worker_or_push(repo, lock):
    operation, _ = create_operation(repo)
    operation.request_cancel('transfer')
    operation.run()
    operation.close()
    with OperationLease(git.pending_git_file(repo).parent / lock):
        with pytest.raises(RuntimeError, match='active'):
            discard.discard_job(repo, lambda _: True)
    assert transfer.operation_file(repo).exists()


def test_discard_blocks_divergent_remote(repo, monkeypatch):
    canceled_commit(repo, monkeypatch, published=True)
    remote_commit = run(repo, 'commit-tree', 'HEAD^{tree}', '-m', 'Independent remote root').decode()
    run(repo, 'push', 'origin', f'{remote_commit}:refs/heads/other')
    run(repo.parent / 'remote.git', 'update-ref', 'refs/heads/main', remote_commit)
    with pytest.raises(RuntimeError, match='Remote diverged'):
        discard.discard_job(repo, lambda _: pytest.fail('unsafe remote'))
    assert transfer.operation_file(repo).exists()


def test_declined_discard_and_changed_confirmation_leave_records(repo, monkeypatch):
    canceled_commit(repo, monkeypatch)
    assert discard.discard_job(repo, lambda _: False) is None

    def confirm(_):
        run(repo, 'commit', '--allow-empty', '-m', 'Concurrent edit')
        return True

    with pytest.raises(RuntimeError, match='changed during confirmation'):
        discard.discard_job(repo, confirm)
    assert transfer.operation_file(repo).exists()
    assert not (git.pending_git_file(repo).parent / 'mover-discard.json').exists()


def test_discard_archives_owned_pending_git_without_replaying_it(repo, monkeypatch):
    operation, pairs = create_operation(repo, count=1)
    original = git._git

    def cancel(repository, *args, **kwargs):
        result = original(repository, *args, **kwargs)
        if 'commit' in args:
            operation.request_cancel('transfer')
        return result

    monkeypatch.setattr(git, '_git', cancel)
    operation.run()
    operation.close()
    monkeypatch.setattr(git, '_git', original)
    assert git.pending_git_file(repo).exists()
    head = transfer.local_head(repo)
    archive = discard.discard_job(repo, lambda _: True)
    assert (archive / 'mover-pending.json').exists()
    assert (archive / 'mover-pending.jsonl').exists()
    assert not git.pending_git_file(repo).exists()
    assert transfer.local_head(repo) == head
    assert git.remote_head(repo, 'main') == ''
    assert all(s.exists() and d.exists() for s, d in pairs)


@pytest.mark.parametrize('point', ['archive', 'clear'])
def test_discard_crash_preserves_records_and_restart_finishes(repo, monkeypatch, point):
    canceled_commit(repo, monkeypatch)
    original_copy, original_unlink = discard.shutil.copyfile, Path.unlink

    class Crash(BaseException):
        pass

    def copy(*args, **kwargs):
        original_copy(*args, **kwargs)
        if point == 'archive':
            raise Crash()

    def unlink(path, *args, **kwargs):
        original_unlink(path, *args, **kwargs)
        if point == 'clear' and path.parent == transfer.transfer_directory(repo):
            raise Crash()

    monkeypatch.setattr(discard.shutil, 'copyfile', copy)
    monkeypatch.setattr(Path, 'unlink', unlink)
    with pytest.raises(Crash):
        discard.discard_job(repo, lambda _: True)
    monkeypatch.setattr(discard.shutil, 'copyfile', original_copy)
    monkeypatch.setattr(Path, 'unlink', original_unlink)
    assert transfer.operation_file(repo).exists()
    if point == 'clear':
        archive = discard.recover_discard(repo)
        assert not transfer.operation_file(repo).exists()
        state = json.loads((archive / 'discard.json').read_text())
        for record in state['files']:
            assert git.file_digest(archive / record['path']) == record['hash']
        assert discard.recover_discard(repo) is None
    else:
        assert discard.recover_discard(repo) is None
        assert discard.discard_job(repo, lambda _: True).exists()


def test_actual_push_lease_blocks_discard(repo, monkeypatch):
    operation, _ = create_operation(repo)
    operation.request_cancel('transfer')
    operation.run()
    operation.close()

    def pushing(*args):
        with pytest.raises(RuntimeError, match='active'):
            discard.discard_job(repo, lambda _: True)

    monkeypatch.setattr(git, '_push_unpublished', pushing)
    git.push_unpublished(repo, 'main')
    assert transfer.operation_file(repo).exists()


def test_discard_preserves_unrelated_staged_and_working_files(repo):
    operation, pairs = create_operation(repo)
    operation.request_cancel('transfer')
    operation.run()
    operation.close()
    unrelated = repo / 'unrelated'
    unrelated.write_text('staged')
    run(repo, 'add', 'unrelated')
    unrelated.write_text('working')
    index = run(repo, 'ls-files', '--stage')
    discard.discard_job(repo, lambda _: True)
    assert run(repo, 'ls-files', '--stage') == index
    assert unrelated.read_text() == 'working'
    assert all(s.exists() and not d.exists() for s, d in pairs)


def test_unowned_pending_records_block_discard(repo):
    operation, _ = create_operation(repo)
    operation.request_cancel('transfer')
    operation.run()
    operation.close()
    path = git.pending_git_file(repo).with_suffix('.building')
    path.write_text('unknown staging work')
    with pytest.raises(RuntimeError, match='no ownership journal'):
        discard.discard_job(repo, lambda _: pytest.fail('must preserve unknown records'))
    assert path.read_text() == 'unknown staging work'
    assert transfer.operation_file(repo).exists()


def test_discard_blocks_another_open_database(repo):
    operation, _ = create_operation(repo)
    operation.request_cancel('transfer')
    operation.run()
    operation.close()
    other = sqlite3.connect(transfer.operation_file(repo))
    other.execute('SELECT value FROM state').fetchall()
    try:
        with pytest.raises(RuntimeError, match='another connection'):
            discard.discard_job(repo, lambda _: True)
        assert transfer.operation_file(repo).exists()
    finally:
        other.close()
    assert discard.discard_job(repo, lambda _: True).exists()


def test_restart_refuses_damaged_archive_before_clearing_metadata(repo, monkeypatch):
    operation, _ = create_operation(repo)
    operation.request_cancel('transfer')
    operation.run()
    operation.close()
    original = discard.finish_discard

    def interrupted(root):
        raise OSError('interrupted after durable marker')

    monkeypatch.setattr(discard, 'finish_discard', interrupted)
    with pytest.raises(OSError, match='interrupted'):
        discard.discard_job(repo, lambda _: True)
    monkeypatch.setattr(discard, 'finish_discard', original)
    root = git.pending_git_file(repo).parent
    state = json.loads((root / 'mover-discard.json').read_text())
    (root / 'mover-canceled' / state['archive'] / 'mover-operation.sqlite').write_bytes(b'damaged')
    with pytest.raises(RuntimeError, match='records changed'):
        discard.recover_discard(repo)
    assert transfer.operation_file(repo).exists()
    with pytest.raises(RuntimeError, match='startup recovery'):
        transfer.BatchOperation(repo)


@pytest.mark.parametrize('damage', ['missing_manifest', 'branch'])
def test_discard_blocks_unreconciled_transfer_records(repo, monkeypatch, damage):
    canceled_commit(repo, monkeypatch)
    journal = next(transfer.transfer_directory(repo).glob('*.json'))
    if damage == 'missing_manifest':
        journal.with_suffix('.jsonl').unlink()
    else:
        state = json.loads(journal.read_text())
        state['branch'] = 'unrelated'
        journal.write_text(json.dumps(state))
    with pytest.raises(RuntimeError, match='manifest is missing or branch differs'):
        discard.discard_job(repo, lambda _: pytest.fail('must reconcile before confirmation'))
    assert transfer.operation_file(repo).exists()
