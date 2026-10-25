import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import mover_git.git as git
from mover_git.core import FileEntry


def run(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo).rstrip(b"\r\n")


def test_thousands_of_literal_paths_and_long_names(repo, monkeypatch):
    names = [f"file {i:04d} [x] Ã©.txt" for i in range(2200)]
    names += ["-option.txt", " leading space.txt", "a&b;$(echo).txt", "x" * 180 + ".txt"]
    paths = [repo / name for name in names]
    for path in paths:
        path.write_bytes(b"payload")
    # Would match [x] as a glob if literal pathspec handling were missing.
    unselected = repo / "file 0000 x Ã©.txt"
    unselected.write_bytes(b"leave alone")
    original = git.subprocess.run
    calls = []

    def record(command, **kwargs):
        for argument in command:
            if argument.startswith("--pathspec-from-file="):
                calls.append(Path(argument.split("=", 1)[1]).read_bytes())
        assert len(subprocess.list2cmdline(command)) < 2000
        return original(command, **kwargs)

    monkeypatch.setattr(git.subprocess, "run", record)
    git.check_staging_support(repo)
    git.stage_commit_push(repo, paths, "batch", "main")
    tracked = set(run(repo, "ls-tree", "-rz", "--name-only", "HEAD").split(b"\0")) - {b""}
    assert tracked == {name.encode() for name in names}
    assert len(calls) == 2
    for payload in calls:
        assert set(payload.split(b"\0")) - {b""} == tracked
    assert run(repo, "rev-list", "--count", "HEAD") == b"1"
    assert not git.pending_git_file(repo).exists()


@pytest.mark.parametrize("failure", ["add", "commit", "push"])
@pytest.mark.parametrize("initial_commit", [False, True])
def test_failed_git_work_resumes_once(repo, monkeypatch, failure, initial_commit):
    if initial_commit:
        run(repo, "commit", "--allow-empty", "-m", "initial")
    source = repo.parent / "source"
    source.mkdir()
    original_file = source / "hello [x] Ã©.txt"
    original_file.write_bytes(b"precious")
    destination = repo / original_file.name
    shutil.move(original_file, destination)
    original = git._git

    def fail(repo, *args, **kwargs):
        if failure in args:
            raise RuntimeError("injected failure")
        return original(repo, *args, **kwargs)

    monkeypatch.setattr(git, "_git", fail)
    with pytest.raises(RuntimeError, match="injected"):
        git.stage_commit_push(repo, [destination], "batch", "main")
    assert source.is_dir()
    assert destination.read_bytes() == b"precious"
    assert git.pending_git_file(repo).exists()
    monkeypatch.setattr(git, "_git", original)
    assert git.finish_pending_git(repo)
    assert not git.finish_pending_git(repo)
    assert run(repo, "rev-list", "--count", "HEAD") == (b"2" if initial_commit else b"1")
    assert run(repo, "rev-parse", "HEAD") == run(repo, "rev-parse", "origin/main")


def test_old_git_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(
        git.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(stdout=b"usage: git add", stderr=b""),
    )
    with pytest.raises(RuntimeError, match="Upgrade Git"):
        git.check_staging_support(tmp_path)


