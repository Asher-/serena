import hashlib
import logging
import os
import shutil
import tempfile
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from serena.agent import SerenaAgent
from serena.config.serena_config import (
    DEFAULT_PROJECT_SERENA_FOLDER_LOCATION,
    AgentInterface,
    ProjectConfig,
    RegisteredProject,
    SerenaConfig,
    SerenaConfigError,
    SerenaPaths,
)
from serena.constants import PROJECT_TEMPLATE_FILE, SERENA_MANAGED_DIR_NAME
from serena.language_backend import BuiltinLanguageBackend, LanguageBackend
from serena.project import MemoryManager, Project
from solidlsp.ls_config import LanguageServerId
from test.conftest import create_default_serena_config


class TestProjectConfigAutogenerate:
    """Test class for ProjectConfig autogeneration functionality."""

    def setup_method(self):
        """Set up test environment before each test method."""
        # Create a temporary directory for testing
        self.test_dir = tempfile.mkdtemp()
        self.serena_config = create_default_serena_config()
        self.project_path = Path(self.test_dir)

    def teardown_method(self):
        """Clean up test environment after each test method."""
        # Remove the temporary directory
        shutil.rmtree(self.test_dir)

    def test_autogenerate_empty_directory(self):
        """Test that autogenerate succeeds with empty languages list for an empty directory."""
        config = ProjectConfig.autogenerate(self.project_path, self.serena_config, save_to_disk=False)

        assert config.project_name == self.project_path.name
        assert config.language_servers == []

    def test_autogenerate_empty_directory_logs_warning(self, caplog):
        """Test that autogenerate logs a warning when no language files are found."""
        with caplog.at_level(logging.WARNING):
            ProjectConfig.autogenerate(self.project_path, self.serena_config, save_to_disk=False)

        assert any("No source files for supported language servers were found" in msg for msg in caplog.messages)

    def test_autogenerate_with_python_files(self):
        """Test successful autogeneration with Python source files."""
        # Create a Python file
        python_file = self.project_path / "main.py"
        python_file.write_text("def hello():\n    print('Hello, world!')\n")

        # Run autogenerate
        config = ProjectConfig.autogenerate(self.project_path, self.serena_config, save_to_disk=False)

        # Verify the configuration
        assert config.project_name == self.project_path.name
        assert config.language_servers == [LanguageServerId.PYTHON]

    def test_autogenerate_with_js_files(self):
        """Test successful autogeneration with JavaScript source files."""
        # Create files for multiple languages
        (self.project_path / "small.js").write_text("console.log('JS');")

        # Run autogenerate - should pick Python as dominant
        config = ProjectConfig.autogenerate(self.project_path, self.serena_config, save_to_disk=False)

        assert config.language_servers == [LanguageServerId.TYPESCRIPT]

    def test_autogenerate_with_multiple_languages(self):
        """Test autogeneration picks dominant language when multiple are present."""
        # Create files for multiple languages
        (self.project_path / "main.py").write_text("print('Python')")
        (self.project_path / "util.py").write_text("def util(): pass")
        (self.project_path / "small.js").write_text("console.log('JS');")

        # Run autogenerate - should pick Python as dominant
        config = ProjectConfig.autogenerate(self.project_path, self.serena_config, save_to_disk=False)

        assert config.language_servers == [LanguageServerId.PYTHON]

    def test_autogenerate_saves_to_disk(self):
        """Test that autogenerate can save the configuration to disk."""
        # Create a Go file
        go_file = self.project_path / "main.go"
        go_file.write_text("package main\n\nfunc main() {}\n")

        # Run autogenerate with save_to_disk=True
        config = ProjectConfig.autogenerate(self.project_path, self.serena_config, save_to_disk=True)

        # Verify the configuration file was created
        config_path = self.project_path / ".serena" / "project.yml"
        assert config_path.exists()

        # Verify the content
        assert config.language_servers == [LanguageServerId.GO]

    def test_autogenerate_nonexistent_path(self):
        """Test that autogenerate raises FileNotFoundError for non-existent path."""
        non_existent = self.project_path / "does_not_exist"

        with pytest.raises(FileNotFoundError) as exc_info:
            ProjectConfig.autogenerate(non_existent, self.serena_config, save_to_disk=False)

        assert "Project root not found" in str(exc_info.value)

    def test_autogenerate_with_gitignored_files_only(self):
        """Test autogenerate creates a project with empty languages when only gitignored files exist."""
        # Create a .gitignore that ignores all Python files
        gitignore = self.project_path / ".gitignore"
        gitignore.write_text("*.py\n")

        # Create Python files that will be ignored
        (self.project_path / "ignored.py").write_text("print('ignored')")

        # Should succeed with empty languages (gitignored files are not counted)
        config = ProjectConfig.autogenerate(self.project_path, self.serena_config, save_to_disk=False)

        assert config.project_name == self.project_path.name
        assert config.language_servers == []

    def test_autogenerate_custom_project_name(self):
        """Test autogenerate with custom project name."""
        # Create a TypeScript file
        ts_file = self.project_path / "index.ts"
        ts_file.write_text("const greeting: string = 'Hello';\n")

        # Run autogenerate with custom name
        custom_name = "my-custom-project"
        config = ProjectConfig.autogenerate(self.project_path, self.serena_config, project_name=custom_name, save_to_disk=False)

        assert config.project_name == custom_name
        assert config.language_servers == [LanguageServerId.TYPESCRIPT]

    def test_autogenerate_with_python_files_and_custom_ls_priorities(self):
        """Test successful autogeneration with Python source files, using custom language server priorities."""
        # Create a Python file
        python_file = self.project_path / "main.py"
        python_file.write_text("def hello():\n    print('Hello, world!')\n")

        serena_config = deepcopy(self.serena_config)
        serena_config.ls_priorities = {LanguageServerId.PYTHON_TY.value: 3}

        # Run autogenerate
        config = ProjectConfig.autogenerate(self.project_path, serena_config, save_to_disk=False)

        # Verify the configuration
        assert config.project_name == self.project_path.name
        assert config.language_servers == [LanguageServerId.PYTHON_TY]


class TestProjectConfig:
    def test_template_is_complete(self):
        _, is_complete = ProjectConfig._load_yaml_dict(PROJECT_TEMPLATE_FILE)
        assert is_complete, "Project template YAML is incomplete; all fields must be present (with descriptions)."


class TestProjectConfigRoundtripEquality:
    """Tests that a ProjectConfig loaded from an incomplete yml compares equal to one reloaded
    after the incomplete-yml re-save has rewritten the file with defaults. Guards against silent
    divergence between in-memory tuple defaults on sequence fields and their ruamel-serialised
    list counterparts on disk.
    """

    def setup_method(self):
        self.test_dir = tempfile.mkdtemp()
        self.project_path = Path(self.test_dir)
        self.serena_config = create_default_serena_config()
        serena_dir = self.project_path / SERENA_MANAGED_DIR_NAME
        serena_dir.mkdir(parents=True, exist_ok=True)
        self.yml_path = serena_dir / ProjectConfig.SERENA_PROJECT_FILE

    def teardown_method(self):
        shutil.rmtree(self.test_dir)

    def test_minimal_yml_reload_equals_first_load(self):
        """A minimal hand-written project.yml (only project_name and languages) must produce a
        ProjectConfig that dataclass-compares equal to one loaded from the now-complete yml
        that the first load re-wrote to disk.
        """
        # minimal yml: only the two FIELDS_WITHOUT_DEFAULTS; every other field will be default-injected
        self.yml_path.write_text(
            "project_name: roundtrip_equality\nlanguages:\n  - python\n",
            encoding="utf-8",
        )

        # first load triggers the incomplete-yml path and re-saves the yml with all defaults materialised
        first = ProjectConfig.load(self.project_path, serena_config=self.serena_config)
        # second load reads the now-complete yml (where ruamel has serialised sequence defaults as YAML lists)
        second = ProjectConfig.load(self.project_path, serena_config=self.serena_config)

        assert first == second


