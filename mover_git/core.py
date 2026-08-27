from dataclasses import dataclass
from pathlib import Path

MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024
MAX_BATCH_SIZE_BYTES = 2 * 1024 * 1024 * 1024


@dataclass(frozen=True)
class FileEntry:
    """File selected for a move operation"""

    src_path: Path
    rel_path: Path
    size: int


def human_size(size_bytes: int) -> str:
    """
    convert a byte count into a readable size
    :param size_bytes: number of bytes to format
    :returns: readable size with a binary unit
    """
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size_bytes)
    for unit in units:
        if abs(size) < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size_bytes} B"


def make_batches(
    files: list[FileEntry], limit: int = MAX_BATCH_SIZE_BYTES
) -> list[list[FileEntry]]:
    """
    divide files into ordered batches within a size limit
    :param files: files to organize into batches
    :param limit: maximum number of bytes in each batch
    :returns: ordered batches of files
    """
    batches: list[list[FileEntry]] = []
    current: list[FileEntry] = []
    current_size = 0
    for entry in files:
        if entry.size > limit:
            continue
        if current and current_size + entry.size > limit:
            batches.append(current)
            current = []
            current_size = 0
        current.append(entry)
        current_size += entry.size
    if current:
        batches.append(current)
    return batches


def validate_paths(source: Path, repo: Path, subfolder: str = "") -> tuple[Path, Path, Path]:
    """
    validate source repository and target paths
    :param source: source directory to move from
    :param repo: destination Git repository
    :param subfolder: relative destination within the repository
    :returns: resolved source repository and target paths
    """
    if not source.is_dir():
        raise ValueError("please choose a valid source folder")
    validate_git_repository(repo)
    source = source.resolve()
    repo = repo.resolve()
    target = (repo / subfolder).resolve()
    if not target.is_relative_to(repo):
        raise ValueError("subfolder must stay inside the destination repository")
    if source.is_relative_to(target):
        raise ValueError("source folder cannot be inside the destination target")
    if target.is_relative_to(source):
        raise ValueError("destination target cannot be inside the source folder")
    return source, repo, target


def validate_git_repository(repo: Path) -> None:
    """
    validate that a destination path is an accessible Git repository
    :param repo: destination path to validate
    """
    try:
        repo_exists = repo.exists()
    except OSError as exc:
        raise ValueError(f"destination folder cannot be accessed: {exc.strerror or exc}") from exc
    if not repo_exists:
        raise ValueError("destination folder does not exist")

    try:
        repo_is_dir = repo.is_dir()
    except OSError as exc:
        raise ValueError(f"destination folder cannot be accessed: {exc.strerror or exc}") from exc
    if not repo_is_dir:
        raise ValueError("destination path is not a folder")

    git_entry = repo / ".git"
    try:
        git_exists = git_entry.exists()
    except OSError as exc:
        raise ValueError(f"destination repository cannot be accessed: {exc.strerror or exc}") from exc
    if not git_exists:
        raise ValueError("destination folder must be a Git repository")

    if git_entry.is_file():
        _validate_git_worktree_pointer(git_entry)
    elif not (git_entry / "HEAD").exists():
        raise ValueError("destination Git repository information could not be determined")


def _validate_git_worktree_pointer(git_file: Path) -> None:
    """
    validate a .git worktree/submodule pointer file resolves to a real Git directory
    :param git_file: .git file containing a gitdir pointer
    """
    try:
        contents = git_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"destination repository cannot be accessed: {exc.strerror or exc}") from exc
    if not contents.startswith("gitdir:"):
        raise ValueError("destination Git repository information could not be determined")
    gitdir = Path(contents.removeprefix("gitdir:").strip())
    if not gitdir.is_absolute():
        gitdir = git_file.parent / gitdir
    if not gitdir.is_dir():
        raise ValueError("destination Git repository information could not be determined")


def make_commit_message(
    prefix: str,
    batch_index: int,
    total_batches: int,
    file_count: int,
    moved_bytes: int,
    timestamp: str,
) -> str:
    """
    create a commit message for a completed batch
    :param prefix: optional commit message prefix
    :param batch_index: current batch number
    :param total_batches: total number of batches
    :param file_count: number of files moved
    :param moved_bytes: number of bytes moved
    :param timestamp: formatted commit timestamp
    :returns: formatted commit message
    """
    base = prefix.strip() or "update"
    suffix = f" {batch_index}" if total_batches > 1 and batch_index > 1 else ""
    return f"{timestamp} {base}{suffix} - {file_count} files - {human_size(moved_bytes)}"