@pytest.mark.parametrize("batch_commits", [False, True])
def test_transfer_failure_retains_source_dirs_and_retry_does_not_move_twice(
    repo,
    monkeypatch,
    batch_commits,
):
    from app import FileMoverGitApp

    app = object.__new__(FileMoverGitApp)
    source = repo.parent / "source"
    folder = source / "nested"
    folder.mkdir(parents=True)
    files = []
    for name in ["one.txt", "two.txt"]:
        path = folder / name
        path.write_text(name)
        files.append(FileEntry(path, path.relative_to(source), path.stat().st_size))
    app.valid_files = files
    app.dest_var = SimpleNamespace(get=lambda: str(repo))
    app.source_var = SimpleNamespace(get=lambda: str(source))
    app.commit_prefix_var = SimpleNamespace(get=lambda: "batch")
    app.session_file = repo.parent / "config" / "session.json"
    app.max_files = 1
    app.pause_event = threading.Event()
    app.batches = [[f] for f in files]
    app.validate_paths = lambda: (source, repo, repo)
    app.include_empty_dirs_var = SimpleNamespace(get=lambda: False)
    app.commit_each_batch_var = SimpleNamespace(get=lambda: batch_commits)
    app.remote_branch_var = SimpleNamespace(get=lambda: "main")
    app.make_commit_message = lambda *args: "batch " + str(args[0])
    app.build_target_path = lambda dst, entry, used: dst / entry.rel_path
    app.ask_yes_no = lambda *args: True
    app.notebook = SimpleNamespace(select=Mock())
    app.root = SimpleNamespace(update_idletasks=Mock())
    for name in [
        "set_status",
        "log",
        "show_error",
        "show_info",
        "ui",
        "update_progress",
        "show_github_link",
    ]:
        setattr(app, name, Mock())

    def scan():
        app.valid_files = [f for f in files if f.src_path.exists()]
        app.batches = [[f] for f in app.valid_files]

    app.scan_and_preview = scan
    original = git._git

    def fail(repo, *args, **kwargs):
        if "add" in args:
            raise RuntimeError("injected staging failure")
        return original(repo, *args, **kwargs)

    monkeypatch.setattr(git, "_git", fail)
    app.move_and_push()
    assert folder.is_dir()
    assert git.pending_git_file(repo).exists()
    monkeypatch.setattr(git, "_git", original)
    copied = []
    original_copy = shutil.copyfileobj

    def record_copy(src, dst, length):
        copied.append(src.name)
        return original_copy(src, dst, length)

    monkeypatch.setattr(shutil, "copyfileobj", record_copy)
    app.move_and_push()
    assert len(copied) == 1
    assert run(repo, "rev-list", "--count", "HEAD") == b"2"
    for entry in files:
        assert (repo / entry.rel_path).read_text() == entry.src_path.name
    assert not git.pending_git_file(repo).exists()


def test_partial_staging_retry_keeps_unrelated_changes_out_of_commit(repo, monkeypatch):
    selected = [repo / "a.txt", repo / "b.txt"]
    for path in selected:
        path.write_bytes(b"keep")
    original = git._git

    def fail_after_partial_add(repo, *args, **kwargs):
        if "add" in args:
            original(repo, "add", "--", "a.txt")
            raise RuntimeError("partial staging")
        return original(repo, *args, **kwargs)

    monkeypatch.setattr(git, "_git", fail_after_partial_add)
    with pytest.raises(RuntimeError, match="partial staging"):
        git.stage_commit_push(repo, selected, "batch", "main")
    extra = repo / "unrelated.txt"
    extra.write_bytes(b"unrelated")
    run(repo, "add", "--", extra.name)
    monkeypatch.setattr(git, "_git", original)
    git.finish_pending_git(repo)
    assert run(repo, "ls-tree", "-rz", "--name-only", "HEAD") == b"a.txt\0b.txt\0"
    assert run(repo, "diff", "--cached", "--name-only") == b"unrelated.txt"


def test_unrelated_head_change_blocks_retry(repo, monkeypatch):
    path = repo / "a.txt"
    path.write_bytes(b"keep")
    original = git._git

    def fail(repo, *args, **kwargs):
        if "add" in args:
            raise RuntimeError("staging failed")
        return original(repo, *args, **kwargs)

    monkeypatch.setattr(git, "_git", fail)
    with pytest.raises(RuntimeError):
        git.stage_commit_push(repo, [path], "batch", "main")
    monkeypatch.setattr(git, "_git", original)
    run(repo, "commit", "--allow-empty", "-m", "unrelated")
    with pytest.raises(RuntimeError, match="HEAD changed"):
        git.finish_pending_git(repo)
    assert git.pending_git_file(repo).exists()
    assert path.read_bytes() == b"keep"


def commit_files(repo, count=4):
    commits = []
    for index in range(count):
        path = repo / f"item{index}.bin"
        path.write_bytes(bytes([index]) * 1000)
        run(repo, "add", "--", path.name)
        run(repo, "commit", "-m", f"item {index}")
        commits.append(run(repo, "rev-parse", "HEAD").decode())
    return commits


def test_incremental_push_uses_measured_budget_and_verifies(repo, monkeypatch):
    commits = commit_files(repo)
    sizes = [git.object_bytes(repo, commit, commits[i-1] if i else "")
             for i, commit in enumerate(commits)]
    original = git._git
    pushed, verified = [], []

    def record(repo, *args, **kwargs):
        result = original(repo, *args, **kwargs)
        if "push" in args:
            pushed.append(args[-1].split(":")[0])
        if "ls-remote" in args and result.strip():
            verified.append(result.split()[0].decode())
        return result

    monkeypatch.setattr(git, "_git", record)
    git.push_unpublished(repo, "main", budget=max(sizes))
    assert pushed == commits
    assert all(commit in verified for commit in commits)
    git.push_unpublished(repo, "main", budget=max(sizes))
    assert pushed == commits


