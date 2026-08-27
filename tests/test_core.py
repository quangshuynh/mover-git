import os
import sys
from pathlib import Path

import pytest

from mover_git.core import (
    FileEntry,
    human_size,
    make_batches,
    make_commit_message,
    validate_git_repository,
    validate_paths,
)

skip_as_root = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="permission checks are unreliable on Windows or when running as root",
)


def make_repo(path: Path) -> Path:
    """
    create a minimal valid Git repository fixture
    :param path: repository directory to initialize
    :returns: the repository path
    """
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return path


def entry(name: str, size: int) -> FileEntry:
    """
    create a file entry for deterministic tests
    :param name: file name for the entry
    :param size: file size in bytes
    :returns: constructed file entry
    """
    path = Path(name)
    return FileEntry(path, path, size)


def test_make_batches_honors_limit_and_order() -> None:
    """
    verify batches stay within the configured limit
        :returns: nothing
    """
    batches = make_batches([entry("a", 6), entry("b", 4), entry("c", 1)], limit=10)
    assert [[item.rel_path.name for item in batch] for batch in batches] == [["a", "b"], ["c"]]


def test_make_batches_skips_individual_file_above_limit() -> None:
    """
    verify files larger than a batch are excluded
        :returns: nothing
    """
    assert make_batches([entry("large", 11), entry("small", 2)], limit=10) == [[entry("small", 2)]]


@pytest.mark.parametrize(
    ("size", "formatted"),
    [(0, "0.00 B"), (1024, "1.00 KB"), (1024**2 * 2, "2.00 MB")],
)
def test_human_size(size: int, formatted: str) -> None:
    """
    verify byte counts use binary units
    :param size: byte count to format
    :param formatted: expected display value
        :returns: nothing
    """
    assert human_size(size) == formatted


def test_validate_paths_accepts_repository_subfolder(tmp_path: Path) -> None:
    """
    verify a separate source and repository target are accepted
    :param tmp_path: temporary test directory
        :returns: nothing
    """
    source = tmp_path / "source"
    repo = make_repo(tmp_path / "repo")
    source.mkdir()
    assert validate_paths(source, repo, "uploads") == (
        source.resolve(), repo.resolve(), (repo / "uploads").resolve()
    )


def test_validate_paths_rejects_nested_source(tmp_path: Path) -> None:
    """
    verify a source inside the destination is rejected
    :param tmp_path: temporary test directory
        :returns: nothing
    """
    repo = make_repo(tmp_path / "repo")
    source = repo / "source"
    source.mkdir()
    with pytest.raises(ValueError, match="source folder"):
        validate_paths(source, repo)


def test_validate_git_repository_rejects_missing_destination(tmp_path: Path) -> None:
    """
    verify a destination that does not exist is rejected with a specific message
    :param tmp_path: temporary test directory
        :returns: nothing
    """
    with pytest.raises(ValueError, match="does not exist"):
        validate_git_repository(tmp_path / "missing")


def test_validate_git_repository_rejects_file_destination(tmp_path: Path) -> None:
    """
    verify a destination that is a file rather than a folder is rejected
    :param tmp_path: temporary test directory
        :returns: nothing
    """
    destination = tmp_path / "not_a_folder"
    destination.write_text("nope")
    with pytest.raises(ValueError, match="not a folder"):
        validate_git_repository(destination)


def test_validate_git_repository_rejects_non_git_folder(tmp_path: Path) -> None:
    """
    verify a plain folder without a .git entry is rejected
    :param tmp_path: temporary test directory
        :returns: nothing
    """
    destination = tmp_path / "plain"
    destination.mkdir()
    with pytest.raises(ValueError, match="must be a Git repository"):
        validate_git_repository(destination)


def test_validate_git_repository_rejects_git_dir_without_head(tmp_path: Path) -> None:
    """
    verify a .git directory missing required repository information is rejected
    :param tmp_path: temporary test directory
        :returns: nothing
    """
    destination = tmp_path / "broken"
    (destination / ".git").mkdir(parents=True)
    with pytest.raises(ValueError, match="could not be determined"):
        validate_git_repository(destination)


def test_validate_git_repository_accepts_valid_worktree_pointer(tmp_path: Path) -> None:
    """
    verify a .git file pointing at a real Git directory is accepted
    :param tmp_path: temporary test directory
        :returns: nothing
    """
    gitdir = tmp_path / "main_repo" / ".git" / "worktrees" / "feature"
    gitdir.mkdir(parents=True)
    destination = tmp_path / "feature_worktree"
    destination.mkdir()
    (destination / ".git").write_text(f"gitdir: {gitdir}\n")
    validate_git_repository(destination)


def test_validate_git_repository_rejects_broken_worktree_pointer(tmp_path: Path) -> None:
    """
    verify a .git file pointing at a missing Git directory is rejected
    :param tmp_path: temporary test directory
        :returns: nothing
    """
    destination = tmp_path / "feature_worktree"
    destination.mkdir()
    (destination / ".git").write_text(f"gitdir: {tmp_path / 'nonexistent'}\n")
    with pytest.raises(ValueError, match="could not be determined"):
        validate_git_repository(destination)


def test_validate_git_repository_rejects_malformed_git_file(tmp_path: Path) -> None:
    """
    verify a .git file without a gitdir pointer is rejected
    :param tmp_path: temporary test directory
        :returns: nothing
    """
    destination = tmp_path / "malformed"
    destination.mkdir()
    (destination / ".git").write_text("not a pointer file")
    with pytest.raises(ValueError, match="could not be determined"):
        validate_git_repository(destination)


@skip_as_root
def test_validate_git_repository_reports_inaccessible_destination(tmp_path: Path) -> None:
    """
    verify a destination that cannot be read raises an access-specific message
    :param tmp_path: temporary test directory
        :returns: nothing
    """
    parent = tmp_path / "locked"
    parent.mkdir()
    destination = parent / "repo"
    destination.mkdir()
    os.chmod(parent, 0o000)
    try:
        with pytest.raises(ValueError, match="cannot be accessed"):
            validate_git_repository(destination)
    finally:
        os.chmod(parent, 0o755)


def test_make_commit_message_adds_later_batch_number() -> None:
    """
    verify commit messages include details and later batch numbers
        :returns: nothing
    """
    result = make_commit_message("archive", 2, 3, 4, 1024, "[01][02][2026] [03:04:05]")
    assert result == "[01][02][2026] [03:04:05] archive 2 - 4 files - 1.00 KB"
