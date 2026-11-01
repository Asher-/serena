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


@pytest.fixture(autouse=True)
def home(work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = work_dir / "home"
    directory.mkdir()
    monkeypatch.setattr(SerenaPaths(), "serena_user_home_dir", str(directory))
    return directory


def _project(work_dir: Path, name: str) -> Path:
    root = work_dir / name
    root.mkdir()
    (root / "module.py").write_text("x = 1\n")
    return root


def _roots_on_file(home: Path) -> set[str]:
    return {str(Path(root).resolve()) for root in load_yaml(str(home / SerenaConfig.CONFIG_FILE))["projects"]}


def test_a_dump_that_raises_leaves_the_file_as_it_was(work_dir: Path) -> None:
    target = work_dir / "config.yml"
    target.write_text("projects: []\n")

    with pytest.raises(Exception, match="cannot represent"):
        save_yaml(str(target), {"unrepresentable": object()})

    assert target.read_text() == "projects: []\n"


def test_a_save_creates_the_file_and_its_directory(work_dir: Path) -> None:
    target = work_dir / "absent" / "config.yml"

    save_yaml(str(target), {"projects": []})

    assert load_yaml(str(target))["projects"] == []


def test_a_save_replaces_the_whole_file_at_once(work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = work_dir / "files" / "config.yml"
    target.parent.mkdir()
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
    assert [path.name for path in target.parent.iterdir()] == ["config.yml"]


def test_two_instances_that_each_register_a_project_leave_both_on_file(home: Path, work_dir: Path) -> None:
    first = SerenaConfig.from_config_file()
    second = SerenaConfig.from_config_file()
    one, other = _project(work_dir, "one"), _project(work_dir, "other")

    first.add_project_from_path(one)
    second.add_project_from_path(other)

    assert _roots_on_file(home) == {str(one.resolve()), str(other.resolve())}


def test_a_removal_by_one_instance_is_not_undone_by_a_later_save_of_another(home: Path, work_dir: Path) -> None:
    project = _project(work_dir, "one")
    SerenaConfig.from_config_file().add_project_from_path(project)
    remover = SerenaConfig.from_config_file()
    bystander = SerenaConfig.from_config_file()

    remover.remove_project(remover.projects[0].project_name)
    bystander.save()

    assert _roots_on_file(home) == set()


def test_a_second_save_keeps_what_another_instance_registered_before_the_first(home: Path, work_dir: Path) -> None:
    first = SerenaConfig.from_config_file()
    second = SerenaConfig.from_config_file()
    one, other, third = _project(work_dir, "one"), _project(work_dir, "other"), _project(work_dir, "third")

    first.add_project_from_path(one)
    second.add_project_from_path(other)
    second.add_project_from_path(third)

    assert _roots_on_file(home) == {str(one.resolve()), str(other.resolve()), str(third.resolve())}


def test_a_second_save_does_not_bring_back_what_another_instance_removed(home: Path, work_dir: Path) -> None:
    gone, one, other = _project(work_dir, "gone"), _project(work_dir, "one"), _project(work_dir, "other")
    SerenaConfig.from_config_file().add_project_from_path(gone)
    remover = SerenaConfig.from_config_file()
    bystander = SerenaConfig.from_config_file()

    remover.remove_project(remover.projects[0].project_name)
    bystander.add_project_from_path(one)
    bystander.add_project_from_path(other)

    assert _roots_on_file(home) == {str(one.resolve()), str(other.resolve())}


def test_a_second_save_applies_this_instances_own_removal(home: Path, work_dir: Path) -> None:
    config = SerenaConfig.from_config_file()
    one = _project(work_dir, "one")

    config.add_project_from_path(one)
    config.remove_project(config.projects[0].project_name)

    assert _roots_on_file(home) == set()


def test_a_save_waits_for_a_save_that_read_the_file_and_has_not_replaced_it(
    home: Path, work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held, other_ended = threading.Event(), threading.Event()
    release = threading.Event()
    first = SerenaConfig.from_config_file()
    second = SerenaConfig.from_config_file()
    one, other = _project(work_dir, "one"), _project(work_dir, "other")
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


def test_a_configuration_with_no_file_saves_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    saved: list[str] = []
    monkeypatch.setattr(serena_config, "save_yaml", lambda path, data: saved.append(path))

    SerenaConfig().save()

    assert saved == []


def test_a_save_writes_a_file_removed_since_it_was_read(home: Path, work_dir: Path) -> None:
    project = _project(work_dir, "one")
    config = SerenaConfig.from_config_file()
    (home / SerenaConfig.CONFIG_FILE).unlink()

    config.add_project_from_path(project)

    assert _roots_on_file(home) == {str(project.resolve())}


def test_a_save_over_a_file_that_lists_no_projects_keeps_what_this_instance_registered(home: Path, work_dir: Path) -> None:
    project = _project(work_dir, "one")
    config = SerenaConfig.from_config_file()
    (home / SerenaConfig.CONFIG_FILE).write_text("{}\n")

    config.add_project_from_path(project)

    assert _roots_on_file(home) == {str(project.resolve())}


def test_a_save_drops_a_listed_root_whose_project_is_gone(home: Path, work_dir: Path) -> None:
    gone = work_dir / "gone"
    config = SerenaConfig.from_config_file()
    config.save()
    path = home / SerenaConfig.CONFIG_FILE
    data = load_yaml(str(path))
    data["projects"] = [str(gone)]
    save_yaml(str(path), data)

    SerenaConfig.from_config_file().add_project_from_path(_project(work_dir, "kept"))

    assert _roots_on_file(home) == {str((work_dir / "kept").resolve())}


def test_a_save_refuses_a_file_it_cannot_read_and_leaves_it_as_it_was(home: Path) -> None:
    config = SerenaConfig.from_config_file()
    path = home / SerenaConfig.CONFIG_FILE
    path.write_text("projects: [unterminated\n")

    with pytest.raises(Exception, match="(?i)flow sequence|expected"):
        config.save()

    assert path.read_text() == "projects: [unterminated\n"
