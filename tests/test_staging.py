import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import mover_git.git as git
from mover_git.core import FileEntry


def run(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo).rstrip(b"\r\n")


@pytest.fixture
def repo(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    run(remote, "init", "--bare")
    repo = tmp_path / "repo"
    repo.mkdir()
    run(repo, "init", "-b", "main")
    run(repo, "config", "user.name", "Test")
    run(repo, "config", "user.email", "test@example.invalid")
    run(repo, "config", "commit.gpgsign", "false")
    run(repo, "config", "core.longpaths", "true")
    run(repo, "remote", "add", "origin", str(remote))
    return repo


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
        calls.append((command, kwargs.get("input")))
        assert len(subprocess.list2cmdline(command)) < 2000
        return original(command, **kwargs)

    monkeypatch.setattr(git.subprocess, "run", record)
    git.check_staging_support(repo)
    git.stage_commit_push(repo, paths, "batch", "main")
    tracked = set(run(repo, "ls-tree", "-rz", "--name-only", "HEAD").split(b"\0")) - {b""}
    assert tracked == {name.encode() for name in names}
    for command, payload in calls:
        if "--pathspec-from-file=-" in command:
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
    moved = []
    original_move = shutil.move

    def record_move(src, dst):
        moved.append(src)
        return original_move(src, dst)

    monkeypatch.setattr(shutil, "move", record_move)
    app.move_and_push()
    assert len(moved) == (1 if batch_commits else 0)
    assert run(repo, "rev-list", "--count", "HEAD") == (b"2" if batch_commits else b"1")
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
