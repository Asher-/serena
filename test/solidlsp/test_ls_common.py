import os
from collections.abc import Sequence

import pytest

from solidlsp import SolidLanguageServer
from solidlsp.ls_config import LanguageServerId
from solidlsp.ls_utils import SymbolUtils
from test.conftest import PYTHON_LANGUAGE_BACKENDS, start_ls_context


class TestLanguageServerCommonFunctionality:
    """Test common functionality of SolidLanguageServer base implementation (not language-specific behaviour)."""

    @pytest.mark.parametrize("language_server", PYTHON_LANGUAGE_BACKENDS, indirect=True)
    def test_open_file_cache_invalidate(self, language_server: SolidLanguageServer) -> None:
        """
        Tests that the file buffer cache is invalidated when the file is changed on disk.
        """
        file_path = os.path.join(language_server.repository_root_path, "test_open_file.py")
        test_string1 = "# foo"
        test_string2 = "# bar"

        with open(file_path, "w") as f:
            f.write(test_string1)

        try:
            with language_server.open_file(file_path) as fb:
                assert fb.contents == test_string1

                # apply external change to file
                with open(file_path, "w") as f:
                    f.write(test_string2)

                # Explicitly bump mtime into the future so the cache sees a change.
                # Relying on natural mtime advancement is flaky because many filesystems
                # (ext4, tmpfs) have only 1-second mtime granularity, and both writes
                # can land in the same second.
                stat = os.stat(file_path)
                os.utime(file_path, (stat.st_atime, stat.st_mtime + 2))

                # check that the file buffer has been invalidated and reloaded
                assert fb.contents == test_string2

        finally:
            os.remove(file_path)

    @pytest.mark.parametrize("language_server", [LanguageServerId.PYTHON], indirect=True)
    def test_reload_from_disk_bumps_version_and_refreshes_buffer(self, language_server: SolidLanguageServer) -> None:
        """
        Regression test for bug #3 — external mutations (e.g. git checkout during
        a project re-activation) invalidate both the in-memory buffer AND the
        language server's document copy. ``LSPFileBuffer.reload_from_disk``
        detects the staleness, refreshes the buffer, and pushes a full-document
        ``didChange`` notification. Version numbers are bumped so the LSP
        accepts the update.
        """
        file_path = os.path.join(language_server.repository_root_path, "test_reload_from_disk.py")
        initial_text = "# initial\n"
        updated_text = "# updated\n"

        with open(file_path, "w") as f:
            f.write(initial_text)

        try:
            with language_server.open_file(file_path) as fb:
                assert fb.contents == initial_text
                version_before = fb.version

                # simulate an external mutation happening while the buffer is open
                # (e.g. a branch checkout that rewrites the file on disk)
                with open(file_path, "w") as f:
                    f.write(updated_text)

                # bump mtime deterministically — filesystems with 1-second mtime
                # granularity otherwise make this test flaky
                stat = os.stat(file_path)
                os.utime(file_path, (stat.st_atime, stat.st_mtime + 2))

                # reload_from_disk should report that a reload happened and the
                # buffer's contents should now reflect the updated file. The
                # version counter must bump so that the LSP accepts any
                # subsequent incremental edits layered on the refreshed state.
                reloaded = fb.reload_from_disk()
                assert reloaded is True
                assert fb.contents == updated_text
                assert fb.version > version_before

                # a second call with no further disk change is a no-op
                assert fb.reload_from_disk() is False

        finally:
            os.remove(file_path)

    def test_workspace_folders_affect_full_symbol_tree(self):
        """
        Tests that workspace folder configurations are respected when requesting the full symbol tree.
        """

        def check(langsrv: SolidLanguageServer, present: Sequence[str] = (), absent: Sequence[str] = ()) -> None:
            symbols = langsrv.request_full_symbol_tree()
            for name in present:
                assert SymbolUtils.symbol_tree_contains_name(symbols, name)
            for name in absent:
                assert not SymbolUtils.symbol_tree_contains_name(symbols, name)

        symbols_in_subfolder_test_repo = ["OuterClass", "UserService"]
        symbols_in_subfolder_scripts = ["parse_args", "create_sample_users"]
        all_symbols = symbols_in_subfolder_test_repo + symbols_in_subfolder_scripts

        with start_ls_context(ls_id=LanguageServerId.PYTHON, workspace_folders=["."]) as ls:
            ls.request_full_symbol_tree()
            check(ls, present=all_symbols)

        with start_ls_context(ls_id=LanguageServerId.PYTHON, workspace_folders=["./test_repo"]) as ls:
            ls.request_full_symbol_tree()
            check(ls, present=symbols_in_subfolder_test_repo, absent=symbols_in_subfolder_scripts)

        with start_ls_context(ls_id=LanguageServerId.PYTHON, workspace_folders=["./scripts"]) as ls:
            ls.request_full_symbol_tree()
            check(ls, present=symbols_in_subfolder_scripts, absent=symbols_in_subfolder_test_repo)

            # test that explicit requests for symbols in other workspace folders are rejected
            with pytest.raises(ValueError, match="outside"):
                ls.request_full_symbol_tree(within_relative_path="./test_repo")
