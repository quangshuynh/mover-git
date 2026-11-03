"""Interruption, divergence, and manifest-integrity recovery in isolated repositories."""

import json

import pytest
from test_operations import create_operation
from test_staging import run

import mover_git.git as git
import mover_git.transfer as transfer


class Crash(BaseException):
    pass


def partials(repo):
    return sorted(path.name for path in repo.rglob(".*.partial"))


@pytest.mark.skipif(transfer.os.name == "nt", reason="POSIX hard-link publication window")
def test_crash_between_link_and_temp_unlink_leaves_no_orphan(repo, monkeypatch):
    operation, pairs = create_operation(repo, count=3)
    original = transfer.os.link

    def link(source, destination):
        original(source, destination)
        raise Crash()

    monkeypatch.setattr(transfer.os, "link", link)
    with pytest.raises(Crash):
        operation.run()
    operation.close()
    assert partials(repo)
    monkeypatch.setattr(transfer.os, "link", original)
    reopened = transfer.BatchOperation(repo)
    reopened.run()
    assert reopened.state["status"] == "Complete"
    assert partials(repo) == []
    assert run(repo, "status", "--porcelain") == b""
    assert run(repo, "rev-list", "--count", "HEAD") == b"2"
    assert all(not s.exists() and d.read_text() == f"payload {i}"
               for i, (s, d) in enumerate(pairs))
    reopened.close()


def test_temp_with_other_content_is_not_mistaken_for_published_copy(repo, monkeypatch):
    operation, pairs = create_operation(repo, count=2)
    original = transfer.shutil.copyfileobj
    calls = []

    def copy(incoming, outgoing, length):
        calls.append(1)
        if len(calls) == 2:
            outgoing.write(b"part")
            raise Crash()
        original(incoming, outgoing, length)

    monkeypatch.setattr(transfer.shutil, "copyfileobj", copy)
    with pytest.raises(Crash):
        operation.run()
    operation.close()
    monkeypatch.setattr(transfer.shutil, "copyfileobj", original)
    reopened = transfer.BatchOperation(repo)
    reopened.run()
    assert reopened.state["status"] == "Complete"
    assert partials(repo) == []
    assert [d.read_text() for _, d in pairs] == ["payload 0", "payload 1"]
    reopened.close()


def test_cancel_cleanup_removes_owned_partial_copy_and_retire_succeeds(repo, monkeypatch):
    operation, pairs = create_operation(repo, count=4)
    original = transfer.shutil.copyfileobj
    calls = []

    def copy(incoming, outgoing, length):
        calls.append(1)
        if len(calls) == 2:
            outgoing.write(b"part")
            operation.request_cancel("transfer")
            raise Crash()
        original(incoming, outgoing, length)

    monkeypatch.setattr(transfer.shutil, "copyfileobj", copy)
    with pytest.raises(Crash):
        operation.run()
    operation.close()
    monkeypatch.setattr(transfer.shutil, "copyfileobj", original)
    operation = transfer.BatchOperation(repo)
    operation.run()
    assert operation.state["status"] == "Cleanup Available"
    assert partials(repo)
    messages = []
    operation.cleanup(messages.append)
    assert partials(repo) == []
    assert any("temporary" in message for message in messages)
    assert all(s.exists() and not d.exists() for s, d in pairs)
    operation.retire()
    assert run(repo, "status", "--porcelain") == b""


def test_cleanup_preserves_partial_when_source_changed(repo, monkeypatch):
    operation, pairs = create_operation(repo, count=2)
    original = transfer.shutil.copyfileobj

    def copy(incoming, outgoing, length):
        outgoing.write(b"part")
        operation.request_cancel("transfer")
        raise Crash()

    monkeypatch.setattr(transfer.shutil, "copyfileobj", copy)
    with pytest.raises(Crash):
        operation.run()
    operation.close()
    monkeypatch.setattr(transfer.shutil, "copyfileobj", original)
    pairs[0][0].write_text("edited after planning")
    operation = transfer.BatchOperation(repo)
    operation.run()
    messages = []
    operation.cleanup(messages.append)
    assert partials(repo)
    assert any("Preserved" in message for message in messages)
    with pytest.raises(RuntimeError, match="temporary"):
        operation.retire()
    operation.close()


def published_baseline(repo):
    (repo / "base.txt").write_text("base")
    run(repo, "add", "base.txt")
    run(repo, "commit", "-m", "base")
    run(repo, "push", "origin", "main")