class TestProjectConfigLanguageBackend:
    """Tests for the per-project language_backend field."""

    def test_language_backend_defaults_to_none(self):
        config = ProjectConfig(
            project_name="test",
            language_servers=[LanguageServerId.PYTHON],
        )
        assert config.language_backend is None

    def test_language_backend_can_be_set(self):
        config = ProjectConfig(
            project_name="test",
            language_servers=[LanguageServerId.PYTHON],
            language_backend=BuiltinLanguageBackend.JETBRAINS.get_instance(),
        )
        assert config.language_backend == BuiltinLanguageBackend.JETBRAINS.get_instance()

    def test_language_backend_roundtrips_through_yaml(self):
        config = ProjectConfig(
            project_name="test",
            language_servers=[LanguageServerId.PYTHON],
            language_backend=BuiltinLanguageBackend.JETBRAINS.get_instance(),
        )
        d = config._to_yaml_dict()
        assert d["language_backend"] == "JetBrains"

    def test_language_backend_none_roundtrips_through_yaml(self):
        config = ProjectConfig(
            project_name="test",
            language_servers=[LanguageServerId.PYTHON],
        )
        d = config._to_yaml_dict()
        assert d["language_backend"] is None

    def test_language_backend_parsed_from_dict(self):
        """Test that _from_dict parses language_backend correctly."""
        template_path = PROJECT_TEMPLATE_FILE
        data, _ = ProjectConfig._load_yaml_dict(template_path)
        data["project_name"] = "test"
        data["languages"] = ["python"]
        data["language_backend"] = "JetBrains"
        config = ProjectConfig._from_dict(data, local_override_keys=[])
        assert config.language_backend == BuiltinLanguageBackend.JETBRAINS.get_instance()

    def test_language_backend_none_when_missing_from_dict(self):
        """Test that _from_dict handles missing language_backend gracefully."""
        template_path = PROJECT_TEMPLATE_FILE
        data, _ = ProjectConfig._load_yaml_dict(template_path)
        data["project_name"] = "test"
        data["languages"] = ["python"]
        data.pop("language_backend", None)
        config = ProjectConfig._from_dict(data, local_override_keys=[])
        assert config.language_backend is None


def _make_config_with_project(
    project_name: str,
    language_backend: LanguageBackend | None = None,
    global_backend: LanguageBackend = BuiltinLanguageBackend.LSP.get_instance(),
) -> tuple[SerenaConfig, str]:
    """Create a SerenaConfig with a single registered project and return (config, project_name)."""
    config = SerenaConfig(
        gui_log_window=False,
        web_dashboard=False,
        log_level=logging.ERROR,
        language_backend=global_backend,
    )
    project = Project(
        project_root=str(Path(__file__).parent.parent / "resources" / "repos" / "python" / "test_repo"),
        project_config=ProjectConfig(
            project_name=project_name,
            language_servers=[LanguageServerId.PYTHON],
            language_backend=language_backend,
        ),
        serena_config=config,
    )
    config.projects = [RegisteredProject.from_project_instance(project)]
    return config, project_name


class TestEffectiveLanguageBackend:
    """Tests for per-project language_backend override logic in SerenaAgent."""

    def test_default_backend_is_global(self):
        """When no project override, effective backend matches global config."""
        config, name = _make_config_with_project("test_proj", language_backend=None, global_backend=BuiltinLanguageBackend.LSP.get_instance())
        agent = SerenaAgent(project=name, serena_config=config)
        try:
            assert agent.get_language_backend().is_lsp()
        finally:
            agent.on_shutdown(timeout=5)

    def test_project_overrides_global_backend(self):
        """When startup project has language_backend set, it overrides the global."""
        config, name = _make_config_with_project(
            "test_jetbrains", language_backend=BuiltinLanguageBackend.JETBRAINS.get_instance(), global_backend=BuiltinLanguageBackend.LSP.get_instance()
        )
        agent = SerenaAgent(project=name, serena_config=config)
        try:
            assert agent.get_language_backend().is_jetbrains()
        finally:
            agent.on_shutdown(timeout=5)

    def test_no_project_uses_global_backend(self):
        """When no startup project is provided, effective backend is the global one."""
        config = SerenaConfig(
            gui_log_window=False,
            web_dashboard=False,
            log_level=logging.ERROR,
            language_backend=BuiltinLanguageBackend.LSP.get_instance(),
        )
        agent = SerenaAgent(project=None, serena_config=config)
        try:
            assert agent.get_language_backend() == BuiltinLanguageBackend.LSP.get_instance()
        finally:
            agent.on_shutdown(timeout=5)

    def test_activate_project_rejects_backend_mismatch(self):
        """Post-init activation of a project with mismatched backend raises ValueError."""
        # Start with LSP backend
        config, name = _make_config_with_project("lsp_proj", language_backend=None, global_backend=BuiltinLanguageBackend.LSP.get_instance())

        # Add a second project that requires JetBrains
        jb_project = Project(
            project_root=str(Path(__file__).parent.parent / "resources" / "repos" / "java" / "test_repo"),
            project_config=ProjectConfig(
                project_name="jb_proj",
                language_servers=[LanguageServerId.JAVA],
                language_backend=BuiltinLanguageBackend.JETBRAINS.get_instance(),
            ),
            serena_config=config,
        )
        config.projects.append(RegisteredProject.from_project_instance(jb_project))

        agent = SerenaAgent(project=name, serena_config=config)
        try:
            with pytest.raises(ValueError, match="Cannot activate project"):
                agent.activate_project_from_path_or_name("jb_proj")
        finally:
            agent.on_shutdown(timeout=5)

    def test_activate_project_allows_matching_backend(self):
        """Post-init activation of a project with matching backend succeeds."""
        config, name = _make_config_with_project("lsp_proj", language_backend=None, global_backend=BuiltinLanguageBackend.LSP.get_instance())

        # Add a second project that also uses LSP
        lsp_project2 = Project(
            project_root=str(Path(__file__).parent.parent / "resources" / "repos" / "python" / "test_repo"),
            project_config=ProjectConfig(
                project_name="lsp_proj2",
                language_servers=[LanguageServerId.PYTHON],
                language_backend=BuiltinLanguageBackend.LSP.get_instance(),
            ),
            serena_config=config,
        )
        config.projects.append(RegisteredProject.from_project_instance(lsp_project2))

        agent = SerenaAgent(project=name, serena_config=config)
        try:
            # Should not raise
            agent.activate_project_from_path_or_name("lsp_proj2")
        finally:
            agent.on_shutdown(timeout=5)

    def test_activate_project_allows_none_backend(self):
        """Post-init activation of a project with no backend override succeeds."""
        config, name = _make_config_with_project("lsp_proj", language_backend=None, global_backend=BuiltinLanguageBackend.LSP.get_instance())

        # Add a second project with no backend override
        proj2 = Project(
            project_root=str(Path(__file__).parent.parent / "resources" / "repos" / "python" / "test_repo"),
            project_config=ProjectConfig(
                project_name="proj2",
                language_servers=[LanguageServerId.PYTHON],
                language_backend=None,
            ),
            serena_config=config,
        )
        config.projects.append(RegisteredProject.from_project_instance(proj2))

        agent = SerenaAgent(project=name, serena_config=config)
        try:
            # Should not raise — None means "inherit session backend"
            agent.activate_project_from_path_or_name("proj2")
        finally:
            agent.on_shutdown(timeout=5)

    def test_activate_project_switches_backend_with_repl_interface(self):
        """With the REPL interface, post-init activation of a project with a different backend switches the backend."""
        config, name = _make_config_with_project("lsp_proj", language_backend=None, global_backend=BuiltinLanguageBackend.LSP)
        config.agent_interface = AgentInterface.REPL
        jb_project = Project(
            project_root=str(Path(__file__).parent.parent / "resources" / "repos" / "java" / "test_repo"),
            project_config=ProjectConfig(
                project_name="jb_proj",
                language_servers=[LanguageServerId.JAVA],
                language_backend=BuiltinLanguageBackend.JETBRAINS.get_instance(),
            ),
            serena_config=config,
        )
        config.projects.append(RegisteredProject.from_project_instance(jb_project))

        agent = SerenaAgent(project=name, serena_config=config)
        try:
            assert agent.get_language_backend().is_lsp()
            assert "s.lsp" in agent.get_repl().entrypoint.overview()

            # the backend and everything depending on it follow the activated project
            agent.activate_project_from_path_or_name("jb_proj")
            assert agent.get_language_backend().is_jetbrains()
            overview = agent.get_repl().entrypoint.overview()
            assert "s.jb" in overview and "s.lsp" not in overview
            assert "jetbrains" in [m.name for m in agent.get_active_modes().get_modes(include_background_base_modes=True)]
        finally:
            agent.on_shutdown(timeout=5)


