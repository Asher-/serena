"""
The writes of Serena's configuration file: a reader never sees part of the file, and a save keeps what another
process registered or removed since this one loaded it (bug://serena/config/file/write/atomicity).
"""

import os
import threading
from pathlib import Path
from typing import Any

import pytest

from serena.config import serena_config
from serena.config.serena_config import SerenaConfig, SerenaPaths
from serena.util import file_lifecycle
from serena.util.yaml import load_yaml, save_yaml


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "home"
    directory.mkdir()
    monkeypatch.setattr(SerenaPaths(), "serena_user_home_dir", str(directory))
    return directory


def _project(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "module.py").write_text("x = 1\n")
    return root


def _roots_on_file(home: Path) -> set[str]:
    return {str(Path(root).resolve()) for root in load_yaml(str(home / SerenaConfig.CONFIG_FILE))["projects"]}


def test_a_dump_that_raises_leaves_the_file_as_it_was(tmp_path: Path) -> None:
    target = tmp_path / "config.yml"
    target.write_text("projects: []\n")

    with pytest.raises(Exception, match="cannot represent"):
        save_yaml(str(target), {"unrepresentable": object()})

    assert target.read_text() == "projects: []\n"


def test_a_save_replaces_the_whole_file_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "config.yml"
    target.write_text("projects: []\n")
    seen: list[tuple[str, str]] = []
    replace = os.replace

    def spying_replace(source: Any, destination: Any) -> None:
        seen.append((target.read_text(), Path(source).read_text()))
        replace(source, destination)

    monkeypatch.setattr(file_lifecycle.os, "replace", spying_replace)

    save_yaml(str(target), {"projects": ["/a"]})

    # the file held its whole old text until the rename brought in the whole new text
    assert seen == [("projects: []\n", target.read_text())]
    assert "/a" in target.read_text()
    assert [path.name for path in tmp_path.iterdir()] == ["config.yml"]


def test_two_instances_that_each_register_a_project_leave_both_on_file(home: Path, tmp_path: Path) -> None:
    first = SerenaConfig.from_config_file()
    second = SerenaConfig.from_config_file()
    one, other = _project(tmp_path, "one"), _project(tmp_path, "other")

    first.add_project_from_path(one)
    second.add_project_from_path(other)

    assert _roots_on_file(home) == {str(one.resolve()), str(other.resolve())}


def test_a_removal_by_one_instance_is_not_undone_by_a_later_save_of_another(home: Path, tmp_path: Path) -> None:
    project = _project(tmp_path, "one")
    SerenaConfig.from_config_file().add_project_from_path(project)
    remover = SerenaConfig.from_config_file()
    bystander = SerenaConfig.from_config_file()

    remover.remove_project(remover.projects[0].project_name)
    bystander.save()

    assert _roots_on_file(home) == set()


def test_a_save_waits_for_a_save_that_read_the_file_and_has_not_replaced_it(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held, other_ended = threading.Event(), threading.Event()
    release = threading.Event()
    first = SerenaConfig.from_config_file()
    second = SerenaConfig.from_config_file()
    one, other = _project(tmp_path, "one"), _project(tmp_path, "other")
    save_yaml_unheld = serena_config.save_yaml
    claimed = threading.Lock()

    def holding_save_yaml(path: str, data: Any) -> None:
        if path == first.config_file_path and claimed.acquire(blocking=False):
            held.set()
            assert release.wait(60)
        save_yaml_unheld(path, data)

    monkeypatch.setattr(serena_config, "save_yaml", holding_save_yaml)

    def register_other() -> None:
        second.add_project_from_path(other)
        other_ended.set()

    registering_one = threading.Thread(target=first.add_project_from_path, args=(one,))
    registering_other = threading.Thread(target=register_other)
    registering_one.start()
    assert held.wait(60)
    registering_other.start()

    # with the read and the replace under one lock the second save cannot end here; without it, it ends and is then lost
    assert not other_ended.wait(2)
    release.set()
    registering_one.join(60)
    registering_other.join(60)

    assert _roots_on_file(home) == {str(one.resolve()), str(other.resolve())}


def test_a_configuration_with_no_file_saves_nothing(tmp_path: Path) -> None:
    SerenaConfig().save()

    assert list(tmp_path.iterdir()) == []


def test_a_save_writes_a_file_removed_since_it_was_read(home: Path, tmp_path: Path) -> None:
    project = _project(tmp_path, "one")
    config = SerenaConfig.from_config_file()
    (home / SerenaConfig.CONFIG_FILE).unlink()

    config.add_project_from_path(project)

    assert _roots_on_file(home) == {str(project.resolve())}


def test_a_save_over_a_file_that_lists_no_projects_keeps_what_this_instance_registered(home: Path, tmp_path: Path) -> None:
    project = _project(tmp_path, "one")
    config = SerenaConfig.from_config_file()
    (home / SerenaConfig.CONFIG_FILE).write_text("{}\n")

    config.add_project_from_path(project)

    assert _roots_on_file(home) == {str(project.resolve())}


def test_a_save_drops_a_listed_root_whose_project_is_gone(home: Path, tmp_path: Path) -> None:
    gone = tmp_path / "gone"
    config = SerenaConfig.from_config_file()
    config.save()
    path = home / SerenaConfig.CONFIG_FILE
    data = load_yaml(str(path))
    data["projects"] = [str(gone)]
    save_yaml(str(path), data)

    SerenaConfig.from_config_file().add_project_from_path(_project(tmp_path, "kept"))

    assert _roots_on_file(home) == {str((tmp_path / "kept").resolve())}


def test_a_save_refuses_a_file_it_cannot_read_and_leaves_it_as_it_was(home: Path) -> None:
    config = SerenaConfig.from_config_file()
    path = home / SerenaConfig.CONFIG_FILE
    path.write_text("projects: [unterminated\n")

    with pytest.raises(Exception, match="(?i)flow sequence|expected"):
        config.save()

    assert path.read_text() == "projects: [unterminated\n"
