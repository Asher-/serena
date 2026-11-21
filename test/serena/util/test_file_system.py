import contextlib
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NoReturn
from unittest import mock

import pytest

# Assuming the gitignore parser code is in a module named 'gitignore_parser'
from pathspec import PathSpec

from serena.util import file_system
from serena.util.file_system import GitignoreParser, GitignoreSpec, _escape_gitignore_path_component, match_path, write_file_atomic


class TestGitignoreParser:
    """Test class for GitignoreParser functionality."""

    def setup_method(self):
        """Set up test environment before each test method."""
        # Create a temporary directory for testing
        self.test_dir = tempfile.mkdtemp()
        self.repo_path = Path(self.test_dir)

        # Create test repository structure
        self._create_repo_structure()

    def teardown_method(self):
        """Clean up test environment after each test method."""
        # Remove the temporary directory
        shutil.rmtree(self.test_dir)

    def _create_repo_structure(self):
        """
        Create a test repository structure with multiple gitignore files.

        Structure:
        repo/
        ├── .gitignore
        ├── file1.txt
        ├── test.log
        ├── src/
        │   ├── .gitignore
        │   ├── main.py
        │   ├── test.log
        │   ├── build/
        │   │   └── output.o
        │   └── lib/
        │       ├── .gitignore
        │       └── cache.tmp
        └── docs/
            ├── .gitignore
            ├── api.md
            └── temp/
                └── draft.md
        """
        # Create directories
        (self.repo_path / "src").mkdir()
        (self.repo_path / "src" / "build").mkdir()
        (self.repo_path / "src" / "lib").mkdir()
        (self.repo_path / "docs").mkdir()
        (self.repo_path / "docs" / "temp").mkdir()

        # Create files
        (self.repo_path / "file1.txt").touch()
        (self.repo_path / "test.log").touch()
        (self.repo_path / "src" / "main.py").touch()
        (self.repo_path / "src" / "test.log").touch()
        (self.repo_path / "src" / "build" / "output.o").touch()
        (self.repo_path / "src" / "lib" / "cache.tmp").touch()
        (self.repo_path / "docs" / "api.md").touch()
        (self.repo_path / "docs" / "temp" / "draft.md").touch()

        # Create root .gitignore
        root_gitignore = self.repo_path / ".gitignore"
        root_gitignore.write_text(
            """# Root gitignore
*.log
/build/
"""
        )

        # Create src/.gitignore
        src_gitignore = self.repo_path / "src" / ".gitignore"
        src_gitignore.write_text(
            """# Source gitignore
*.o
build/
!important.log
"""
        )

        # Create src/lib/.gitignore (deeply nested)
        src_lib_gitignore = self.repo_path / "src" / "lib" / ".gitignore"
        src_lib_gitignore.write_text(
            """# Library gitignore
*.tmp
*.cache
"""
        )

        # Create docs/.gitignore
        docs_gitignore = self.repo_path / "docs" / ".gitignore"
        docs_gitignore.write_text(
            """# Docs gitignore
temp/
*.tmp
"""
        )

    def test_initialization(self):
        """Constructing a parser lists no directory and reads no .gitignore."""
        with (
            _no_directory_listing(),
            mock.patch("serena.util.file_system.open", side_effect=AssertionError("a file was read"), create=True),
        ):
            parser = GitignoreParser(str(self.repo_path))

        assert parser.repo_root == str(self.repo_path.absolute())

    def test_find_gitignore_files(self):
        """The .gitignore files governing a path are those on its ancestor chain, including deeply nested ones, root first."""
        parser = GitignoreParser(str(self.repo_path))

        def governing(path: str) -> list[str]:
            return [os.path.relpath(spec.file_path, self.repo_path) for spec in parser.specs_of(path)]

        with _no_directory_listing():
            assert governing("src/lib/cache.tmp") == [
                ".gitignore",
                os.path.join("src", ".gitignore"),
                os.path.join("src", "lib", ".gitignore"),
            ]
            assert governing("docs/api.md") == [".gitignore", os.path.join("docs", ".gitignore")]
            assert governing("file1.txt") == [".gitignore"]

    def test_parse_patterns_root_directory(self):
        """Test parsing gitignore patterns in root directory."""
        # Create a simple test case with only root gitignore
        test_dir = self.repo_path / "test_root"
        test_dir.mkdir()

        gitignore = test_dir / ".gitignore"
        gitignore.write_text(
            """*.log
build/
/temp.txt
"""
        )

        parser = GitignoreParser(str(test_dir))
        specs = parser.specs_of("file.txt")

        assert len(specs) == 1
        patterns = specs[0].patterns

        assert "*.log" in patterns
        assert "build/" in patterns
        assert "/temp.txt" in patterns

    def test_parse_patterns_subdirectory(self):
        """Test parsing gitignore patterns in subdirectory."""
        # Create a test case with subdirectory gitignore
        test_dir = self.repo_path / "test_sub"
        test_dir.mkdir()
        subdir = test_dir / "src"
        subdir.mkdir()

        gitignore = subdir / ".gitignore"
        gitignore.write_text(
            """*.o
/build/
test.log
"""
        )

        parser = GitignoreParser(str(test_dir))
        specs = parser.specs_of("src/file.txt")

        assert len(specs) == 1
        patterns = specs[0].patterns

        # Non-anchored pattern should get ** prefix
        assert "src/**/*.o" in patterns
        # Anchored pattern should not get ** prefix
        assert "src/build/" in patterns
        # Non-anchored pattern without slash
        assert "src/**/test.log" in patterns

    def test_should_ignore_root_patterns(self):
        """Test ignoring files based on root .gitignore."""
        parser = GitignoreParser(str(self.repo_path))

        # Files that should be ignored
        assert parser.should_ignore("test.log")
        assert parser.should_ignore(str(self.repo_path / "test.log"))

        # Files that should NOT be ignored
        assert not parser.should_ignore("file1.txt")
        assert not parser.should_ignore("src/main.py")

    def test_match_path_root_directory(self):
        """Root directory should never be ignored by pathspec patterns."""
        spec = PathSpec.from_lines("gitwildmatch", ["/.*/"])

        assert not match_path(".", spec, root_path=str(self.repo_path))
        assert not match_path("", spec, root_path=str(self.repo_path))

    def test_should_ignore_subdirectory_patterns(self):
        """Test ignoring files based on subdirectory .gitignore files."""
        parser = GitignoreParser(str(self.repo_path))

        # .o files in src should be ignored
        assert parser.should_ignore("src/build/output.o")

        # build/ directory in src should be ignored
        assert parser.should_ignore("src/build/")

        # temp/ directory in docs should be ignored
        assert parser.should_ignore("docs/temp/draft.md")

        # But temp/ outside docs should not be ignored by docs/.gitignore
        assert not parser.should_ignore("temp/file.txt")

        # Test deeply nested .gitignore in src/lib/
        # .tmp files in src/lib should be ignored
        assert parser.should_ignore("src/lib/cache.tmp")

        # .cache files in src/lib should also be ignored
        assert parser.should_ignore("src/lib/data.cache")

        # But .tmp files outside src/lib should not be ignored by src/lib/.gitignore
        assert not parser.should_ignore("src/other.tmp")

    def test_anchored_vs_non_anchored_patterns(self):
        """Test the difference between anchored and non-anchored patterns."""
        # Create new test structure
        test_dir = self.repo_path / "test_anchored"
        test_dir.mkdir()
        (test_dir / "src").mkdir()
        (test_dir / "src" / "subdir").mkdir()
        (test_dir / "src" / "subdir" / "deep").mkdir()

        # Create src/.gitignore with both anchored and non-anchored patterns
        gitignore = test_dir / "src" / ".gitignore"
        gitignore.write_text(
            """/temp.txt
data.json
"""
        )

        # Create test files
        (test_dir / "src" / "temp.txt").touch()
        (test_dir / "src" / "data.json").touch()
        (test_dir / "src" / "subdir" / "temp.txt").touch()
        (test_dir / "src" / "subdir" / "data.json").touch()
        (test_dir / "src" / "subdir" / "deep" / "data.json").touch()

        parser = GitignoreParser(str(test_dir))

        # Anchored pattern /temp.txt should only match in src/
        assert parser.should_ignore("src/temp.txt")
        assert not parser.should_ignore("src/subdir/temp.txt")

        # Non-anchored pattern data.json should match anywhere under src/
        assert parser.should_ignore("src/data.json")
        assert parser.should_ignore("src/subdir/data.json")
        assert parser.should_ignore("src/subdir/deep/data.json")

    def test_root_anchored_patterns(self):
        """Test anchored patterns in root .gitignore only match root-level files."""
        # Create new test structure for root anchored patterns
        test_dir = self.repo_path / "test_root_anchored"
        test_dir.mkdir()
        (test_dir / "src").mkdir()
        (test_dir / "docs").mkdir()
        (test_dir / "src" / "nested").mkdir()

        # Create root .gitignore with anchored patterns
        gitignore = test_dir / ".gitignore"
        gitignore.write_text(
            """/config.json
/temp.log
/build
*.pyc
"""
        )

        # Create test files at root level
        (test_dir / "config.json").touch()
        (test_dir / "temp.log").touch()
        (test_dir / "build").mkdir()
        (test_dir / "file.pyc").touch()

        # Create same-named files in subdirectories
        (test_dir / "src" / "config.json").touch()
        (test_dir / "src" / "temp.log").touch()
        (test_dir / "src" / "build").mkdir()
        (test_dir / "src" / "file.pyc").touch()
        (test_dir / "docs" / "config.json").touch()
        (test_dir / "docs" / "temp.log").touch()
        (test_dir / "src" / "nested" / "config.json").touch()
        (test_dir / "src" / "nested" / "temp.log").touch()
        (test_dir / "src" / "nested" / "build").mkdir()

        parser = GitignoreParser(str(test_dir))

        # Anchored patterns should only match root-level files
        assert parser.should_ignore("config.json")
        assert not parser.should_ignore("src/config.json")
        assert not parser.should_ignore("docs/config.json")
        assert not parser.should_ignore("src/nested/config.json")

        assert parser.should_ignore("temp.log")
        assert not parser.should_ignore("src/temp.log")
        assert not parser.should_ignore("docs/temp.log")
        assert not parser.should_ignore("src/nested/temp.log")

        assert parser.should_ignore("build")
        assert not parser.should_ignore("src/build")
        assert not parser.should_ignore("src/nested/build")

        # Non-anchored patterns should match everywhere
        assert parser.should_ignore("file.pyc")
        assert parser.should_ignore("src/file.pyc")

    def test_mixed_anchored_and_non_anchored_root_patterns(self):
        """Test mix of anchored and non-anchored patterns in root .gitignore."""
        test_dir = self.repo_path / "test_mixed_patterns"
        test_dir.mkdir()
        (test_dir / "app").mkdir()
        (test_dir / "tests").mkdir()
        (test_dir / "app" / "modules").mkdir()

        # Create root .gitignore with mixed patterns
        gitignore = test_dir / ".gitignore"
        gitignore.write_text(
            """/secrets.env
/dist/
node_modules/
*.tmp
/app/local.config
debug.log
"""
        )

        # Create test files and directories
        (test_dir / "secrets.env").touch()
        (test_dir / "dist").mkdir()
        (test_dir / "node_modules").mkdir()
        (test_dir / "file.tmp").touch()
        (test_dir / "app" / "local.config").touch()
        (test_dir / "debug.log").touch()

        # Create same files in subdirectories
        (test_dir / "app" / "secrets.env").touch()
        (test_dir / "app" / "dist").mkdir()
        (test_dir / "app" / "node_modules").mkdir()
        (test_dir / "app" / "file.tmp").touch()
        (test_dir / "app" / "debug.log").touch()
        (test_dir / "tests" / "secrets.env").touch()
        (test_dir / "tests" / "node_modules").mkdir()
        (test_dir / "tests" / "debug.log").touch()
        (test_dir / "app" / "modules" / "local.config").touch()

        parser = GitignoreParser(str(test_dir))

        # Anchored patterns should only match at root
        assert parser.should_ignore("secrets.env")
        assert not parser.should_ignore("app/secrets.env")
        assert not parser.should_ignore("tests/secrets.env")

        assert parser.should_ignore("dist")
        assert not parser.should_ignore("app/dist")

        assert parser.should_ignore("app/local.config")
        assert not parser.should_ignore("app/modules/local.config")

        # Non-anchored patterns should match everywhere
        assert parser.should_ignore("node_modules")
        assert parser.should_ignore("app/node_modules")
        assert parser.should_ignore("tests/node_modules")

        assert parser.should_ignore("file.tmp")
        assert parser.should_ignore("app/file.tmp")

        assert parser.should_ignore("debug.log")
        assert parser.should_ignore("app/debug.log")
        assert parser.should_ignore("tests/debug.log")

    def test_negation_patterns(self):
        """Test negation patterns are parsed correctly."""
        test_dir = self.repo_path / "test_negation"
        test_dir.mkdir()

        gitignore = test_dir / ".gitignore"
        gitignore.write_text(
            """*.log
!important.log
!src/keep.log
"""
        )

        parser = GitignoreParser(str(test_dir))
        specs = parser.specs_of("file.txt")

        assert len(specs) == 1
        patterns = specs[0].patterns

        assert "*.log" in patterns
        assert "!important.log" in patterns
        assert "!src/keep.log" in patterns

    def test_comments_and_empty_lines(self):
        """Test that comments and empty lines are ignored."""
        test_dir = self.repo_path / "test_comments"
        test_dir.mkdir()

        gitignore = test_dir / ".gitignore"
        gitignore.write_text(
            """# This is a comment
*.log

# Another comment
  # Indented comment

build/
"""
        )

        parser = GitignoreParser(str(test_dir))
        specs = parser.specs_of("file.txt")

        assert len(specs) == 1
        patterns = specs[0].patterns

        assert len(patterns) == 2
        assert "*.log" in patterns
        assert "build/" in patterns

    def test_escaped_characters(self):
        """Test escaped special characters."""
        test_dir = self.repo_path / "test_escaped"
        test_dir.mkdir()

        gitignore = test_dir / ".gitignore"
        gitignore.write_text(
            """\\#not-a-comment.txt
\\!not-negation.txt
"""
        )

        parser = GitignoreParser(str(test_dir))
        specs = parser.specs_of("file.txt")

        assert len(specs) == 1
        patterns = specs[0].patterns

        assert "#not-a-comment.txt" in patterns
        assert "!not-negation.txt" in patterns

    def test_escaped_negation_patterns(self):
        test_dir = self.repo_path / "test_escaped_negation"
        test_dir.mkdir()

        gitignore = test_dir / ".gitignore"
        gitignore.write_text(
            """*.log
\\!not-negation.log
!actual-negation.log
"""
        )

        parser = GitignoreParser(str(test_dir))
        specs = parser.specs_of("file.txt")

        assert len(specs) == 1
        patterns = specs[0].patterns

        # Key assertions: escaped exclamation becomes literal, real negation preserved
        assert "!not-negation.log" in patterns  # escaped -> literal
        assert "!actual-negation.log" in patterns  # real negation preserved

        # Test the actual behavioral difference between escaped and real negation:
        # *.log pattern should ignore test.log
        assert parser.should_ignore("test.log")

        # Escaped negation file should still be ignored by *.log pattern
        assert parser.should_ignore("!not-negation.log")

        # Actual negation should override the *.log pattern
        assert not parser.should_ignore("actual-negation.log")

    def test_glob_patterns(self):
        """Test various glob patterns work correctly."""
        test_dir = self.repo_path / "test_glob"
        test_dir.mkdir()

        gitignore = test_dir / ".gitignore"
        gitignore.write_text(
            """*.pyc
**/*.tmp
src/*.o
!src/important.o
[Tt]est*
"""
        )

        # Create test files
        (test_dir / "src").mkdir()
        (test_dir / "src" / "nested").mkdir()
        (test_dir / "file.pyc").touch()
        (test_dir / "src" / "file.pyc").touch()
        (test_dir / "file.tmp").touch()
        (test_dir / "src" / "nested" / "file.tmp").touch()
        (test_dir / "src" / "file.o").touch()
        (test_dir / "src" / "important.o").touch()
        (test_dir / "Test.txt").touch()
        (test_dir / "test.log").touch()

        parser = GitignoreParser(str(test_dir))

        # *.pyc should match everywhere
        assert parser.should_ignore("file.pyc")
        assert parser.should_ignore("src/file.pyc")

        # **/*.tmp should match all .tmp files
        assert parser.should_ignore("file.tmp")
        assert parser.should_ignore("src/nested/file.tmp")

        # src/*.o should only match .o files directly in src/
        assert parser.should_ignore("src/file.o")

        # Character class patterns
        assert parser.should_ignore("Test.txt")
        assert parser.should_ignore("test.log")

    def test_empty_gitignore(self):
        """Test handling of empty gitignore files."""
        test_dir = self.repo_path / "test_empty"
        test_dir.mkdir()

        gitignore = test_dir / ".gitignore"
        gitignore.write_text("")

        parser = GitignoreParser(str(test_dir))

        # Should not crash and should return empty list
        with _no_directory_listing():
            assert parser.specs_of("file.txt") == []

    def test_malformed_gitignore(self):
        """Test handling of malformed gitignore content."""
        test_dir = self.repo_path / "test_malformed"
        test_dir.mkdir()

        gitignore = test_dir / ".gitignore"
        gitignore.write_text(
            """# Only comments and empty lines
    
# More comments
    
    """
        )

        parser = GitignoreParser(str(test_dir))

        # Should handle gracefully
        with _no_directory_listing():
            assert parser.specs_of("file.txt") == []

    def test_reload(self):
        """Test reloading gitignore files."""
        test_dir = self.repo_path / "test_reload"
        test_dir.mkdir()

        # Create initial gitignore
        gitignore = test_dir / ".gitignore"
        gitignore.write_text("*.log")

        parser = GitignoreParser(str(test_dir))
        assert len(parser.specs_of("test.log")) == 1
        assert parser.should_ignore("test.log")

        # Modify gitignore
        gitignore.write_text("*.tmp")

        # Without reload, should still use old patterns
        assert parser.should_ignore("test.log")
        assert not parser.should_ignore("test.tmp")

        # After reload, should use new patterns
        parser.reload()
        assert not parser.should_ignore("test.log")
        assert parser.should_ignore("test.tmp")

    def test_gitignore_spec_matches(self):
        """Test GitignoreSpec.matches method."""
        spec = GitignoreSpec("/path/to/.gitignore", ["*.log", "build/", "!important.log"])

        assert spec.matches("test.log")
        assert spec.matches("build/output.o")
        assert spec.matches("src/test.log")

        # Note: Negation patterns in pathspec work differently than in git
        # This is a limitation of the pathspec library

    def test_subdirectory_gitignore_pattern_scoping(self):
        """Test that subdirectory .gitignore patterns are scoped correctly."""
        # Create test structure: foo/ with subdirectory bar/
        test_dir = self.repo_path / "test_subdir_scoping"
        test_dir.mkdir()
        (test_dir / "foo").mkdir()
        (test_dir / "foo" / "bar").mkdir()

        # Create files in various locations
        (test_dir / "foo.txt").touch()  # root level
        (test_dir / "foo" / "foo.txt").touch()  # in foo/
        (test_dir / "foo" / "bar" / "foo.txt").touch()  # in foo/bar/

        # Test case 1: foo.txt in foo/.gitignore should only ignore in foo/ subtree
        gitignore = test_dir / "foo" / ".gitignore"
        gitignore.write_text("foo.txt\n")

        parser = GitignoreParser(str(test_dir))

        # foo.txt at root should NOT be ignored by foo/.gitignore
        assert not parser.should_ignore("foo.txt"), "Root foo.txt should not be ignored by foo/.gitignore"

        # foo.txt in foo/ should be ignored
        assert parser.should_ignore("foo/foo.txt"), "foo/foo.txt should be ignored"

        # foo.txt in foo/bar/ should be ignored (within foo/ subtree)
        assert parser.should_ignore("foo/bar/foo.txt"), "foo/bar/foo.txt should be ignored"

    def test_anchored_pattern_in_subdirectory(self):
        """Test that anchored patterns in subdirectory only match immediate children."""
        test_dir = self.repo_path / "test_anchored_subdir"
        test_dir.mkdir()
        (test_dir / "foo").mkdir()
        (test_dir / "foo" / "bar").mkdir()

        # Create files
        (test_dir / "foo.txt").touch()  # root level
        (test_dir / "foo" / "foo.txt").touch()  # in foo/
        (test_dir / "foo" / "bar" / "foo.txt").touch()  # in foo/bar/

        # Test case 2: /foo.txt in foo/.gitignore should only match foo/foo.txt
        gitignore = test_dir / "foo" / ".gitignore"
        gitignore.write_text("/foo.txt\n")

        parser = GitignoreParser(str(test_dir))

        # foo.txt at root should NOT be ignored
        assert not parser.should_ignore("foo.txt"), "Root foo.txt should not be ignored"

        # foo.txt directly in foo/ should be ignored
        assert parser.should_ignore("foo/foo.txt"), "foo/foo.txt should be ignored by /foo.txt pattern"

        # foo.txt in foo/bar/ should NOT be ignored (anchored pattern only matches immediate children)
        assert not parser.should_ignore("foo/bar/foo.txt"), "foo/bar/foo.txt should NOT be ignored by /foo.txt pattern"

    def test_double_star_pattern_scoping(self):
        """Test that **/pattern in subdirectory only applies within that subtree."""
        test_dir = self.repo_path / "test_doublestar_scope"
        test_dir.mkdir()
        (test_dir / "foo").mkdir()
        (test_dir / "foo" / "bar").mkdir()
        (test_dir / "other").mkdir()

        # Create files
        (test_dir / "foo.txt").touch()  # root level
        (test_dir / "foo" / "foo.txt").touch()  # in foo/
        (test_dir / "foo" / "bar" / "foo.txt").touch()  # in foo/bar/
        (test_dir / "other" / "foo.txt").touch()  # in other/

        # Test case 3: **/foo.txt in foo/.gitignore should only ignore within foo/ subtree
        gitignore = test_dir / "foo" / ".gitignore"
        gitignore.write_text("**/foo.txt\n")

        parser = GitignoreParser(str(test_dir))

        # foo.txt at root should NOT be ignored
        assert not parser.should_ignore("foo.txt"), "Root foo.txt should not be ignored by foo/.gitignore"

        # foo.txt in foo/ should be ignored
        assert parser.should_ignore("foo/foo.txt"), "foo/foo.txt should be ignored"

        # foo.txt in foo/bar/ should be ignored (within foo/ subtree)
        assert parser.should_ignore("foo/bar/foo.txt"), "foo/bar/foo.txt should be ignored"

        # foo.txt in other/ should NOT be ignored (outside foo/ subtree)
        assert not parser.should_ignore("other/foo.txt"), "other/foo.txt should NOT be ignored by foo/.gitignore"

    def test_anchored_double_star_pattern(self):
        """Test that /**/pattern in subdirectory works correctly."""
        test_dir = self.repo_path / "test_anchored_doublestar"
        test_dir.mkdir()
        (test_dir / "foo").mkdir()
        (test_dir / "foo" / "bar").mkdir()
        (test_dir / "other").mkdir()

        # Create files
        (test_dir / "foo.txt").touch()  # root level
        (test_dir / "foo" / "foo.txt").touch()  # in foo/
        (test_dir / "foo" / "bar" / "foo.txt").touch()  # in foo/bar/
        (test_dir / "other" / "foo.txt").touch()  # in other/

        # Test case 4: /**/foo.txt in foo/.gitignore should correctly ignore only within foo/ subtree
        gitignore = test_dir / "foo" / ".gitignore"
        gitignore.write_text("/**/foo.txt\n")

        parser = GitignoreParser(str(test_dir))

        # foo.txt at root should NOT be ignored
        assert not parser.should_ignore("foo.txt"), "Root foo.txt should not be ignored"

        # foo.txt in foo/ should be ignored
        assert parser.should_ignore("foo/foo.txt"), "foo/foo.txt should be ignored"

        # foo.txt in foo/bar/ should be ignored (within foo/ subtree)
        assert parser.should_ignore("foo/bar/foo.txt"), "foo/bar/foo.txt should be ignored"

        # foo.txt in other/ should NOT be ignored (outside foo/ subtree)
        assert not parser.should_ignore("other/foo.txt"), "other/foo.txt should NOT be ignored by foo/.gitignore"

    def test_unreadable_subdirectory_is_skipped_not_fatal(self):
        """A subdirectory whose .gitignore cannot be read for want of permission is skipped rather
        than failing the decision, mirroring scan_directory's guard.

        Regression for the ~/Desktop EPERM crash: an unreadable directory on a path's chain must
        not abort the decision.
        """
        test_dir = self.repo_path / "test_perm_subtree"
        test_dir.mkdir()
        (test_dir / ".gitignore").write_text("*.log\n")
        secret = test_dir / "secret"
        secret.mkdir()
        (secret / ".gitignore").write_text("*.tmp\n")  # would apply if the dir were readable
        secret_gitignore = str((secret / ".gitignore").absolute())

        def guarded_open(file, *args, **kwargs):
            if os.path.abspath(file) == secret_gitignore:
                raise PermissionError(1, "Operation not permitted")
            return open(file, *args, **kwargs)

        with _no_directory_listing(), mock.patch("serena.util.file_system.open", guarded_open, create=True):
            parser = GitignoreParser(str(test_dir))
            rel_paths = [os.path.relpath(spec.file_path, test_dir) for spec in parser.specs_of("secret/drop.tmp")]  # must NOT raise
            assert parser.should_ignore("secret/drop.log")  # the root .gitignore still applies
            assert not parser.should_ignore("secret/drop.tmp")

        assert ".gitignore" in rel_paths  # root .gitignore still read
        assert os.path.join("secret", ".gitignore") not in rel_paths  # unreadable subtree skipped

    def test_unreadable_root_raises_actionable_error(self):
        """When the repository ROOT itself is unreadable (e.g. a macOS privacy-protected folder
        such as ~/Desktop), deciding a path raises a clear, actionable error that names the folder —
        not an opaque, path-less crash. Constructing the parser reads nothing, so it does not raise.
        """
        test_dir = self.repo_path / "test_perm_root"
        test_dir.mkdir()
        root_abs = str(test_dir.absolute())
        root_gitignore = os.path.join(root_abs, ".gitignore")

        def guarded_open(file, *args, **kwargs):
            if os.path.abspath(file) == root_gitignore:
                raise PermissionError(1, "Operation not permitted")
            return open(file, *args, **kwargs)

        with _no_directory_listing(), mock.patch("serena.util.file_system.open", guarded_open, create=True):
            parser = GitignoreParser(str(test_dir))
            with pytest.raises(PermissionError) as excinfo:
                parser.should_ignore("file.txt")

        message = str(excinfo.value)
        assert root_abs in message  # names the offending folder
        assert "permission" in message.lower() or "protected" in message.lower()

    def test_fixture_lookup_equivalence(self):
        _assert_linear_equivalence(GitignoreParser(str(self.repo_path)), self.repo_path)

    @pytest.mark.skipif(sys.platform == "win32", reason="'*' is illegal in Windows filenames; this directory name cannot exist there")
    def test_gitignore_dir_name_with_glob_metachars_is_not_a_wildcard(self):
        """A directory named with glob metacharacters (e.g. a stray '***' venv) must be
        matched literally, not interpreted as a pattern (issue #1806).
        """
        test_dir = self.repo_path / "test_metachar_dirname"
        test_dir.mkdir()
        (test_dir / "pkg").mkdir()
        (test_dir / "***").mkdir()

        (test_dir / "pkg" / "mod.py").touch()
        (test_dir / "***" / "junk.txt").touch()

        gitignore = test_dir / "***" / ".gitignore"
        gitignore.write_text("*\n")

        parser = GitignoreParser(str(test_dir))

        # pkg/mod.py is outside the "***" directory and must not be affected by its gitignore
        assert not parser.should_ignore("pkg/mod.py"), "pkg/mod.py should not be ignored by ***/.gitignore"

        # junk.txt inside "***" is still ignored by its own gitignore's "*" pattern
        assert parser.should_ignore("***/junk.txt"), "***/junk.txt should be ignored by its own gitignore"

    def test_gitignore_dir_name_with_metachars_anchored_pattern(self):
        """Same as above, for the anchored-pattern join site."""
        test_dir = self.repo_path / "test_metachar_dirname_anchored"
        test_dir.mkdir()
        (test_dir / "pkg").mkdir()
        (test_dir / "a[1]").mkdir()

        (test_dir / "pkg" / "mod.py").touch()
        (test_dir / "a[1]" / "mod.py").touch()

        gitignore = test_dir / "a[1]" / ".gitignore"
        gitignore.write_text("/mod.py\n")

        parser = GitignoreParser(str(test_dir))

        assert not parser.should_ignore("pkg/mod.py"), "pkg/mod.py should not be ignored by a[1]/.gitignore"
        assert parser.should_ignore("a[1]/mod.py"), "a[1]/mod.py should be ignored by its own /mod.py pattern"

    @pytest.mark.skipif(sys.platform == "win32", reason="'?' is illegal in Windows filenames; this directory name cannot exist there")
    def test_gitignore_dir_name_with_metachars_implicit_double_star_pattern(self):
        """A non-anchored pattern with no leading '**/' is joined as (rel_dir, "**", line):
        the third join site. An unescaped '?' in the directory name would leak the pattern
        into a sibling directory whose name merely matches the wildcard (issue #1806).
        """
        test_dir = self.repo_path / "test_metachar_dirname_implicit_doublestar"
        test_dir.mkdir()
        (test_dir / "q?").mkdir()
        (test_dir / "qA").mkdir()
        (test_dir / "qA" / "sub").mkdir()

        (test_dir / "qA" / "sub" / "mod.py").touch()

        gitignore = test_dir / "q?" / ".gitignore"
        gitignore.write_text("mod.py\n")

        parser = GitignoreParser(str(test_dir))

        # "q?/.gitignore" must not reach into the sibling "qA/" directory just because "?"
        # would match the "A" in "qA" if left as a wildcard.
        assert not parser.should_ignore("qA/sub/mod.py"), "qA/sub/mod.py should not be ignored by q?/.gitignore"

    def test_escape_gitignore_path_component_escapes_all_metachars(self):
        """Pure-function coverage for the '*' and '?' escaping cases the two directory-creation
        tests above cannot exercise on Windows (those characters are illegal in Windows
        filenames, so this runs on every platform instead of touching the filesystem).
        """
        assert _escape_gitignore_path_component("***") == "\\*\\*\\*"
        assert _escape_gitignore_path_component("q?") == "q\\?"
        assert _escape_gitignore_path_component("a[1]") == "a\\[1\\]"
        assert _escape_gitignore_path_component("plain") == "plain"


