import subprocess

import pytest


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

