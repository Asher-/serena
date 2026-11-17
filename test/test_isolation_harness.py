import os
import subprocess
import sys
from pathlib import Path

import pytest

from serena.config.serena_paths import SerenaPaths
from solidlsp.ls_config import LanguageServerId
from test.conftest import get_repo_path, get_repos_dir
from test.fixture_repositories import FixtureRepositories
from test.language_server_installation import TestLanguageServerInstallation

_CHECKED_IN_REPOS = Path(__file__).parent / "resources" / "repos"


def _tracked_files(directory: Path) -> set[str]:
    listing = subprocess.run(["git", "ls-files", "-z"], cwd=directory, check=True, capture_output=True).stdout
    return {path.decode() for path in listing.split(b"\0") if path}


def _static_dir(serena_home: Path) -> Path:
    return serena_home / "language_servers" / "static"


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


class TestProcessRepositories:
    def test_get_repos_dir_is_a_populated_copy_outside_the_checked_in_repos(self) -> None:
        repos_dir = get_repos_dir("typescript")

        assert not repos_dir.is_relative_to(_CHECKED_IN_REPOS)
        assert (repos_dir / "cross_package_a").is_dir()
        assert get_repos_dir("typescript") == repos_dir

    def test_get_repo_path_is_a_copy_outside_the_checked_in_repos(self) -> None:
        repo_path = get_repo_path(LanguageServerId.PYTHON)

        assert not repo_path.is_relative_to(_CHECKED_IN_REPOS)
        assert (repo_path / "test_repo" / "models.py").is_file()

    def test_a_language_server_variant_shares_its_language_s_repository(self) -> None:
        assert get_repo_path(LanguageServerId.PYTHON_JEDI) == get_repo_path(LanguageServerId.PYTHON)


class TestInstallationRoot:
    @staticmethod
    def _install(root: Path, relative_file: str, content: str = "") -> None:
        installed_file = _static_dir(root) / relative_file
        installed_file.parent.mkdir(parents=True, exist_ok=True)
        installed_file.write_text(content)

    @staticmethod
    def _linked_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
        monkeypatch.setenv(TestLanguageServerInstallation.ROOT_ENV_VAR, str(root))
        home = tmp_path / "home"
        TestLanguageServerInstallation().link_into(home)
        return home

    def test_a_server_installed_in_the_root_is_available_in_the_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = tmp_path / "root"
        self._install(root, "SomeServer/1.0/bin/server", "installed")

        home = self._linked_home(tmp_path, monkeypatch, root)

        assert (_static_dir(home) / "SomeServer" / "1.0" / "bin" / "server").read_text() == "installed"

    def test_a_server_installed_into_the_home_does_not_reach_the_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = tmp_path / "root"
        self._install(root, "SomeServer/1.0/bin/server")
        home = self._linked_home(tmp_path, monkeypatch, root)

        (_static_dir(home) / "OtherServer").mkdir()
        (_static_dir(home) / "SomeServer" / "2.0").mkdir()
        (_static_dir(home) / "SomeServer" / "2.0" / "server").write_text("")

        assert sorted(path.name for path in _static_dir(root).iterdir()) == ["SomeServer"]
        assert sorted(path.name for path in (_static_dir(root) / "SomeServer").iterdir()) == ["1.0"]

    def test_replacing_an_installed_entry_in_the_home_leaves_the_root_intact(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = tmp_path / "root"
        self._install(root, "SomeServer/version", "1.0")
        home = self._linked_home(tmp_path, monkeypatch, root)
        home_server = _static_dir(home) / "SomeServer"

        replacement = home_server / "version.new"
        replacement.write_text("2.0")
        os.replace(replacement, home_server / "version")

        assert (home_server / "version").read_text() == "2.0"
        assert (_static_dir(root) / "SomeServer" / "version").read_text() == "1.0"

    def test_runtime_state_a_server_keeps_in_its_directory_stays_in_the_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = tmp_path / "root"
        self._install(root, "SomeServer/bin/server")
        home = self._linked_home(tmp_path, monkeypatch, root)

        (_static_dir(home) / "SomeServer" / "workspaces" / "project").mkdir(parents=True)

        assert sorted(path.name for path in (_static_dir(root) / "SomeServer").iterdir()) == ["bin"]

    def test_a_missing_root_gives_the_home_an_empty_server_directory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = self._linked_home(tmp_path, monkeypatch, tmp_path / "absent")

        assert list(_static_dir(home).iterdir()) == []

    def test_the_root_is_the_configured_directory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TestLanguageServerInstallation.ROOT_ENV_VAR, str(tmp_path / "configured"))

        assert TestLanguageServerInstallation().root == tmp_path / "configured"

    def test_the_root_defaults_to_the_user_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(TestLanguageServerInstallation.ROOT_ENV_VAR, raising=False)

        assert TestLanguageServerInstallation().root == Path.home() / ".cache" / "serena" / "test-language-servers"

    def test_installing_an_unknown_language_server_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(TestLanguageServerInstallation.ROOT_ENV_VAR, str(tmp_path / "root"))
        monkeypatch.setenv("SERENA_HOME", os.environ["SERENA_HOME"])

        with pytest.raises(SystemExit) as exit_info:
            TestLanguageServerInstallation().install(["no-such-language-server"])

        assert exit_info.value.code != 0

    def test_this_process_s_serena_home_holds_every_installed_server(self) -> None:
        installed_dir = _static_dir(TestLanguageServerInstallation().root)
        installed = [path.name for path in installed_dir.iterdir()] if installed_dir.is_dir() else []

        home_static_dir = _static_dir(Path(SerenaPaths().serena_user_home_dir))

        assert set(installed) <= {path.name for path in home_static_dir.iterdir()}


class TestTimeout:
    def test_every_test_is_bounded_by_a_timeout(self, pytestconfig: pytest.Config) -> None:
        assert float(pytestconfig.getini("timeout")) > 0

    def test_a_test_running_past_the_configured_timeout_fails(self, tmp_path: Path, pytestconfig: pytest.Config) -> None:
        probe = tmp_path / "test_probe.py"
        probe.write_text("import time\n\n\ndef test_probe():\n    time.sleep(30)\n")

        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-c", str(pytestconfig.inipath), "--rootdir", str(tmp_path), "-p", "no:cacheprovider"]
            + ["-o", "timeout=1", "-q", str(probe)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

        assert result.returncode != 0
        assert "Timeout" in result.stdout
