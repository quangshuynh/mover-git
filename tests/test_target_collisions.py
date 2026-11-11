from datetime import datetime
from pathlib import Path

import pytest

from mover_git.media import build_target_path
from mover_git.storage import DiskSet

TAKEN = datetime(2026, 8, 22, 9, 5, 3)


@pytest.fixture(params=["set", "disk"])
def used(request):
    """
    provide both kinds of used path sets the app passes to build_target_path
    :param request: pytest request selecting the set kind
    :returns: an empty used path set
    """
    if request.param == "set":
        yield set()
    else:
        with DiskSet() as paths:
            yield paths


def plan(destination: Path, sources: list[str], used, *, date_folder: bool, rename: bool) -> list[Path]:
    """
    build target paths for media sources in order, as the preview does
    :param destination: destination root directory
    :param sources: source paths relative to the source folder
    :param used: used path set shared by the whole plan
    :param date_folder: whether to create a date folder
    :param rename: whether to rename files to their timestamp
    :returns: target path for each source
    """
    return [
        build_target_path(destination, Path(source), Path(source), used,
                          True, True, date_folder, rename, TAKEN)
        for source in sources
    ]


def test_two_sources_with_the_same_name_get_separate_targets(tmp_path: Path, used) -> None:
    """
    verify files from different folders that land in one date folder keep both names
    :param tmp_path: temporary destination directory
    :param used: used path set
        :returns: nothing
    """
    result = plan(tmp_path, ["a/photo.jpg", "b/photo.jpg"], used, date_folder=True, rename=False)
    assert result == [tmp_path / "2026-08-22" / "photo.jpg", tmp_path / "2026-08-22" / "photo_1.jpg"]


def test_same_timestamp_names_get_numbered(tmp_path: Path, used) -> None:
    """
    verify media taken in the same second is numbered instead of sharing one name
    :param tmp_path: temporary destination directory
    :param used: used path set
        :returns: nothing
    """
    result = plan(tmp_path, ["a.jpg", "b.jpg", "c.jpg"], used, date_folder=False, rename=True)
    assert [path.name for path in result] == [
        "2026-08-22 09_05_03.jpg", "2026-08-22 09_05_03_1.jpg", "2026-08-22 09_05_03_2.jpg",
    ]


def test_existing_destination_file_is_skipped_and_kept(tmp_path: Path, used) -> None:
    """
    verify a timestamp name already on disk is not reused or changed
    :param tmp_path: temporary destination directory
    :param used: used path set
        :returns: nothing
    """
    existing = tmp_path / "2026-08-22" / "2026-08-22 09_05_03.jpg"
    existing.parent.mkdir()
    existing.write_bytes(b"existing")
    result = plan(tmp_path, ["a.jpg", "b.jpg"], used, date_folder=True, rename=True)
    assert result == [existing.with_name("2026-08-22 09_05_03_1.jpg"),
                      existing.with_name("2026-08-22 09_05_03_2.jpg")]
    assert existing.read_bytes() == b"existing"


def test_planned_targets_stay_inside_destination_and_overwrite_nothing(tmp_path: Path, used) -> None:
    """
    verify every planned target is unique, inside the destination, and free on disk
    :param tmp_path: temporary destination directory
    :param used: used path set
        :returns: nothing
    """
    destination = tmp_path / "repo"
    (destination / "2026-08-22").mkdir(parents=True)
    (destination / "2026-08-22" / "photo.jpg").write_bytes(b"existing")
    sources = ["photo.jpg", "a/photo.jpg", "a/b/photo.jpg", "photo_1.jpg"]
    result = plan(destination, sources, used, date_folder=True, rename=False)
    assert len(set(result)) == len(sources)
    for index, target in enumerate(result):
        assert target.resolve().is_relative_to(destination.resolve())
        # "x" fails if the file is there, so writing every target proves none is overwritten.
        with target.open("xb") as handle:
            handle.write(str(index).encode())
    assert (destination / "2026-08-22" / "photo.jpg").read_bytes() == b"existing"


def test_collision_resolution_is_deterministic(tmp_path: Path) -> None:
    """
    verify the same sources in the same order always get the same targets
    :param tmp_path: temporary destination directory
        :returns: nothing
    """
    sources = ["a/photo.jpg", "b/photo.jpg", "c.jpg"]
    first = plan(tmp_path, sources, set(), date_folder=True, rename=False)
    with DiskSet() as used:
        second = plan(tmp_path, sources, used, date_folder=True, rename=False)
    assert first == second
