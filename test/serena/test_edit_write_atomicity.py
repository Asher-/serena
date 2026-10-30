"""
Tests for pg://serena/edit/write (task://edit_atomicity.1): an edit writes its file's new contents to a temporary file in the
file's directory and renames it over the file, so an edit whose new contents cannot be encoded leaves the file's bytes, its
directory and the language server's view of the file as they were (bug://serena/write/edit/atomicity/reanchor).
"""

import os
import shutil
import stat
from pathlib import Path

import pytest

from serena.code_editor import LanguageServerCodeEditor
from serena.symbol import LanguageServerSymbolRetriever
from serena.util.file_lifecycle import AtomicFileWriter
from solidlsp.ls_config import Language
from test.conftest import get_repo_path, project_with_ls_context
from test.serena.test_cursor_container_edit import _make_manager_via_subclass

PYTHON_EDITED_FILE = os.path.join("test_repo", "variables.py")
"""the file of the Python test repository whose symbol body the language-server edit replaces"""

PYTHON_EDITED_SYMBOL = "VariableContainer/modify_instance_var"
"""the name path of the symbol whose body the language-server edit replaces"""

BODY_UNENCODABLE_IN_ASCII = """def modify_instance_var(self):
        self.instance_var = "☃"
"""
"""a replacement body holding a snowman, a character the ascii codec cannot encode"""

JSON_SOURCE = '{\n    "members": {\n        "existing": 1\n    }\n}\n'
"""the JSON document the structural edits are applied to"""


class TestLanguageServerEditFailedSave:
    """A language-server symbol edit whose new contents the project's encoding cannot represent."""

    @pytest.mark.python
    def test_unencodable_body_leaves_file_directory_and_buffer_as_they_were(self, work_dir: Path) -> None:
        # a copy of the Python test repository, and the edited file's state before the edit
        repo_path = work_dir / get_repo_path(Language.PYTHON).name
        shutil.copytree(get_repo_path(Language.PYTHON), repo_path)
        edited_path = repo_path / PYTHON_EDITED_FILE
        bytes_before = edited_path.read_bytes()
        listing_before = sorted(os.listdir(edited_path.parent))

        with project_with_ls_context(Language.PYTHON, str(repo_path)) as project:
            # a project whose encoding cannot represent the new body
            project.project_config.encoding = "ascii"
            retriever = LanguageServerSymbolRetriever(project)
            editor = LanguageServerCodeEditor(retriever)

            # the edit, which must fail at the save
            with pytest.raises(UnicodeEncodeError):
                editor.replace_body(PYTHON_EDITED_SYMBOL, PYTHON_EDITED_FILE, BODY_UNENCODABLE_IN_ASCII)

            # the file as the language server now presents it
            with retriever.get_language_server(PYTHON_EDITED_FILE).open_file(PYTHON_EDITED_FILE) as file_buffer:
                buffer_contents = file_buffer.contents

        assert edited_path.read_bytes() == bytes_before
        assert sorted(os.listdir(edited_path.parent)) == listing_before
        assert buffer_contents == bytes_before.decode("utf-8")