class TestGetConfiguredProjectSerenaFolder:
    """Tests for SerenaConfig.get_configured_project_serena_folder (pure template resolution)."""

    def test_default_location(self):
        config = SerenaConfig(
            gui_log_window=False,
            web_dashboard=False,
        )
        result = config.get_configured_project_serena_folder("/home/user/myproject")
        assert result == os.path.abspath("/home/user/myproject/.serena")

    def test_custom_location_with_project_folder_name(self):
        config = SerenaConfig(
            gui_log_window=False,
            web_dashboard=False,
            project_serena_folder_location="/projects-metadata/$projectFolderName/.serena",
        )
        result = config.get_configured_project_serena_folder("/home/user/myproject")
        assert result == os.path.abspath("/projects-metadata/myproject/.serena")

    def test_custom_location_with_project_dir(self):
        config = SerenaConfig(
            gui_log_window=False,
            web_dashboard=False,
            project_serena_folder_location="$projectDir/.custom-serena",
        )
        result = config.get_configured_project_serena_folder("/home/user/myproject")
        assert result == os.path.abspath("/home/user/myproject/.custom-serena")

    def test_custom_location_with_both_placeholders(self):
        config = SerenaConfig(
            gui_log_window=False,
            web_dashboard=False,
            project_serena_folder_location="/data/$projectFolderName/$projectDir/.serena",
        )
        result = config.get_configured_project_serena_folder("/home/user/proj")
        assert result == os.path.abspath("/data/proj/home/user/proj/.serena")

    def test_default_field_value(self):
        config = SerenaConfig(
            gui_log_window=False,
            web_dashboard=False,
        )
        assert config.project_serena_folder_location == DEFAULT_PROJECT_SERENA_FOLDER_LOCATION

    def test_rejects_unknown_placeholder(self):
        config = SerenaConfig(
            gui_log_window=False,
            web_dashboard=False,
            project_serena_folder_location="$projectDir/$unknownVar/.serena",
        )
        with pytest.raises(SerenaConfigError, match=r"Unknown placeholder '\$unknownVar'"):
            config.get_configured_project_serena_folder("/home/user/myproject")

    def test_rejects_typo_projectDirs(self):
        """$projectDirs should not be silently treated as $projectDir + 's'."""
        config = SerenaConfig(
            gui_log_window=False,
            web_dashboard=False,
            project_serena_folder_location="$projectDirs/.serena",
        )
        with pytest.raises(SerenaConfigError, match=r"Unknown placeholder '\$projectDirs'"):
            config.get_configured_project_serena_folder("/home/user/myproject")

    def test_rejects_typo_projectfoldername_lowercase(self):
        config = SerenaConfig(
            gui_log_window=False,
            web_dashboard=False,
            project_serena_folder_location="/data/$projectfoldername/.serena",
        )
        with pytest.raises(SerenaConfigError, match=r"Unknown placeholder '\$projectfoldername'"):
            config.get_configured_project_serena_folder("/home/user/myproject")

    def test_no_placeholders_is_valid(self):
        config = SerenaConfig(
            gui_log_window=False,
            web_dashboard=False,
            project_serena_folder_location="/fixed/path/.serena",
        )
        result = config.get_configured_project_serena_folder("/home/user/myproject")
        assert result == os.path.abspath("/fixed/path/.serena")

    def test_error_message_lists_supported_placeholders(self):
        config = SerenaConfig(
            gui_log_window=False,
            web_dashboard=False,
            project_serena_folder_location="$bogus/.serena",
        )
        with pytest.raises(SerenaConfigError, match=r"\$projectDir.*\$projectFolderName|\$projectFolderName.*\$projectDir"):
            config.get_configured_project_serena_folder("/home/user/myproject")


class TestProjectSerenaDataFolder:
    """Tests for SerenaConfig.get_project_serena_folder fallback logic (via Project)."""

    def setup_method(self):
        self.test_dir = tempfile.mkdtemp()
        self.project_path = Path(self.test_dir) / "myproject"
        self.project_path.mkdir()
        (self.project_path / "main.py").write_text("print('hello')\n")

    def teardown_method(self):
        shutil.rmtree(self.test_dir)

    def _make_project(self, serena_config: "SerenaConfig | None" = None) -> Project:
        project_config = ProjectConfig(
            project_name="myproject",
            language_servers=[LanguageServerId.PYTHON],
        )
        project = Project(
            project_root=str(self.project_path),
            project_config=project_config,
            serena_config=serena_config,
        )
        project._ignore_spec_available.wait()
        return project

    def test_default_config_creates_in_project_dir(self):
        config = SerenaConfig(gui_log_window=False, web_dashboard=False)
        project = self._make_project(config)
        expected = os.path.abspath(str(self.project_path / SERENA_MANAGED_DIR_NAME))
        assert project.path_to_serena_data_folder() == expected

    def test_custom_location_creates_outside_project(self):
        custom_base = Path(self.test_dir) / "metadata"
        custom_base.mkdir()
        config = SerenaConfig(
            gui_log_window=False,
            web_dashboard=False,
            project_serena_folder_location=str(custom_base) + "/$projectFolderName/.serena",
        )
        project = self._make_project(config)
        expected = os.path.abspath(str(custom_base / "myproject" / ".serena"))
        assert project.path_to_serena_data_folder() == expected

    def test_fallback_to_existing_project_dir(self):
        """If config points to a non-existent path but .serena exists in the project root, use the existing one."""
        existing_serena = self.project_path / SERENA_MANAGED_DIR_NAME
        existing_serena.mkdir()
        config = SerenaConfig(
            gui_log_window=False,
            web_dashboard=False,
            project_serena_folder_location="/nonexistent/path/$projectFolderName/.serena",
        )
        project = self._make_project(config)
        assert project.path_to_serena_data_folder() == str(existing_serena)

    def test_configured_path_takes_precedence_when_exists(self):
        """If both config path and project root path exist, use the config path."""
        existing_serena = self.project_path / SERENA_MANAGED_DIR_NAME
        existing_serena.mkdir()

        custom_base = Path(self.test_dir) / "metadata"
        custom_serena = custom_base / "myproject" / ".serena"
        custom_serena.mkdir(parents=True)

        config = SerenaConfig(
            gui_log_window=False,
            web_dashboard=False,
            project_serena_folder_location=str(custom_base) + "/$projectFolderName/.serena",
        )
        project = self._make_project(config)
        assert project.path_to_serena_data_folder() == str(custom_serena)