class TestWriteFileAtomic:
    """Regression tests for issue #1958: a plain ``open(path, "w")`` truncates the file before
    the new content is complete, so a crash, OOM kill, or disk-full error partway through the
    write loses the previous content. ``write_file_atomic`` must never expose that intermediate
    state.
    """

    def test_writes_new_file(self, tmp_path):
        target = tmp_path / "notes.md"
        write_file_atomic(str(target), "hello", encoding="utf-8")
        assert target.read_text(encoding="utf-8") == "hello"

    def test_overwrites_existing_file(self, tmp_path):
        target = tmp_path / "notes.md"
        target.write_text("old", encoding="utf-8")
        write_file_atomic(str(target), "new", encoding="utf-8")
        assert target.read_text(encoding="utf-8") == "new"

    def test_no_leftover_temp_file_after_success(self, tmp_path):
        target = tmp_path / "notes.md"
        write_file_atomic(str(target), "hello", encoding="utf-8")
        assert list(tmp_path.iterdir()) == [target]

    def test_respects_newline_argument(self, tmp_path):
        target = tmp_path / "notes.md"
        write_file_atomic(str(target), "a\nb\n", encoding="utf-8", newline="\r\n")
        assert target.read_bytes() == b"a\r\nb\r\n"

    def test_preserves_original_content_when_write_is_interrupted(self, tmp_path, monkeypatch):
        """The core invariant: an interrupted write (process killed / OOM / disk full while the
        temp file is being written) must leave the target file exactly as it was, never
        truncated or half-overwritten.
        """
        target = tmp_path / "notes.md"
        original = "original content that must survive" * 20
        target.write_text(original, encoding="utf-8")

        real_fdopen = os.fdopen

        def crashing_fdopen(fd, *args, **kwargs):
            f = real_fdopen(fd, *args, **kwargs)
            real_write = f.write

            def crashing_write(data):
                # write a truncated prefix to the temp file, flush it to disk, then blow up,
                # simulating a crash after the OS has seen some but not all of the new content.
                real_write(data[: len(data) // 4])
                f.flush()
                raise RuntimeError("simulated crash mid-write")

            f.write = crashing_write
            return f

        monkeypatch.setattr(file_system.os, "fdopen", crashing_fdopen)

        with pytest.raises(RuntimeError, match="simulated crash mid-write"):
            write_file_atomic(str(target), "brand new content that never fully arrives" * 20, encoding="utf-8")

        assert target.read_text(encoding="utf-8") == original
        # the partially-written temp file must be cleaned up, not left behind
        assert list(tmp_path.iterdir()) == [target]

    def test_replace_with_retry_survives_transient_permission_error(self, tmp_path, monkeypatch):
        """Mirrors ``util/yaml.py``'s ``_replace_with_retry``: on Windows, ``os.replace`` can
        fail with a transient ``PermissionError`` while another process momentarily holds the
        destination open. The write must not be treated as failed while the temp file is still
        complete and a retry can still succeed.
        """
        target = tmp_path / "notes.md"
        target.write_text("old", encoding="utf-8")

        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError("simulated transient sharing violation")
            return real_replace(src, dst)

        monkeypatch.setattr(file_system.os, "replace", flaky_replace)
        monkeypatch.setattr(file_system.time, "sleep", lambda _seconds: None)

        write_file_atomic(str(target), "new", encoding="utf-8")

        assert calls["n"] == 3
        assert target.read_text(encoding="utf-8") == "new"


@contextlib.contextmanager
def _no_directory_listing() -> Iterator[None]:
    """Make every directory listing raise, so that a decision which lists any directory fails."""

    def refuse(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("a directory was listed")

    with mock.patch.object(os, "scandir", refuse), mock.patch.object(os, "listdir", refuse), mock.patch.object(os, "walk", refuse):
        yield


def _every_spec(parser: GitignoreParser, root: Path) -> list[GitignoreSpec]:
    """Every non-empty .gitignore under root, found by listing the tree, independently of the parser's chain lookup."""
    specs = []
    for gitignore in sorted(root.rglob(".gitignore")):
        spec = GitignoreSpec(str(gitignore), parser._parse_gitignore_content(gitignore.read_text(), str(gitignore.parent)))
        if spec.patterns:
            specs.append(spec)
    return specs


def _linear_should_ignore(parser: GitignoreParser, specs: list[GitignoreSpec], path: str) -> bool:
    """Decide a path against every spec of the tree, linearly and independently of the parser's chain lookup."""
    if os.path.isabs(path):
        try:
            rel_path = os.path.relpath(path, parser.repo_root)
        except Exception:
            return True
    else:
        rel_path = path
    if Path(rel_path).parts[0] == ".git":
        return True
    abs_path = os.path.join(parser.repo_root, rel_path)
    rel_path = rel_path.replace(os.sep, "/")
    if os.path.exists(abs_path) and os.path.isdir(abs_path) and not rel_path.endswith("/"):
        rel_path += "/"
    return any(spec.matches(rel_path) for spec in specs)


def _assert_linear_equivalence(parser: GitignoreParser, root: Path) -> None:
    """The parser decides every path of the tree as a linear check against every spec of the tree does, listing no directory."""
    specs = _every_spec(parser, root)
    entries = list(root.rglob("*"))
    with _no_directory_listing():
        for entry in entries:
            relative = entry.relative_to(root).as_posix()
            paths = [relative, str(entry), "./" + relative]
            if entry.is_dir():
                paths.extend([relative + "/", str(entry) + "/"])
            for path in paths:
                assert parser.should_ignore(path) == _linear_should_ignore(parser, specs, path), path


def test_nested_lookup_equivalence(tmp_path: Path):
    directories = [tmp_path]
    level = [tmp_path]
    for _ in range(2):
        level = [parent / name for parent in level for name in ("left", "right")]
        for directory in level:
            directory.mkdir()
        directories.extend(level)
    for index, directory in enumerate(directories):
        (directory / ".gitignore").write_text(f"/anchored-{index}\nloose-{index}\n**/deep-{index}\n*.tmp\n!keep.tmp\nblocked-{index}/\n")
        # Each spec's names occur both inside and outside its subtree, at every depth.
        for target in directories:
            for name in (f"anchored-{index}", f"loose-{index}", f"deep-{index}"):
                (target / name).touch()
        (directory / f"blocked-{index}").mkdir()
        for name in ("drop.tmp", "keep.tmp", "plain.txt"):
            (directory / name).touch()
    parser = GitignoreParser(str(tmp_path))
    assert len(_every_spec(parser, tmp_path)) == len(directories)
    _assert_linear_equivalence(parser, tmp_path)


@pytest.mark.parametrize("pattern", ["/drop.txt", "drop.txt", "**/drop.txt"])
def test_literal_gitignore_directory(tmp_path: Path, pattern: str):
    for name in ("a[bc]", "ab"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "drop.txt").touch()
    (tmp_path / "a[bc]" / ".gitignore").write_text(pattern)
    parser = GitignoreParser(str(tmp_path))
    with _no_directory_listing():
        assert parser.should_ignore("a[bc]/drop.txt")
        assert not parser.should_ignore("ab/drop.txt")
        # Also pin the compiled patterns used by Project's per-path spec.
        (spec,) = parser.specs_of("a[bc]/drop.txt")
        assert spec.matches("a[bc]/drop.txt")
        assert not spec.matches("ab/drop.txt")
    _assert_linear_equivalence(parser, tmp_path)


@pytest.mark.parametrize("directory_name", [" nested", "nested ", " nested "])
def test_whitespace_gitignore_directory(tmp_path: Path, directory_name: str):
    if sys.platform == "win32" and directory_name.endswith(" "):
        pytest.skip("Windows does not support directory names with trailing spaces")
    for name in (directory_name, "nested"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "drop.txt").touch()
    (tmp_path / directory_name / ".gitignore").write_text("/drop.txt\n")
    parser = GitignoreParser(str(tmp_path))
    assert parser.should_ignore(f"{directory_name}/drop.txt")
    assert not parser.should_ignore("nested/drop.txt")
    _assert_linear_equivalence(parser, tmp_path)


def test_discovery_prune_spec(tmp_path: Path):
    for name in ("build/sub", "outside"):
        directory = tmp_path / name
        directory.mkdir(parents=True)
        (directory / ".gitignore").write_text("*.tmp\n")
    unpruned = GitignoreParser(str(tmp_path))
    parser = GitignoreParser(str(tmp_path), prune_spec=PathSpec.from_lines("gitwildmatch", ["build/"]))
    with _no_directory_listing():
        assert unpruned.should_ignore("build/sub/drop.tmp")
        assert unpruned.should_ignore("outside/drop.tmp")
        # the pruned directory's .gitignore is never read, so it decides nothing
        assert parser.specs_of("build/sub/drop.tmp") == []
        assert not parser.should_ignore("build/sub/drop.tmp")
        assert [Path(spec.file_path).relative_to(tmp_path).as_posix() for spec in parser.specs_of("outside/drop.tmp")] == [
            "outside/.gitignore"
        ]
        assert parser.should_ignore("outside/drop.tmp")


def test_discovery_wildcard_prune_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name in ("rounds/one/evidence/deep", "rounds/two/source"):
        directory = tmp_path / name
        directory.mkdir(parents=True)
        (directory / ".gitignore").write_text("*.tmp\n")
    opened: list[str] = []

    def record_open(file: str, *args: Any, **kwargs: Any) -> Any:
        opened.append(Path(file).relative_to(tmp_path).as_posix())
        return open(file, *args, **kwargs)

    monkeypatch.setattr("serena.util.file_system.open", record_open, raising=False)
    parser = GitignoreParser(str(tmp_path), prune_spec=PathSpec.from_lines("gitwildmatch", ["rounds/*/evidence/"]))
    with _no_directory_listing():
        assert not parser.should_ignore("rounds/one/evidence/deep/drop.tmp")
        assert parser.should_ignore("rounds/two/source/drop.tmp")
    assert not [path for path in opened if path.startswith("rounds/one/evidence")]
    assert "rounds/two/source/.gitignore" in opened


def test_discovery_matches_cost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    depth = 3
    directories = [tmp_path]
    level = [tmp_path]
    for _ in range(depth):
        level = [parent / f"d{i}" for parent in level for i in range(4)]
        for directory in level:
            directory.mkdir()
        directories.extend(level)
    for directory in directories:
        (directory / ".gitignore").write_text("*.ignored\n")
    assert len(directories) == 85
    matches = GitignoreSpec.matches
    calls = 0

    def count_matches(spec: GitignoreSpec, path: str) -> bool:
        nonlocal calls
        calls += 1
        return matches(spec, path)

    opened: list[str] = []

    def record_open(file: str, *args: Any, **kwargs: Any) -> Any:
        opened.append(Path(file).relative_to(tmp_path).as_posix())
        return open(file, *args, **kwargs)

    monkeypatch.setattr(GitignoreSpec, "matches", count_matches)
    monkeypatch.setattr("serena.util.file_system.open", record_open, raising=False)
    parser = GitignoreParser(str(tmp_path))
    with _no_directory_listing():
        assert parser.should_ignore("d0/d1/d2/file.ignored")
    # only the four .gitignore files on the chain are read, and the decision checks each loaded spec once per chain level
    assert opened == [".gitignore", "d0/.gitignore", "d0/d1/.gitignore", "d0/d1/d2/.gitignore"]
    assert calls <= depth * (depth + 1) // 2 + depth + 1


def test_reload_rebuilds_lookup(tmp_path: Path):
    (tmp_path / "nested").mkdir()
    root_ignore = tmp_path / ".gitignore"
    nested_ignore = tmp_path / "nested/.gitignore"
    root_ignore.write_text("*.old\n")
    nested_ignore.write_text("*.nested\n")
    for name in ("file.old", "file.nested", "file.new"):
        (tmp_path / "nested" / name).touch()
    parser = GitignoreParser(str(tmp_path))
    _assert_linear_equivalence(parser, tmp_path)
    root_ignore.unlink()
    nested_ignore.write_text("*.new\n")
    # until reloaded, the parser keeps the rules it read
    with _no_directory_listing():
        assert parser.should_ignore("nested/file.old")
        parser.reload()
        assert not parser.should_ignore("nested/file.old")
        assert not parser.should_ignore("nested/file.nested")
        assert parser.should_ignore("nested/file.new")
    _assert_linear_equivalence(parser, tmp_path)


class TestGitignoreParserPermissionError:
    """Test PermissionError handling in GitignoreParser."""

    def setup_method(self):
        self.test_dir = tempfile.mkdtemp()
        self.repo_path = Path(self.test_dir)

    def teardown_method(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_scandir_permission_error_on_subdirectory(self):
        """
        Test that GitignoreParser does not crash when a subdirectory
        is not readable (PermissionError on os.scandir).

        Regression test for https://github.com/oraios/serena/issues/1624
        """
        # Create a root .gitignore
        gitignore = self.repo_path / ".gitignore"
        gitignore.write_text("*.log\n")

        # Create an unreadable subdirectory
        unreadable = self.repo_path / "unreadable_dir"
        unreadable.mkdir()

        # Remove read permissions (Unix only; on Windows this is a no-op)
        old_mode = os.stat(unreadable).st_mode
        os.chmod(unreadable, 0o000)

        try:
            # This should not raise PermissionError
            parser = GitignoreParser(str(self.repo_path))
            # Parser should still function
            assert parser.should_ignore("test.log")
        finally:
            # Restore permissions so teardown can clean up
            os.chmod(unreadable, old_mode)


class TestWriteFileAtomicSymlinks:
    """``write_file_atomic`` replaces ``open(path, "w")`` at its call sites, so it has to agree
    with it about symlinks: a plain write follows the link and updates its target, whereas a bare
    ``os.replace`` onto the link path would swap the link itself out for a regular file and leave
    the target holding stale content (issue #1958 asks for symlink behaviour to be preserved
    before source files use this).
    """

    @staticmethod
    def _symlink_or_skip(link: Path, target: Path) -> None:
        """Windows needs developer mode or admin rights to create a symlink; skip there rather
        than fail, matching how ``test_memories_manager.py`` handles the same limitation.
        """
        try:
            link.symlink_to(target)
        except OSError as e:
            pytest.skip(f"cannot create symlinks on this platform/permissions: {e}")

    def test_writes_through_a_symlink_instead_of_replacing_it(self, tmp_path):
        target = tmp_path / "real.txt"
        target.write_text("old", encoding="utf-8")
        link = tmp_path / "link.txt"
        self._symlink_or_skip(link, target)

        write_file_atomic(str(link), "new", encoding="utf-8")

        assert link.is_symlink(), "the symlink must survive the write, not be replaced by a regular file"
        assert target.read_text(encoding="utf-8") == "new", "the content must reach the link's target"

    def test_writes_through_a_symlink_pointing_outside_its_directory(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "real.txt"
        target.write_text("old", encoding="utf-8")
        inside = tmp_path / "inside"
        inside.mkdir()
        link = inside / "link.txt"
        self._symlink_or_skip(link, target)

        write_file_atomic(str(link), "new", encoding="utf-8")

        assert link.is_symlink()
        assert target.read_text(encoding="utf-8") == "new"
        assert list(inside.iterdir()) == [link], "no temp file may be left beside the link"

    def test_broken_symlink_creates_its_target(self, tmp_path):
        """``open(path, "w")`` on a dangling link creates the target; this must do the same."""
        target = tmp_path / "missing.txt"
        link = tmp_path / "link.txt"
        self._symlink_or_skip(link, target)

        write_file_atomic(str(link), "new", encoding="utf-8")

        assert link.is_symlink()
        assert target.read_text(encoding="utf-8") == "new"

    def test_writes_through_a_symlinked_parent_directory(self, tmp_path):
        """The path is resolved in full, so a symlinked *directory* on the way to the file is
        followed too, and the temporary file is created in the destination's real directory (it has
        to be on the same filesystem as the destination for the rename to be atomic).
        """
        real_dir = tmp_path / "real_dir"
        real_dir.mkdir()
        target = real_dir / "file.txt"
        target.write_text("old", encoding="utf-8")
        link_dir = tmp_path / "link_dir"
        self._symlink_or_skip(link_dir, real_dir)

        write_file_atomic(str(link_dir / "file.txt"), "new", encoding="utf-8")

        assert link_dir.is_symlink(), "the directory symlink must survive"
        assert target.read_text(encoding="utf-8") == "new"
        assert list(real_dir.iterdir()) == [target], "no temp file may be left in the real directory"

    def test_non_ascii_filename_round_trips(self, tmp_path):
        target = tmp_path / "測試檔案.txt"
        try:
            target.write_text("old", encoding="utf-8")
        except (OSError, UnicodeError) as e:
            pytest.skip(f"cannot create non-ASCII filenames on this filesystem: {e}")

        write_file_atomic(str(target), "new", encoding="utf-8")

        assert target.read_text(encoding="utf-8") == "new"
        assert list(tmp_path.iterdir()) == [target]

    def test_regular_file_is_written_in_place(self, tmp_path):
        """Control: the symlink handling must not change the ordinary case."""
        target = tmp_path / "plain.txt"
        target.write_text("old", encoding="utf-8")

        write_file_atomic(str(target), "new", encoding="utf-8")

        assert not target.is_symlink()
        assert target.read_text(encoding="utf-8") == "new"
        assert list(tmp_path.iterdir()) == [target]