def test_rejected_push_halves_checkpoint_and_resumes(repo, monkeypatch):
    commits = commit_files(repo)
    original = git._git
    attempted = []

    def reject_large(repo, *args, **kwargs):
        if "push" in args:
            attempted.append(args[-1].split(":")[0])
            if len(attempted) == 1:
                raise RuntimeError("remote rejected large pack")
        return original(repo, *args, **kwargs)

    monkeypatch.setattr(git, "_git", reject_large)
    git.push_unpublished(repo, "main")
    assert attempted == [commits[3], commits[1], commits[3]]
    assert git.remote_head(repo, "main") == commits[-1]


def test_push_failure_is_bounded_and_preserves_commits(repo, monkeypatch):
    commits = commit_files(repo)
    original = git._git
    attempted = []

    def reject(repo, *args, **kwargs):
        if "push" in args:
            attempted.append(args[-1].split(":")[0])
            raise RuntimeError("protected branch rejects push")
        return original(repo, *args, **kwargs)

    monkeypatch.setattr(git, "_git", reject)
    with pytest.raises(RuntimeError, match="after 4 attempts.*protected branch"):
        git.push_unpublished(repo, "main")
    assert attempted == [commits[3], commits[1], commits[0], commits[0]]
    assert run(repo, "rev-parse", "HEAD").decode() == commits[-1]
    monkeypatch.setattr(git, "_git", original)
    git.push_unpublished(repo, "main")
    assert git.remote_head(repo, "main") == commits[-1]


def test_lost_push_response_is_reconciled(repo, monkeypatch):
    commits = commit_files(repo, 1)
    original = git._git
    calls = []

    def lose_response(repo, *args, **kwargs):
        result = original(repo, *args, **kwargs)
        if "push" in args:
            calls.append(args)
            raise RuntimeError("connection lost after acceptance")
        return result

    monkeypatch.setattr(git, "_git", lose_response)
    git.push_unpublished(repo, "main")
    assert len(calls) == 1
    assert git.remote_head(repo, "main") == commits[0]


def test_false_push_success_is_not_accepted(repo, monkeypatch):
    commit_files(repo, 1)
    original = git._git

    def no_op(repo, *args, **kwargs):
        return b"" if "push" in args else original(repo, *args, **kwargs)

    monkeypatch.setattr(git, "_git", no_op)
    with pytest.raises(RuntimeError, match="verification"):
        git.push_unpublished(repo, "main", max_attempts=2)
    assert git.remote_head(repo, "main") == ""


def test_single_commit_over_budget_is_not_pushed(repo):
    commit_files(repo, 1)
    with pytest.raises(RuntimeError, match="history was not rewritten"):
        git.push_unpublished(repo, "main", budget=100)
    assert git.remote_head(repo, "main") == ""


def test_diverged_remote_preserves_both_histories(repo):
    commits = commit_files(repo, 1)
    git.push_unpublished(repo, "main")
    run(repo, "checkout", "--orphan", "different")
    run(repo, "commit", "-m", "different root")
    run(repo, "branch", "-M", "main")
    with pytest.raises(RuntimeError, match="diverged"):
        git.push_unpublished(repo, "main")
    assert git.remote_head(repo, "main") == commits[0]


def test_oversized_file_rejected_before_staging(repo):
    path = repo / "too-large.bin"
    with path.open("wb") as stream:
        stream.truncate(100 * 1024**2 + 1)
    with pytest.raises(ValueError, match="100 MiB"):
        git.stage_commit_push(repo, [path], "large", "main")
    assert run(repo, "ls-files") == b""
    assert not git.pending_git_file(repo).exists()


def test_interrupted_copy_resumes_without_overwriting_or_losing_sources(repo, monkeypatch):
    import mover_git.transfer as transfer

    source = repo.parent / "source.bin"
    source.write_bytes(b"precious" * 1024)
    destination = repo / "folder [x]" / "file é.bin"
    journal = transfer.prepare_transfer(repo, [(source, destination)], "batch", "main")
    original = transfer.shutil.copyfileobj

    def interrupt(incoming, outgoing, length):
        outgoing.write(incoming.read(10))
        raise OSError("drive disconnected")

    monkeypatch.setattr(transfer.shutil, "copyfileobj", interrupt)
    with pytest.raises(OSError, match="disconnected"):
        transfer.finish_transfer(repo, journal)
    assert source.exists()
    assert not destination.exists()
    monkeypatch.setattr(transfer.shutil, "copyfileobj", original)
    assert transfer.resume_transfers(repo, "main")
    assert destination.read_bytes() == b"precious" * 1024
    assert not source.exists()
    assert run(repo, "rev-list", "--count", "HEAD") == b"1"
    assert not transfer.resume_transfers(repo, "main")


