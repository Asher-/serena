import os

import pytest

from solidlsp import SolidLanguageServer
from solidlsp.ls_config import Language


class TestLanguageServerCommonFunctionality:
    """Test common functionality of SolidLanguageServer base implementation (not language-specific behaviour)."""

    @pytest.mark.parametrize("language_server", [Language.PYTHON], indirect=True)
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

    @pytest.mark.parametrize("language_server", [Language.PYTHON], indirect=True)
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
