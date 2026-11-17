import subprocess
from pathlib import Path

import pytest

from solidlsp.ls_config import LanguageServerId
from test.conftest import get_repo_path
from test.fixture_repositories import FixtureRepositories
from test.language_server_installation import TestLanguageServerInstallation

_CHECKED_IN_REPOS = Path(__file__).parent / "resources" / "repos"


def _tracked_files(directory: Path) -> set[str]:
    listing = subprocess.run(["git", "ls-files", "-z"], cwd=directory, check=True, capture_output=True).stdout
    return {path.decode() for path in listing.split(b"\0") if path}


class TestFixtureRepositories:
    def test_a_copy_holds_exactly_the_tracked_files(self, tmp_path: Path) -> None:
        copy_dir = FixtureRepositories(tmp_path / "repos").get_dir("python")

        copied = {str(path.relative_to(copy_dir)) for path in copy_dir.rglob("*") if not path.is_dir()}
        assert copied == _tracked_files(_CHECKED_IN_REPOS / "python")

    def test_a_file_written_into_a_copy_does_not_reach_the_checked_in_directory(self, tmp_path: Path) -> None:
        copy_dir = FixtureRepositories(tmp_path / "repos").get_dir("python")

        (copy_dir / "test_repo" / "_isolation_probe.py").write_text("x = 1\n")

        assert not (_CHECKED_IN_REPOS / "python" / "test_repo" / "_isolation_probe.py").exists()

    def test_a_second_use_returns_the_same_copy_with_its_changes(self, tmp_path: Path) -> None:
        repositories = FixtureRepositories(tmp_path / "repos")
        first = repositories.get_dir("python")
        (first / "_isolation_probe").write_text("")

        second = repositories.get_dir("python")

        assert second == first
        assert (second / "_isolation_probe").exists()

    def test_get_repo_path_is_a_copy_outside_the_checked_in_repos(self) -> None:
        repo_path = get_repo_path(LanguageServerId.PYTHON)

        assert not repo_path.is_relative_to(_CHECKED_IN_REPOS)
        assert (repo_path / "test_repo" / "models.py").is_file()


class TestInstallationRoot:
    @staticmethod
    def _static_dir(serena_home: Path) -> Path:
        return serena_home / "language_servers" / "static"

    def test_a_server_installed_in_the_root_is_available_in_the_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = tmp_path / "root"
        (self._static_dir(root) / "SomeServer").mkdir(parents=True)
        (self._static_dir(root) / "SomeServer" / "server").write_text("")
        monkeypatch.setenv(TestLanguageServerInstallation.ROOT_ENV_VAR, str(root))
        home = tmp_path / "home"

        TestLanguageServerInstallation().link_into(home)

        assert (self._static_dir(home) / "SomeServer" / "server").is_file()

    def test_a_server_installed_into_the_home_does_not_reach_the_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = tmp_path / "root"
        self._static_dir(root).mkdir(parents=True)
        monkeypatch.setenv(TestLanguageServerInstallation.ROOT_ENV_VAR, str(root))
        home = tmp_path / "home"
        TestLanguageServerInstallation().link_into(home)

        (self._static_dir(home) / "OtherServer").mkdir()

        assert list(self._static_dir(root).iterdir()) == []

    def test_a_missing_root_gives_the_home_an_empty_server_directory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TestLanguageServerInstallation.ROOT_ENV_VAR, str(tmp_path / "absent"))
        home = tmp_path / "home"

        TestLanguageServerInstallation().link_into(home)

        assert list(self._static_dir(home).iterdir()) == []

    def test_the_root_defaults_to_the_user_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(TestLanguageServerInstallation.ROOT_ENV_VAR, raising=False)

        assert TestLanguageServerInstallation().root == Path.home() / ".cache" / "serena" / "test-language-servers"


def test_every_test_is_bounded_by_a_timeout(pytestconfig: pytest.Config) -> None:
    assert float(pytestconfig.getini("timeout")) > 0