class TestStructuralEditWrite:
    """A structural container-member edit written by CursorManager.apply_container_edit."""

    def test_unencodable_value_leaves_file_and_directory_as_they_were(self, work_dir: Path) -> None:
        # the edited file's state before the edit
        edited_path = work_dir / "doc.json"
        edited_path.write_text(JSON_SOURCE, encoding="utf-8")
        bytes_before = edited_path.read_bytes()
        listing_before = sorted(os.listdir(work_dir))

        # a project whose encoding cannot represent the new value
        manager = _make_manager_via_subclass(work_dir)
        manager._project.project_config.encoding = "ascii"  # type: ignore[attr-defined]
        cursor_id, _ = manager.start_cursor(name_path="members/existing", relative_path="doc.json")

        with pytest.raises(UnicodeEncodeError):
            manager.apply_container_edit(cursor_id, "replace", '"☃"')

        assert edited_path.read_bytes() == bytes_before
        assert sorted(os.listdir(work_dir)) == listing_before

    def test_lone_surrogate_value_leaves_file_and_directory_as_they_were(self, work_dir: Path) -> None:
        # the edited file's state before the edit
        edited_path = work_dir / "doc.json"
        edited_path.write_text(JSON_SOURCE, encoding="utf-8")
        bytes_before = edited_path.read_bytes()
        listing_before = sorted(os.listdir(work_dir))

        # a project in its own utf-8 encoding, which cannot encode a lone surrogate
        manager = _make_manager_via_subclass(work_dir)
        cursor_id, _ = manager.start_cursor(name_path="members/existing", relative_path="doc.json")

        with pytest.raises(UnicodeEncodeError):
            manager.apply_container_edit(cursor_id, "replace", '"\ud800"')

        assert edited_path.read_bytes() == bytes_before
        assert sorted(os.listdir(work_dir)) == listing_before

    def test_edit_keeps_permission_bits(self, work_dir: Path) -> None:
        """Regression guard: replacing the file by a renamed temporary file must not drop its mode."""
        edited_path = work_dir / "doc.json"
        edited_path.write_text(JSON_SOURCE, encoding="utf-8")
        os.chmod(edited_path, 0o755)
        manager = _make_manager_via_subclass(work_dir)
        cursor_id, _ = manager.start_cursor(name_path="members/existing", relative_path="doc.json")

        manager.apply_container_edit(cursor_id, "replace", "42")

        assert '"existing": 42' in edited_path.read_text(encoding="utf-8")
        assert stat.S_IMODE(edited_path.stat().st_mode) == 0o755

    def test_edit_in_crlf_project_writes_crlf(self, work_dir: Path) -> None:
        """Regression guard: encoding the contents before the write must translate line endings as the text-mode write did."""
        edited_path = work_dir / "doc.json"
        edited_path.write_text(JSON_SOURCE, encoding="utf-8")
        manager = _make_manager_via_subclass(work_dir)
        manager._project.line_ending.newline_str = "\r\n"  # type: ignore[attr-defined]
        cursor_id, _ = manager.start_cursor(name_path="members/existing", relative_path="doc.json")

        manager.apply_container_edit(cursor_id, "replace", "42")

        raw = edited_path.read_bytes()
        assert b'"existing": 42\r\n' in raw
        assert b"\n" not in raw.replace(b"\r\n", b"")

    def test_edit_through_a_symlink_writes_the_file_it_points_at(self, work_dir: Path) -> None:
        """Regression guard: an edit of a file reached through a symlink replaces the file the link points at, as the text-mode write did, keeping the link and the file's mode."""
        target_path = work_dir / "target.json"
        target_path.write_text(JSON_SOURCE, encoding="utf-8")
        os.chmod(target_path, 0o755)
        link_path = work_dir / "doc.json"
        link_path.symlink_to(target_path.name)
        manager = _make_manager_via_subclass(work_dir)
        cursor_id, _ = manager.start_cursor(name_path="members/existing", relative_path="doc.json")

        manager.apply_container_edit(cursor_id, "replace", "42")

        assert link_path.is_symlink()
        assert '"existing": 42' in target_path.read_text(encoding="utf-8")
        assert stat.S_IMODE(target_path.stat().st_mode) == 0o755
        assert sorted(os.listdir(work_dir)) == ["doc.json", "target.json"]


class TestAtomicFileWriter:
    """The writer every edit replaces its file through."""

    def test_write_to_a_path_without_a_directory_component(self, work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression guard: a target named without a directory is written in the working directory, leaving no temporary file."""
        monkeypatch.chdir(work_dir)

        AtomicFileWriter().write_bytes("doc.json", JSON_SOURCE.encode("utf-8"))

        assert (work_dir / "doc.json").read_bytes() == JSON_SOURCE.encode("utf-8")
        assert sorted(os.listdir(work_dir)) == ["doc.json"]