@pytest.mark.parametrize("failure_point", ["before_commit", "after_commit", "after_push"])
def test_transfer_resume_retains_unrelated_index(repo, monkeypatch, failure_point):
    import mover_git.transfer as transfer

    unrelated = repo / "unrelated.txt"
    unrelated.write_text("staged version")
    run(repo, "add", "unrelated.txt")
    unrelated.write_text("working version")
    staged_before = run(repo, "ls-files", "--stage", "unrelated.txt")
    source = repo.parent / "source.txt"
    source.write_text("precious")
    destination = repo / "destination.txt"
    journal = transfer.prepare_transfer(repo, [(source, destination)], "batch", "main")
    original = transfer.stage_commit_push
    original_push = transfer.push_unpublished

    def stop_commit(*args, **kwargs):
        if failure_point == "after_commit":
            original(*args, **kwargs)
        raise RuntimeError("interrupted")

    def stop_push(*args, **kwargs):
        original_push(*args, **kwargs)
        raise RuntimeError("interrupted")

    if failure_point == "after_push":
        monkeypatch.setattr(transfer, "push_unpublished", stop_push)
    else:
        monkeypatch.setattr(transfer, "stage_commit_push", stop_commit)
    with pytest.raises(RuntimeError, match="interrupted"):
        transfer.finish_transfer(repo, journal)
    assert source.read_text() == "precious"
    monkeypatch.setattr(transfer, "stage_commit_push", original)
    monkeypatch.setattr(transfer, "push_unpublished", original_push)
    transfer.resume_transfers(repo, "main")
    assert run(repo, "rev-list", "--count", "HEAD") == b"1"
    assert run(repo, "ls-files", "--stage", "unrelated.txt") == staged_before
    assert unrelated.read_text() == "working version"
    assert run(repo, "ls-tree", "--name-only", "HEAD") == b"destination.txt"
    assert not source.exists()


def test_conflicting_destination_and_changed_source_are_preserved(repo):
    import mover_git.transfer as transfer

    source = repo.parent / "source.txt"
    source.write_text("original")
    destination = repo / "destination.txt"
    journal = transfer.prepare_transfer(repo, [(source, destination)], "batch", "main")
    destination.write_text("unrelated")
    with pytest.raises(RuntimeError, match="differs"):
        transfer.finish_transfer(repo, journal)
    assert source.read_text() == "original"
    assert destination.read_text() == "unrelated"
    destination.unlink()
    source.write_text("changed")
    with pytest.raises(RuntimeError, match="changed"):
        transfer.finish_transfer(repo, journal)
    assert source.read_text() == "changed"


def test_deferred_batches_resume_and_publish_without_duplicate_commits(repo):
    import mover_git.transfer as transfer

    sources = []
    for i in range(3):
        source = repo.parent / f"source{i}.txt"
        source.write_text(f"payload {i}")
        sources.append(source)
        journal = transfer.prepare_transfer(repo, [(source, repo / source.name)], "batch", "main")
        transfer.finish_transfer(repo, journal, push=False)
    assert all(source.exists() for source in sources)
    assert git.remote_head(repo, "main") == ""
    transfer.resume_transfers(repo, "main")
    assert not any(source.exists() for source in sources)
    assert run(repo, "rev-list", "--count", "HEAD") == b"3"
    assert git.remote_head(repo, "main") == run(repo, "rev-parse", "HEAD").decode()


def test_changed_pending_file_blocks_commit_recovery(repo, monkeypatch):
    path = repo / "file.txt"
    path.write_text("verified")
    original = git._git

    def fail_commit(repo, *args, **kwargs):
        if "commit" in args:
            raise RuntimeError("interrupted")
        return original(repo, *args, **kwargs)

    monkeypatch.setattr(git, "_git", fail_commit)
    with pytest.raises(RuntimeError, match="interrupted"):
        git.stage_commit_push(repo, [path], "batch", "main")
    path.write_text("changed")
    monkeypatch.setattr(git, "_git", original)
    with pytest.raises(RuntimeError, match="Pending file changed"):
        git.finish_pending_git(repo)
    assert git.pending_git_file(repo).exists()
    assert git.remote_head(repo, "main") == ""


