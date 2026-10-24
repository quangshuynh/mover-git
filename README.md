<p align="center">
  <img src="docs/images/logo.png" alt="GitProfileLens logo" width="280" />
</p>

# Mover Git

[![CI](https://github.com/quangshuynh/mover-git/actions/workflows/ci.yml/badge.svg)](https://github.com/quangshuynh/mover-git/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Mover Git is a Python and Tkinter desktop utility that previews and moves files into an existing Git repository, then commits and pushes them in size-limited batches. It is designed for controlled archival workflows where file placement, hosting limits, and unrelated Git changes need to remain visible.

## Features

- previews source files, destination paths, skipped files, and batch assignments before moving anything
- skips symbolic links, unreadable files, and individual files larger than 100 MiB
- creates batches no larger than 200 MiB while preserving file order
- optionally organizes supported photos and videos by date and timestamp
- avoids destination filename collisions without overwriting existing files
- preserves unrelated staged, unstaged, and untracked changes
- stages only files moved by the current operation
- keeps the interface responsive and reports progress, status, and Git output

## Screenshot

<p align="center">
  <img src="./docs/images/screenshot.png"
       alt="Git Mover screenshot"
       width="900">
</p>

## Workflow

1. Select a source directory.
2. Select an existing local Git repository and an optional subfolder within it.
3. Configure batching, commit, and media organization options.
4. Scan and review the valid, skipped, and batch preview tabs.
5. Start the move and confirm the destructive operation.
6. Review move, commit, and push progress in the log.

Mover Git copies and hashes files first, then removes sources only after a verified push. Keep backups of important data.

## Safety

The application validates paths, nesting, repository state, symbolic links, size limits, and destination collisions. Git staging uses explicit repository-relative pathspecs for files moved by Mover Git. It does not run `git add .`, reset unrelated work, or clean the repository.

After interruption, retry Move + Git Push to resume journaled copies, commits, and unpublished history without duplicating commits. Conflicting or changed files require manual review.

## Media Organization

Supported extensions are `.jpg`, `.jpeg`, `.png`, `.heic`, `.mp4`, `.mov`, `.avi`, and `.aae`, matched without regard to case. Images use available EXIF capture dates and otherwise fall back to filesystem modification time. Other supported files use modification time.

Media can be placed in `YYYY-MM-DD` folders and renamed as `YYYY-MM-DD HH_MM_SS.ext`. Numeric suffixes are added when timestamps collide.

## Limits

| Limit | Value |
| --- | ---: |
| Maximum individual file | 100 MiB |
| Maximum Git batch | 200 MiB |

These are application limits, not guarantees that a Git hosting provider will accept a push. GitHub normally blocks files larger than 100 MiB and recommends Git LFS for large binary assets. Mover Git does not configure Git LFS. Pushes use a 1 GiB object budget, retry smaller commit steps up to four attempts, and verify remote refs. An existing commit exceeding the budget requires manual review; history is never rewritten automatically.

## Installation

Requirements are Python 3.10 or newer with Tkinter, Git on `PATH`, and an existing destination repository with an `origin` remote and configured authentication.

```bash
python -m venv .venv
python -m pip install -e .
```

Activate the environment first using `.venv\Scripts\Activate.ps1` on Windows or `source .venv/bin/activate` on macOS and Linux. OpenCV remains available as an optional dependency through `python -m pip install -e ".[video]"`.

## Usage

Run the actual application entry point:

```bash
python app.py
```

Select the checked-out branch to publish to `origin`. Commits always stay within 200 MiB. The batch checkbox selects pushing after each batch or after all batches are committed. Sources remain until publication is verified.

## Development

Install development dependencies and run the same checks used by CI:

```bash
python -m pip install -e ".[dev]"
ruff check .
pytest
```

Tests use temporary directories and pure helpers. They do not open Tkinter windows, access a network, use Git credentials, or push to a remote.

## Project Structure

```text
mover-git/
|-- .github/workflows/ci.yml
|-- mover_git/
|   |-- __init__.py
|   |-- core.py
|   |-- git.py
|   `-- media.py
|-- tests/
|   |-- test_core.py
|   |-- test_git.py
|   `-- test_media.py
|-- app.py
|-- pyproject.toml
|-- LICENSE
`-- README.md
```

`app.py` owns the Tkinter interface and background workflow. `mover_git.core` contains path validation, batching, sizes, and commit messages. `mover_git.media` contains media recognition, timestamps, and collision-safe destination naming. `mover_git.git` contains remote parsing and safe staging path helpers.

## Limitations

- no automatic Git LFS setup
- no rollback across completed file moves or commits
- no automatic conflict resolution or push retry
- no remote creation or credential management
- empty directories are not represented by Git unless they contain a tracked placeholder

## License

Released under the [MIT License](LICENSE).