@pytest.mark.parametrize("change", ["rewound", "replaced"])
def test_remote_rewind_or_replacement_blocks_resume_without_push(repo, change):
    published_baseline(repo)
    operation, pairs = create_operation(repo, count=4)
    operation.run(progress=lambda _: operation.pause.set())
    assert operation.state["batch"] == 1
    operation.close()
    pushed = git.remote_head(repo, "main")
    if change == "rewound":
        run(repo, "push", "--force", "origin", "HEAD~1:refs/heads/main")
    else:
        other = repo.parent / "other"
        run(repo.parent, "clone", "-q", "-b", "main", str(repo.parent / "remote.git"), str(other))
        run(other, "reset", "-q", "--hard", "HEAD~1")
        run(other, "-c", "user.name=T", "-c", "user.email=t@example.invalid",
            "commit", "-q", "--allow-empty", "-m", "external")
        run(other, "push", "-q", "--force", "origin", "HEAD:refs/heads/main")
    external = git.remote_head(repo, "main")
    assert external != pushed
    reopened = transfer.BatchOperation(repo)
    with pytest.raises(RuntimeError, match="(?i)remote.*(rewound|rewritten|diverged)"):
        reopened.run()
    assert git.remote_head(repo, "main") == external
    assert all(s.exists() and not d.exists() for s, d in pairs[2:])
    reopened.close()


def eol_attributes(repo):
    (repo / ".gitattributes").write_text("target* text eol=lf\n")
    run(repo, "add", ".gitattributes")
    run(repo, "commit", "-m", "attributes")
    run(repo, "push", "origin", "main")


def test_content_altering_git_attributes_are_rejected_before_commit(repo):
    eol_attributes(repo)
    source = repo.parent / "crlf.txt"
    source.write_bytes(b"line one\r\nline two\r\n")
    operation = transfer.BatchOperation.create(repo, [(source, repo / "target.txt")], "main")
    head = transfer.local_head(repo)
    with pytest.raises(RuntimeError, match="(?i)attributes or filters.*target.txt"):
        operation.run()
    assert transfer.local_head(repo) == head
    assert git.remote_head(repo, "main") == head
    assert source.read_bytes() == b"line one\r\nline two\r\n"
    assert run(repo, "diff", "--cached", "--name-only") == b""
    operation.close()


def test_committed_blob_mismatch_is_rejected_before_push(repo, monkeypatch):
    """Defense in depth: a commit already made (crash window, older release) is re-verified."""
    eol_attributes(repo)
    source = repo.parent / "crlf.txt"
    source.write_bytes(b"a\r\nb\r\n")
    journal = transfer.prepare_transfer(repo, [(source, repo / "target.txt")], "batch", "main")
    monkeypatch.setattr(transfer, "check_content_filters", lambda *args, **kwargs: None)
    transfer.finish_transfer(repo, journal, push=False)
    assert json.loads(journal.read_text())["committed"]
    remote = git.remote_head(repo, "main")
    with pytest.raises(RuntimeError, match="(?i)committed content.*manifest.*target.txt"):
        transfer.finish_transfer(repo, journal)
    assert git.remote_head(repo, "main") == remote
    assert source.read_bytes() == b"a\r\nb\r\n"
    assert journal.exists()


def test_committed_blobs_verified_against_manifest_hashes(repo, monkeypatch):
    operation, pairs = create_operation(repo, count=3, maximum=None)
    verified = []
    original = transfer.verify_committed

    def spy(repository, commit, state, journal, checkpoint=lambda: None):
        original(repository, commit, state, journal, checkpoint)
        verified.extend(record["path"] for record in transfer.records(state, journal))

    monkeypatch.setattr(transfer, "verify_committed", spy)
    operation.run()
    assert operation.state["status"] == "Complete"
    assert sorted(verified) == ["target0", "target1", "target2"]
    operation.close()


def test_cleanup_identity_ignores_access_time_from_hashing(tmp_path):
    path = tmp_path / "copy"
    path.write_bytes(b"payload")
    stat = path.stat()
    transfer.os.utime(path, ns=(0, stat.st_mtime_ns))
    before = path.lstat()
    transfer.digest(path)
    after = path.lstat()
    if after.st_atime_ns == before.st_atime_ns:
        pytest.skip("filesystem does not update access times")
    assert before != after  # the previous whole-stat comparison saw a "change"
    assert transfer.stable_identity(before) == transfer.stable_identity(after)
    path.write_bytes(b"changed")
    assert transfer.stable_identity(path.lstat()) != transfer.stable_identity(after)


@pytest.mark.parametrize("autocrlf", ["true", "input"])
def test_autocrlf_config_does_not_alter_committed_bytes(repo, autocrlf):
    run(repo, "config", "core.autocrlf", autocrlf)
    payload = b"line one\r\nline two\r\n"
    source = repo.parent / "crlf.txt"
    source.write_bytes(payload)
    operation = transfer.BatchOperation.create(repo, [(source, repo / "notes.txt")], "main")
    operation.run()
    assert operation.state["status"] == "Complete"
    stored = transfer.subprocess.check_output(["git", "cat-file", "blob", "HEAD:notes.txt"], cwd=repo)
    assert stored == payload
    assert run(repo, "status", "--porcelain") == b""
    assert (repo / "notes.txt").read_bytes() == payload
    assert not source.exists()
    operation.close()