def test_remote_hook_rejection_retains_source_and_can_resume(repo):
    import mover_git.transfer as transfer

    remote = repo.parent / "remote.git"
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'policy rejection' >&2\nexit 1\n", newline="\n")
    hook.chmod(0o755)
    source = repo.parent / "original.txt"
    source.write_text("precious")
    journal = transfer.prepare_transfer(repo, [(source, repo / "copy.txt")], "batch", "main")
    with pytest.raises(RuntimeError, match="after 4 attempts.*policy rejection"):
        transfer.finish_transfer(repo, journal)
    assert source.read_text() == "precious"
    assert journal.exists()
    assert run(repo, "rev-list", "--count", "HEAD") == b"1"
    hook.unlink()
    transfer.resume_transfers(repo, "main")
    assert not source.exists()
    assert run(repo, "rev-list", "--count", "HEAD") == b"1"


def test_cleanup_interruption_resumes_remaining_sources(repo, monkeypatch):
    from pathlib import Path

    import mover_git.transfer as transfer

    pairs = []
    for i in range(2):
        source = repo.parent / f"source{i}.txt"
        source.write_text(str(i))
        pairs.append((source, repo / source.name))
    journal = transfer.prepare_transfer(repo, pairs, "batch", "main")
    original = Path.unlink

    def fail_last(path, *args, **kwargs):
        if path == pairs[1][0]:
            raise OSError("interrupted cleanup")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_last)
    with pytest.raises(OSError, match="cleanup"):
        transfer.finish_transfer(repo, journal)
    assert not pairs[0][0].exists()
    assert pairs[1][0].exists()
    monkeypatch.setattr(Path, "unlink", original)
    transfer.resume_transfers(repo, "main")
    assert not pairs[1][0].exists()
    assert run(repo, "rev-list", "--count", "HEAD") == b"1"


def test_branch_switch_blocks_recovery(repo):
    import mover_git.transfer as transfer

    source = repo.parent / "source.txt"
    source.write_text("precious")
    journal = transfer.prepare_transfer(repo, [(source, repo / "copy.txt")], "batch", "main")
    run(repo, "checkout", "-b", "other")
    with pytest.raises(RuntimeError, match="Switch back"):
        transfer.finish_transfer(repo, journal)
    assert source.exists()
    assert not (repo / "copy.txt").exists()


def test_existing_staged_deletion_is_not_overwritten(repo):
    import mover_git.transfer as transfer

    commit_files(repo, 1)
    run(repo, "rm", "item0.bin")
    before = run(repo, "diff", "--cached")
    source = repo.parent / "source.bin"
    source.write_bytes(b"replacement")
    with pytest.raises(RuntimeError, match="tracked/staged"):
        transfer.prepare_transfer(repo, [(source, repo / "item0.bin")], "batch", "main")
    assert run(repo, "diff", "--cached") == before
    assert source.exists()


def test_merge_push_counts_side_branch_objects(repo):
    commits = commit_files(repo, 1)
    git.push_unpublished(repo, "main")
    run(repo, "checkout", "-b", "side")
    path = repo / "side.bin"
    path.write_bytes(b"s" * 8000)
    run(repo, "add", "side.bin")
    run(repo, "commit", "-m", "side")
    run(repo, "checkout", "main")
    run(repo, "commit", "--allow-empty", "-m", "main advance")
    run(repo, "merge", "--no-ff", "side", "-m", "merge")
    head = run(repo, "rev-parse", "HEAD").decode()
    assert git.object_bytes(repo, head, commits[0]) > 8000
    with pytest.raises(RuntimeError, match="exceeding"):
        git.push_unpublished(repo, "main", budget=4000)
    assert git.remote_head(repo, "main") != head
    git.push_unpublished(repo, "main")
    assert git.remote_head(repo, "main") == head


def test_interruption_immediately_after_commit_does_not_duplicate(repo, monkeypatch):
    path = repo / "selected.txt"
    path.write_text("precious")
    original = git._git

    def commit_then_interrupt(repo, *args, **kwargs):
        result = original(repo, *args, **kwargs)
        if "commit" in args:
            raise RuntimeError("process interrupted after commit")
        return result

    monkeypatch.setattr(git, "_git", commit_then_interrupt)
    with pytest.raises(RuntimeError, match="interrupted"):
        git.stage_commit_push(repo, [path], "batch", "main")
    assert git.pending_git_file(repo).exists()
    monkeypatch.setattr(git, "_git", original)
    git.finish_pending_git(repo)
    assert run(repo, "rev-list", "--count", "HEAD") == b"1"
    assert git.remote_head(repo, "main") == run(repo, "rev-parse", "HEAD").decode()