class TestDataFolderPlacement:
    """Tests calling directly the SerenaConfig units that locate a project's data folder and place it outside the project."""

    @pytest.fixture(autouse=True)
    def _directories(self, work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Provides a project directory and a serena home directory confined to the test."""
        self.work_dir = work_dir
        self.project_root = work_dir / "myproject"
        self.project_root.mkdir()

        # the serena home directory, and the three folders a project's data can occupy
        serena_home = work_dir / "serena_home"
        serena_home.mkdir()
        monkeypatch.setattr(SerenaPaths(), "serena_user_home_dir", str(serena_home))
        self.serena_home = serena_home
        self.home_folder = Path(SerenaConfig.get_home_project_serena_folder(self.project_root))
        self.default_folder = self.project_root / SERENA_MANAGED_DIR_NAME
        self.configured_folder = work_dir / "metadata" / "myproject" / SERENA_MANAGED_DIR_NAME

    def _config(self, location: str | None = None) -> SerenaConfig:
        """
        :param location: the configured data folder location; ``None`` selects the location outside the project,
            whose folder is ``configured_folder``
        :return: a configuration with no registered project and no configuration file
        """
        if location is None:
            location = str(self.work_dir / "metadata") + "/$projectFolderName/" + SERENA_MANAGED_DIR_NAME
        return SerenaConfig(gui_log_window=False, web_dashboard=False, project_serena_folder_location=location)

    @staticmethod
    def _make_folder(folder: Path, holds_project_yml: bool = False) -> Path:
        """
        :param folder: the data folder to create
        :param holds_project_yml: whether to write a project.yml into it
        :return: the folder
        """
        folder.mkdir(parents=True)
        if holds_project_yml:
            (folder / ProjectConfig.SERENA_PROJECT_FILE).write_text("project_name: myproject\n")
        return folder

    def _files_under_project(self) -> set[Path]:
        """
        :return: every file and directory beneath the project directory
        """
        return set(self.project_root.rglob("*"))

    @staticmethod
    def _add(config: SerenaConfig, project_root: Path) -> Project:
        """
        Adds a project and waits for its background scan, which would otherwise outlive the test's directory.

        :param config: the configuration to add the project to
        :param project_root: the project directory
        :return: the added project
        """
        project = config.add_project_from_path(project_root)
        project._ignore_spec_available.wait()
        return project

    def test_add_refuses_a_path_that_does_not_exist(self):
        with pytest.raises(FileNotFoundError, match="Path does not exist"):
            self._config(DEFAULT_PROJECT_SERENA_FOLDER_LOCATION).add_project_from_path(self.project_root / "absent")

    def test_add_refuses_a_path_that_is_not_a_directory(self):
        module = self.project_root / "main.py"
        module.write_text("print('hello')\n")

        with pytest.raises(FileNotFoundError, match="Path is not a directory"):
            self._config(DEFAULT_PROJECT_SERENA_FOLDER_LOCATION).add_project_from_path(module)

    def test_add_refuses_a_directory_already_registered(self):
        config = self._config(DEFAULT_PROJECT_SERENA_FOLDER_LOCATION)
        self._add(config, self.project_root)

        with pytest.raises(FileExistsError, match="already added with name 'myproject'"):
            config.add_project_from_path(self.project_root)

        assert len(config.projects) == 1

    def test_add_registers_a_directory_beside_another_and_writes_nothing_inside_it(self):
        # a directory holding source, and a configuration in which another project is registered
        (self.project_root / "main.py").write_text("print('hello')\n")
        files_before = self._files_under_project()
        config = self._config(DEFAULT_PROJECT_SERENA_FOLDER_LOCATION)
        other_root = self.work_dir / "other"
        other_root.mkdir()
        self._add(config, other_root)

        project = self._add(config, self.project_root)

        assert project.project_root == str(self.project_root.resolve())
        assert [str(registered.project_root) for registered in config.projects] == [str(other_root.resolve()), project.project_root]
        assert (self.home_folder / ProjectConfig.SERENA_PROJECT_FILE).is_file()
        assert self._files_under_project() == files_before

    def test_a_configured_folder_holding_project_yml_is_resolved_first(self):
        self._make_folder(self.configured_folder, holds_project_yml=True)
        self._make_folder(self.default_folder, holds_project_yml=True)
        self._make_folder(self.home_folder)

        assert self._config().get_project_serena_folder(self.project_root) == str(self.configured_folder)

    def test_a_default_folder_holding_project_yml_outranks_the_home_folder(self):
        self._make_folder(self.configured_folder)
        self._make_folder(self.default_folder, holds_project_yml=True)
        self._make_folder(self.home_folder)

        assert self._config().get_project_serena_folder(self.project_root) == str(self.default_folder)

    def test_the_home_folder_outranks_folders_without_project_yml(self):
        self._make_folder(self.configured_folder)
        self._make_folder(self.default_folder)
        self._make_folder(self.home_folder)

        assert self._config().get_project_serena_folder(self.project_root) == str(self.home_folder)

    def test_a_configured_folder_without_project_yml_outranks_the_default_folder(self):
        self._make_folder(self.configured_folder)
        self._make_folder(self.default_folder)

        assert self._config().get_project_serena_folder(self.project_root) == str(self.configured_folder)

    def test_a_default_folder_without_project_yml_is_used_when_nothing_else_exists(self):
        self._make_folder(self.default_folder)

        assert self._config().get_project_serena_folder(self.project_root) == str(self.default_folder)

    def test_the_configured_path_is_returned_for_creation_when_no_folder_exists(self):
        assert self._config().get_project_serena_folder(self.project_root) == str(self.configured_folder)

    def test_the_default_location_is_returned_for_creation_when_it_is_the_configured_one(self):
        config = self._config(DEFAULT_PROJECT_SERENA_FOLDER_LOCATION)

        assert config.get_project_serena_folder(self.project_root) == str(self.default_folder)

    @pytest.mark.parametrize("location", [None, DEFAULT_PROJECT_SERENA_FOLDER_LOCATION], ids=["configured", "default"])
    def test_placement_leaves_a_directory_that_holds_project_yml_alone(self, location: str | None):
        holder = self.configured_folder if location is None else self.default_folder
        self._make_folder(holder, holds_project_yml=True)

        self._config(location).place_data_folder_outside_project(self.project_root)

        assert not self.home_folder.exists()

    def test_placement_leaves_a_default_folder_holding_project_yml_alone_under_another_location(self):
        self._make_folder(self.default_folder, holds_project_yml=True)

        self._config().place_data_folder_outside_project(self.project_root)

        assert not self.home_folder.exists()

    def test_placement_creates_a_configured_location_outside_the_project(self):
        self._config().place_data_folder_outside_project(self.project_root)

        assert self.configured_folder.is_dir()
        assert not self.home_folder.exists()

    def test_placement_makes_a_configured_location_outside_the_project_outrank_a_default_folder_without_project_yml(self):
        self._make_folder(self.default_folder)
        config = self._config()

        config.place_data_folder_outside_project(self.project_root)

        assert config.get_project_serena_folder(self.project_root) == str(self.configured_folder)

    @pytest.mark.parametrize("location", [DEFAULT_PROJECT_SERENA_FOLDER_LOCATION, "$projectDir/.data"], ids=["default", "inside"])
    def test_placement_creates_the_home_folder_for_a_location_inside_the_project(self, location: str):
        files_before = self._files_under_project()

        self._config(location).place_data_folder_outside_project(self.project_root)

        assert self.home_folder.is_dir()
        assert self._files_under_project() == files_before

    def test_placement_tolerates_a_home_folder_that_exists(self):
        self._make_folder(self.home_folder)

        self._config(DEFAULT_PROJECT_SERENA_FOLDER_LOCATION).place_data_folder_outside_project(self.project_root)

        assert self.home_folder.is_dir()

    def test_the_home_folder_is_named_after_the_directory_and_a_digest_of_its_path(self):
        digest = hashlib.sha256(str(self.project_root.resolve()).encode("utf-8")).hexdigest()[:16]

        assert self.home_folder == self.serena_home / "projects" / f"myproject-{digest}"

    def test_two_directories_of_one_name_get_different_home_folders(self):
        namesake = self.work_dir / "elsewhere" / "myproject"
        namesake.mkdir(parents=True)

        assert SerenaConfig.get_home_project_serena_folder(namesake) != str(self.home_folder)

    def test_a_folder_holds_project_yml_only_when_that_file_is_in_it(self):
        self._make_folder(self.default_folder, holds_project_yml=True)
        self._make_folder(self.configured_folder)

        assert SerenaConfig._holds_project_yml(str(self.default_folder))
        assert not SerenaConfig._holds_project_yml(str(self.configured_folder))
        assert not SerenaConfig._holds_project_yml(str(self.home_folder))


class TestMemoriesManagerCustomPath:
    """Tests for MemoriesManager with a custom serena data folder."""

    def setup_method(self):
        self.test_dir = tempfile.mkdtemp()
        self.data_folder = Path(self.test_dir) / "custom_serena"

    def teardown_method(self):
        shutil.rmtree(self.test_dir)

    def test_memories_subdir_is_created(self):
        assert not self.data_folder.exists()
        MemoryManager(str(self.data_folder))
        assert (self.data_folder / "memories").exists()

    def test_save_and_load_memory(self):
        manager = MemoryManager(str(self.data_folder))
        manager.save_memory("test_topic", "test content", is_tool_context=False)
        content = manager.load_memory("test_topic")
        assert content == "test content"

    def test_list_memories(self):
        manager = MemoryManager(str(self.data_folder))
        manager.save_memory("topic_a", "content a", is_tool_context=False)
        manager.save_memory("topic_b", "content b", is_tool_context=False)
        memories = manager.list_project_memories()
        assert sorted(memories.get_full_list()) == ["topic_a", "topic_b"]

    def test_path_traversal_is_rejected(self):
        manager = MemoryManager(str(self.data_folder))
        # Parent-directory segments must be rejected before any filesystem touch.
        for bad in ("../escape", "nested/../../../escape", "a/../../b"):
            with pytest.raises(ValueError, match="forbidden path segment"):
                manager.get_memory_file_path(bad)
        # Empty segments (double slash) are also rejected.
        with pytest.raises(ValueError, match="forbidden path segment"):
            manager.get_memory_file_path("a//b")
        # Current-directory segments are rejected.
        with pytest.raises(ValueError, match="forbidden path segment"):
            manager.get_memory_file_path("./a")
        # Legitimate nested names continue to resolve inside the memories root.
        good_path = manager.get_memory_file_path("nested/name")
        assert good_path.parent.name == "nested"


class TestRegisteredProjectReloadIfChanged:
    """Tests for :meth:`RegisteredProject.reload_if_changed` — the hot-reload path that picks up
    user edits to ``project.yml`` between project activations without requiring an MCP restart.
    """

    def setup_method(self):
        # scratch project directory seeded with a minimal hand-written project.yml (only the two
        # FIELDS_WITHOUT_DEFAULTS) so the very first load goes through the incomplete-yml path:
        # _load_yaml_dict injects defaults and ProjectConfig.load re-saves a complete yml to disk.
        # A subsequent reload must then still report "unchanged" — catching regressions of the
        # tuple-vs-list divergence on Sequence[str] fields whose defaults are ().
        self.test_dir = tempfile.mkdtemp()
        self.project_path = Path(self.test_dir)
        self.serena_config = create_default_serena_config()
        serena_dir = self.project_path / SERENA_MANAGED_DIR_NAME
        serena_dir.mkdir(parents=True, exist_ok=True)
        self.yml_path = serena_dir / ProjectConfig.SERENA_PROJECT_FILE
        self.yml_path.write_text(
            "project_name: reload_test_project\nlanguages:\n  - python\n",
            encoding="utf-8",
        )
        self.registered = RegisteredProject.from_project_root(str(self.project_path), serena_config=self.serena_config)

    def teardown_method(self):
        shutil.rmtree(self.test_dir)

    def test_returns_false_when_yml_unchanged(self):
        """freshly-loaded config already reflects disk; a no-op reload reports False and preserves state."""
        assert self.registered.project_config.language_servers == [LanguageServerId.PYTHON]
        assert self.registered.reload_if_changed(self.serena_config) is False
        assert self.registered.project_config.language_servers == [LanguageServerId.PYTHON]

    def test_picks_up_added_language(self):
        """Editing project.yml to append a language is reflected on the next reload_if_changed call."""
        cfg = ProjectConfig.load(self.project_path, serena_config=self.serena_config)
        cfg.language_servers.append(LanguageServerId.TYPESCRIPT)
        cfg.save(str(self.yml_path), save_project_local_yml=False)
        assert self.registered.reload_if_changed(self.serena_config) is True
        assert self.registered.project_config.language_servers == [LanguageServerId.PYTHON, LanguageServerId.TYPESCRIPT]

    def test_picks_up_removed_language(self):
        """Editing project.yml to drop and replace a language is reflected on the next reload_if_changed call."""
        cfg = ProjectConfig.load(self.project_path, serena_config=self.serena_config)
        cfg.language_servers = [LanguageServerId.TYPESCRIPT]
        cfg.save(str(self.yml_path), save_project_local_yml=False)
        assert self.registered.reload_if_changed(self.serena_config) is True
        assert self.registered.project_config.language_servers == [LanguageServerId.TYPESCRIPT]

    def test_drops_cached_project_instance_on_change(self):
        """On a detected change, the next get_project_instance must build a fresh Project, not return the memoized one."""
        # force instantiation so we have a cached Project to invalidate
        project_first = self.registered.get_project_instance(serena_config=self.serena_config)
        # edit project.yml to change languages through the proper round-trip
        cfg = ProjectConfig.load(self.project_path, serena_config=self.serena_config)
        cfg.language_servers.append(LanguageServerId.TYPESCRIPT)
        cfg.save(str(self.yml_path), save_project_local_yml=False)
        assert self.registered.reload_if_changed(self.serena_config) is True
        project_second = self.registered.get_project_instance(serena_config=self.serena_config)
        assert project_second is not project_first
        assert project_second.project_config.language_servers == [LanguageServerId.PYTHON, LanguageServerId.TYPESCRIPT]

    def test_keeps_cached_instance_when_unchanged(self):
        """When the on-disk config is unchanged, the memoized Project instance is preserved."""
        project_first = self.registered.get_project_instance(serena_config=self.serena_config)
        assert self.registered.reload_if_changed(self.serena_config) is False
        project_second = self.registered.get_project_instance(serena_config=self.serena_config)
        assert project_second is project_first

    def test_keeps_cache_when_yml_deleted(self):
        """If project.yml has been removed, reload_if_changed reports False and preserves the cached config defensively."""
        self.yml_path.unlink()
        assert self.registered.reload_if_changed(self.serena_config) is False
        assert self.registered.project_config.language_servers == [LanguageServerId.PYTHON]

    def test_keeps_cache_when_yml_malformed(self):
        """If project.yml is malformed, reload_if_changed reports False and preserves the cached config defensively."""
        self.yml_path.write_text("this: is: not: valid: yaml: :\n", encoding="utf-8")
        assert self.registered.reload_if_changed(self.serena_config) is False
        assert self.registered.project_config.language_servers == [LanguageServerId.PYTHON]


class TestGetRegisteredProjectWithRelocatedRoot:
    """Tests that a registered project whose directory is relocated or deleted *after* registration
    does not break resolution of other projects.

    The daemon caches each registered project in memory at load time. If a project directory is then
    moved or removed during the daemon's lifetime (e.g. a repo extracted into a submodule elsewhere),
    the stale in-memory registration must not raise ``FileNotFoundError`` from
    :meth:`RegisteredProject.matches_root_path` and abort the by-path resolution loop in
    :meth:`SerenaConfig.get_registered_project`. Projects registered *after* the dead entry must still
    resolve -- the regression that broke ``activate_project`` (by path) for every project listed below
    a relocated one.
    """

    def setup_method(self):
        # three on-disk projects registered in list order -- a live one, a soon-to-be-relocated one,
        # and the target listed AFTER it (mirrors the real failure ordering: a live project, then the
        # dead entry, then the project that fails to resolve because it sits below the dead entry).
        self.serena_config = create_default_serena_config()
        self.dirs: dict[str, Path] = {}
        registered: list[RegisteredProject] = []
        for name in ("live_before", "relocated", "target_after"):
            project_dir = Path(tempfile.mkdtemp())
            self.dirs[name] = project_dir
            serena_dir = project_dir / SERENA_MANAGED_DIR_NAME
            serena_dir.mkdir(parents=True, exist_ok=True)
            (serena_dir / ProjectConfig.SERENA_PROJECT_FILE).write_text(
                f"project_name: {name}\nlanguages:\n  - python\n",
                encoding="utf-8",
            )
            registered.append(RegisteredProject.from_project_root(str(project_dir), serena_config=self.serena_config))
        self.serena_config.projects = registered

        # relocate the middle project out from under its registration: the directory is gone but the
        # in-memory RegisteredProject (with its cached project_config) survives.
        shutil.rmtree(self.dirs["relocated"])

    def teardown_method(self):
        shutil.rmtree(self.dirs["live_before"], ignore_errors=True)
        shutil.rmtree(self.dirs["target_after"], ignore_errors=True)

    def test_matches_root_path_returns_false_for_relocated_root(self):
        """A registration whose directory no longer exists matches nothing instead of raising FileNotFoundError."""
        relocated = self.serena_config.projects[1]
        assert relocated.matches_root_path(str(self.dirs["target_after"])) is False

    def test_resolves_project_listed_after_relocated_entry_by_path(self):
        """by-path resolution skips the dead entry and still finds a project registered after it."""
        resolved = self.serena_config.get_registered_project(str(self.dirs["target_after"]))
        assert resolved is not None
        assert resolved.project_name == "target_after"

    def test_resolution_by_name_unaffected_by_relocated_entry(self):
        """by-name resolution reads in-memory config and is unaffected by a relocated entry."""
        resolved = self.serena_config.get_registered_project("target_after")
        assert resolved is not None
        assert resolved.project_name == "target_after"


class TestAgentInterface:
    """Tests for the agent_interface setting (global and per project)."""

    @staticmethod
    def _project_config(agent_interface: AgentInterface | None) -> ProjectConfig:
        return ProjectConfig(project_name="test", language_servers=[LanguageServerId.PYTHON], agent_interface=agent_interface)

    def test_agent_interface_roundtrips_through_project_yaml(self):
        assert self._project_config(AgentInterface.REPL)._to_yaml_dict()["agent_interface"] == "REPL"
        assert self._project_config(None)._to_yaml_dict()["agent_interface"] is None

    def test_agent_interface_parsed_from_project_dict(self):
        data, _ = ProjectConfig._load_yaml_dict(PROJECT_TEMPLATE_FILE)
        data["project_name"] = "test"
        data["languages"] = ["python"]
        data["agent_interface"] = "repl"  # case-insensitive
        assert ProjectConfig._from_dict(data, local_override_keys=[]).agent_interface == AgentInterface.REPL
        data.pop("agent_interface")
        assert ProjectConfig._from_dict(data, local_override_keys=[]).agent_interface is None

    def test_determine_agent_interface_precedence(self):
        # default
        assert SerenaConfig().determine_agent_interface() == AgentInterface.TOOLS
        assert SerenaConfig().determine_agent_interface(self._project_config(None)) == AgentInterface.TOOLS
        # global configuration
        assert SerenaConfig(agent_interface=AgentInterface.REPL).determine_agent_interface() == AgentInterface.REPL
        # project configuration takes precedence
        config = SerenaConfig(agent_interface=AgentInterface.REPL)
        assert config.determine_agent_interface(self._project_config(AgentInterface.TOOLS)) == AgentInterface.TOOLS
        assert config.determine_agent_interface(self._project_config(None)) == AgentInterface.REPL

    def test_repl_toolset_is_fixed_and_repl_follows_project_activation(self):
        """
        In REPL mode, neither the exposed nor the active toolset is affected by tool inclusion/exclusion definitions
        (here: the project's exclusions and read-only setting), whereas the REPL's API scope follows the active project.
        """
        config, name = _make_config_with_project("test_proj")
        config.agent_interface = AgentInterface.REPL
        project_config = config.projects[0].project_config
        project_config.excluded_tools = ["initial_instructions", "serena_repl"]
        project_config.excluded_apis = ["mem"]
        project_config.read_only = True

        agent = SerenaAgent(project=None, serena_config=config)
        try:
            # before activation: the fixed toolset and the full set of facades
            fixed_toolset = {"serena_repl", "initial_instructions", "activate_project"}
            assert {t.get_name() for t in agent.get_exposed_tool_instances()} == fixed_toolset
            assert set(agent.get_active_tool_names()) == fixed_toolset
            overview = agent.get_repl().entrypoint.overview()
            assert "s.mem" in overview
            # the dashboard is disabled in the test configuration, so opening it is not offered
            assert "s.cfg" in overview and "open_dashboard" not in overview

            # after activation: the toolset is unchanged, the REPL reflects the project's API exclusions
            agent.activate_project_from_path_or_name(name)
            assert set(agent.get_active_tool_names()) == fixed_toolset
            assert "s.mem" not in agent.get_repl().entrypoint.overview()
        finally:
            agent.on_shutdown(timeout=5)


class TestProjectConfigYamlValidation:
    def test_ignored_paths_globs_starting_with_star_require_quotes(self):
        project_dir = Path(tempfile.mkdtemp())
        try:
            serena_dir = project_dir / SERENA_MANAGED_DIR_NAME
            serena_dir.mkdir(parents=True)
            (serena_dir / "project.yml").write_text('project_name: "demo"\nlanguages: ["csharp"]\nignored_paths:\n- **/bin/**\n')

            with pytest.raises(ValueError) as exc_info:
                ProjectConfig.load(project_dir, create_default_serena_config())

            msg = str(exc_info.value)
            assert "values that start with `*` must be quoted" in msg
            assert "ignored_paths" in msg
            assert '"**/bin/**"' in msg
        finally:
            shutil.rmtree(project_dir)


class TestSerenaConfigLoadSave:
    """
    Supports tests of loading/saving SerenaConfig,
    allowing to set up a temporary directory with a master config and multiple project directories.
    """

    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        """
        Sets up a temporary directory for testing, and monkeypatch SerenaConfig to use a test config file path.
        """
        self.test_dir = Path(tempfile.mkdtemp())
        self.master_config_path = self.test_dir / "serena_config.yml"
        monkeypatch.setattr(
            SerenaConfig,
            "_determine_config_file_path",
            classmethod(lambda cls: str(self.master_config_path)),
        )

    def teardown_method(self):
        shutil.rmtree(self.test_dir)

    def _make_project_dir(self, name: str, project_yml_body: str) -> Path:
        project_dir = self.test_dir / name
        (project_dir / SERENA_MANAGED_DIR_NAME).mkdir(parents=True)
        (project_dir / SERENA_MANAGED_DIR_NAME / "project.yml").write_text(project_yml_body)
        return project_dir

    def _write_master_config(self, project_paths: list[Path | str]) -> None:
        body_lines = ["projects:"]
        for p in project_paths:
            body_lines.append(f"  - {p}")
        self.master_config_path.write_text("\n".join(body_lines) + "\n")

    def test_empty_projects_key_is_treated_as_empty_list(self):
        """A bare ``projects:`` key should not abort config loading."""
        self.master_config_path.write_text("projects:\n")
        config = SerenaConfig.from_config_file(generate_if_missing=False)
        assert config.projects == []

    @pytest.mark.parametrize("setting", ["", "auth_secret: null\n", 'auth_secret: ""\n'])
    def test_unset_auth_secret_is_generated_and_persisted(self, setting: str) -> None:
        # load an existing configuration without a usable secret
        self.master_config_path.write_text("projects: []\n" + setting)
        config = SerenaConfig.from_config_file(generate_if_missing=False)

        # subsequent loads retain the generated random UUID
        assert UUID(config.auth_secret).version == 4
        assert SerenaConfig.from_config_file(generate_if_missing=False).auth_secret == config.auth_secret

    def test_configured_auth_secret_is_preserved(self) -> None:
        # retain a user-provided secret across loading and migration
        self.master_config_path.write_text("projects: []\nauth_secret: custom-secret\n")
        assert SerenaConfig.from_config_file(generate_if_missing=False).auth_secret == "custom-secret"
        assert SerenaConfig.from_config_file(generate_if_missing=False).auth_secret == "custom-secret"

    @pytest.mark.parametrize("value", [True, False])
    def test_save_dashboard_launch_preference(self, value: bool) -> None:
        # load a configuration and apply a transient override
        self.master_config_path.write_text("projects: []\nweb_dashboard_open_on_launch: null\nlog_level: 20\n")
        config = SerenaConfig.from_config_file(generate_if_missing=False)
        assert config.web_dashboard_open_on_launch is None
        config.log_level = 10

        # persist only the launch preference and retain other settings
        config.save_web_dashboard_open_on_launch(value)
        reloaded = SerenaConfig.from_config_file(generate_if_missing=False)
        assert config.web_dashboard_open_on_launch is value
        assert reloaded.web_dashboard_open_on_launch is value
        assert reloaded.log_level == 20

    def test_new_config_has_persistent_auth_secret(self) -> None:
        # generate the configuration from the template and retain its secret
        config = SerenaConfig.from_config_file()
        assert UUID(config.auth_secret).version == 4
        assert SerenaConfig.from_config_file().auth_secret == config.auth_secret

    def test_direct_config_instances_have_distinct_auth_secrets(self) -> None:
        # directly constructed configurations receive independent secrets
        first = SerenaConfig()
        second = SerenaConfig()
        assert UUID(first.auth_secret).version == 4
        assert UUID(second.auth_secret).version == 4
        assert first.auth_secret != second.auth_secret

    def test_malformed_project_is_skipped_with_warning(self, caplog):
        """A malformed project.yml must not abort loading of the others."""
        good_project = self._make_project_dir(
            "good_project",
            'project_name: "good_project"\nlanguages: ["python"]\n',
        )
        # Invalid YAML: a stray colon at the start of a mapping value.
        bad_project = self._make_project_dir(
            "bad_project",
            ": this is not : valid : yaml :\n",
        )
        self._write_master_config([good_project, bad_project])

        with caplog.at_level(logging.ERROR):
            config = SerenaConfig.from_config_file(generate_if_missing=False)

        registered_roots = {Path(p.project_root).resolve() for p in config.projects}
        assert registered_roots == {good_project.resolve()}, f"Expected only the good project to be registered, got {registered_roots}"
        assert any("Failed to load project configuration" in msg and str(bad_project.resolve()) in msg for msg in caplog.messages), (
            f"Expected a warning naming {bad_project.resolve()}, got: {caplog.messages}"
        )

    def test_alias_like_ignored_path_error_is_logged_with_hint(self, caplog):
        good_project = self._make_project_dir(
            "good_project",
            'project_name: "good_project"\nlanguages: ["python"]\n',
        )
        bad_project = self._make_project_dir(
            "bad_project",
            'project_name: "bad_project"\nlanguages: ["csharp"]\nignored_paths:\n- **/bin/**\n',
        )
        self._write_master_config([good_project, bad_project])

        with caplog.at_level(logging.ERROR):
            config = SerenaConfig.from_config_file(generate_if_missing=False)

        registered_roots = {Path(p.project_root).resolve() for p in config.projects}
        assert registered_roots == {good_project.resolve()}
        assert any("must be quoted" in msg and str(bad_project.resolve()) in msg for msg in caplog.messages), caplog.messages

    def test_parallel_agents_changing_project_lists(self):
        """
        Tests that agents changing SerenaConfig by adding projects in parallel do not lose each other's changes.
        """
        p1 = self._make_project_dir("project1", 'project_name: "project1"\nlanguages: ["python"]\n')
        p2 = self._make_project_dir("project2", 'project_name: "project1"\nlanguages: ["python"]\n')
        p3 = self._make_project_dir("project3", 'project_name: "project1"\nlanguages: ["python"]\n')
        p4 = self._make_project_dir("project4", 'project_name: "project1"\nlanguages: ["python"]\n')

        self._write_master_config([p1, p2])

        config1 = SerenaConfig.from_config_file(generate_if_missing=False)
        config2 = SerenaConfig.from_config_file(generate_if_missing=False)

        config1.add_project_from_path(p3)
        config2.add_project_from_path(p4)

        resulting_config = SerenaConfig.from_config_file(generate_if_missing=False)

        assert len(resulting_config.projects) == 4

    def test_remove_project_persists_across_reload(self):
        """Removing a project must update the on-disk project registry."""
        p1 = self._make_project_dir("project1", 'project_name: "project1"\nlanguages: ["python"]\n')
        p2 = self._make_project_dir("project2", 'project_name: "project2"\nlanguages: ["python"]\n')
        self._write_master_config([p1, p2])

        config = SerenaConfig.from_config_file(generate_if_missing=False)
        config.remove_project("project2")

        reloaded = SerenaConfig.from_config_file(generate_if_missing=False)
        assert [project.project_config.project_name for project in reloaded.projects] == ["project1"]

    def test_remove_project_preserves_concurrent_addition(self):
        """A removal must not discard a project added by another config instance."""
        p1 = self._make_project_dir("project1", 'project_name: "project1"\nlanguages: ["python"]\n')
        p2 = self._make_project_dir("project2", 'project_name: "project2"\nlanguages: ["python"]\n')
        p3 = self._make_project_dir("project3", 'project_name: "project3"\nlanguages: ["python"]\n')
        self._write_master_config([p1, p2])

        removing_config = SerenaConfig.from_config_file(generate_if_missing=False)
        adding_config = SerenaConfig.from_config_file(generate_if_missing=False)
        adding_config.add_project_from_path(p3)
        removing_config.remove_project("project2")

        reloaded = SerenaConfig.from_config_file(generate_if_missing=False)
        assert {project.project_config.project_name for project in reloaded.projects} == {"project1", "project3"}

    def test_add_project_preserves_concurrent_removal(self):
        """An addition must not resurrect a project removed by another config instance."""
        p1 = self._make_project_dir("project1", 'project_name: "project1"\nlanguages: ["python"]\n')
        p2 = self._make_project_dir("project2", 'project_name: "project2"\nlanguages: ["python"]\n')
        p3 = self._make_project_dir("project3", 'project_name: "project3"\nlanguages: ["python"]\n')
        self._write_master_config([p1, p2])

        removing_config = SerenaConfig.from_config_file(generate_if_missing=False)
        adding_config = SerenaConfig.from_config_file(generate_if_missing=False)
        removing_config.remove_project("project2")
        adding_config.add_project_from_path(p3)

        reloaded = SerenaConfig.from_config_file(generate_if_missing=False)
        assert {project.project_config.project_name for project in reloaded.projects} == {"project1", "project3"}

    def test_persist_projects_serializes_overlapping_reads_and_writes(self, monkeypatch):
        """A write started before another process's write must not clobber it with a stale read.

        The two tests above call ``remove_project``/``add_project_from_path`` back to back, so
        by the time the second one reads the disk copy, the first one has already finished
        writing it; there is no actual overlap between the two. Here the first call's disk read
        is held open on a background thread until a second, independent call has fully read,
        merged and written its own change, reproducing the interleaving that two concurrent
        agent processes can hit.
        """
        p1 = self._make_project_dir("project1", 'project_name: "project1"\nlanguages: ["python"]\n')
        p2 = self._make_project_dir("project2", 'project_name: "project2"\nlanguages: ["python"]\n')
        p3 = self._make_project_dir("project3", 'project_name: "project3"\nlanguages: ["python"]\n')
        self._write_master_config([p1, p2])

        adding_config = SerenaConfig.from_config_file(generate_if_missing=False)
        removing_config = SerenaConfig.from_config_file(generate_if_missing=False)
        adding_config.projects.append(RegisteredProject.from_project_root(p3, serena_config=adding_config))
        for i, project in enumerate(list(removing_config.projects)):
            if project.project_name == "project2":
                del removing_config.projects[i]
                break

        real_from_config_file = SerenaConfig.from_config_file.__func__
        first_read_started = threading.Event()
        first_call_may_write = threading.Event()
        call_count = {"n": 0}

        def paused_from_config_file(cls: type[SerenaConfig], *args: Any, **kwargs: Any) -> SerenaConfig:
            call_count["n"] += 1
            result = real_from_config_file(cls, *args, **kwargs)
            if call_count["n"] == 1:
                first_read_started.set()
                assert first_call_may_write.wait(timeout=5), "test never released the paused first call"
            return result

        monkeypatch.setattr(SerenaConfig, "from_config_file", classmethod(paused_from_config_file))

        adder_thread = threading.Thread(target=adding_config._persist_projects)
        adder_thread.start()
        assert first_read_started.wait(timeout=5), "adding_config never reached its disk read"

        # Run the second call on its own thread too: with the fix, it blocks acquiring the same
        # lock the paused first call is still holding, so calling it inline here would deadlock.
        # Do not use a sleep to guess whether it got a chance to run: join with a timeout instead,
        # so the outcome depends on the lock actually blocking it, not on scheduler luck. Without
        # the fix, nothing blocks it and it always finishes well inside the timeout; with the fix,
        # it is still blocked on the lock the paused first call holds, so it never does.
        remover_thread = threading.Thread(target=removing_config._persist_projects)
        remover_thread.start()
        remover_thread.join(timeout=1)

        first_call_may_write.set()
        adder_thread.join(timeout=5)
        remover_thread.join(timeout=5)
        assert not adder_thread.is_alive()
        assert not remover_thread.is_alive()

        reloaded = SerenaConfig.from_config_file(generate_if_missing=False)
        assert {project.project_config.project_name for project in reloaded.projects} == {"project1", "project3"}


class TestGetRegisteredProjectWithDanglingProject:
    """A registered project whose root directory was deleted (e.g. a removed git
    worktree) must not break lookup/activation of other, valid projects.

    Reproduces the bug where ``get_registered_project`` iterates over every
    registered project and ``RegisteredProject.matches_root_path`` raises
    ``FileNotFoundError`` for the dangling project, aborting the whole lookup.
    """

    def setup_method(self):
        self.test_dir = Path(tempfile.mkdtemp())

    def teardown_method(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _make_project_dir(self, name: str) -> Path:
        project_dir = self.test_dir / name
        (project_dir / SERENA_MANAGED_DIR_NAME).mkdir(parents=True)
        (project_dir / SERENA_MANAGED_DIR_NAME / "project.yml").write_text(f'project_name: "{name}"\nlanguages: ["python"]\n')
        return project_dir

    def test_dangling_project_does_not_break_lookup_of_valid_project(self):
        config = create_default_serena_config()
        dangling_dir = self._make_project_dir("dangling_project")
        valid_dir = self._make_project_dir("valid_project")

        # Register the dangling project FIRST so the path-matching loop hits the
        # deleted directory before reaching the valid project.
        config.projects = [
            RegisteredProject.from_project_root(dangling_dir, serena_config=config),
            RegisteredProject.from_project_root(valid_dir, serena_config=config),
        ]

        # Delete the dangling project's directory out from under Serena.
        shutil.rmtree(dangling_dir)

        # Looking up the valid project by path must succeed, not raise
        # FileNotFoundError on the deleted dangling project's path.
        result = config.get_registered_project(str(valid_dir))
        assert result is not None
        assert result.project_name == "valid_project"


class TestProjectConfigActivationCommand:
    """Tests for the activation_command and activation_command_timeout fields."""

    def _base_data(self) -> dict:
        data, _ = ProjectConfig._load_yaml_dict(PROJECT_TEMPLATE_FILE)
        data["project_name"] = "test"
        data["languages"] = ["python"]
        return data

    def test_activation_command_defaults_to_none(self):
        config = ProjectConfig(project_name="test", language_servers=[LanguageServerId.PYTHON])
        assert config.activation_command is None

    def test_activation_command_timeout_default(self):
        config = ProjectConfig(project_name="test", language_servers=[LanguageServerId.PYTHON])
        assert config.activation_command_timeout == 180.0

    def test_activation_command_parsed_from_dict(self):
        data = self._base_data()
        data["activation_command"] = "npx nx run-many -t build"
        data["activation_command_timeout"] = 300
        config = ProjectConfig._from_dict(data, local_override_keys=[])
        assert config.activation_command == "npx nx run-many -t build"
        assert config.activation_command_timeout == 300.0

    def test_activation_command_defaults_when_absent(self):
        data = self._base_data()
        data.pop("activation_command", None)
        data.pop("activation_command_timeout", None)
        config = ProjectConfig._from_dict(data, local_override_keys=[])
        assert config.activation_command is None
        assert config.activation_command_timeout == 180.0

    def test_activation_command_timeout_zero_raises(self):
        data = self._base_data()
        data["activation_command"] = "echo hi"
        data["activation_command_timeout"] = 0
        with pytest.raises(ValueError, match="activation_command_timeout must be positive"):
            ProjectConfig._from_dict(data, local_override_keys=[])

    def test_activation_command_timeout_negative_raises(self):
        data = self._base_data()
        data["activation_command"] = "echo hi"
        data["activation_command_timeout"] = -10
        with pytest.raises(ValueError, match="activation_command_timeout must be positive"):
            ProjectConfig._from_dict(data, local_override_keys=[])


class TestTrustedProjectPathPatterns:
    """Pins the trust semantics that `serena_config.template.yml` documents by example."""

    @staticmethod
    def _config(*patterns: str) -> SerenaConfig:
        return SerenaConfig(
            gui_log_window=False,
            web_dashboard=False,
            trusted_project_path_patterns=list(patterns),
        )

    def test_bare_project_root_trusts_that_project(self):
        """The documented way to trust a single project: its root path, without a trailing glob."""
        root = "/opt/dev/projects/my_trusted_project"
        assert self._config(root).is_trusted_project_path(root)

    def test_project_root_with_trailing_glob_trusts_nothing(self):
        """`<root>/**` matches only paths below the root, and trust is decided by the root itself.

        This is why the template documents the bare form; the difference is invisible otherwise.
        """
        root = "/opt/dev/projects/my_trusted_project"
        assert not self._config(root + "/**").is_trusted_project_path(root)

    def test_parent_directory_glob_trusts_projects_below_it(self):
        parent = self._config("/home/user/projects/**")
        assert parent.is_trusted_project_path("/home/user/projects/some_project")
        assert not parent.is_trusted_project_path("/home/user/projects")

    def test_unrelated_path_is_not_trusted(self):
        """Control: the patterns above are not vacuously true."""
        assert not self._config("/opt/dev/projects/my_trusted_project", "/home/user/projects/**").is_trusted_project_path("/somewhere/else")
