"""
Installation of the language servers that test runs use, by a step of its own, into a root that no test run writes.

Run ``poe install-test-language-servers <language server> ...`` to install servers into the root.
"""

import os
import sys
from pathlib import Path


class TestLanguageServerInstallation:
    """
    The root into which the language servers for test runs are installed: ``$SERENA_TEST_LANGUAGE_SERVERS`` when set, else
    ``~/.cache/serena/test-language-servers``.

    The root is a serena home, so the servers installed into it are in ``<root>/language_servers/static``.
    """

    __test__ = False

    ROOT_ENV_VAR = "SERENA_TEST_LANGUAGE_SERVERS"

    def __init__(self) -> None:
        configured_root = os.environ.get(self.ROOT_ENV_VAR, "").strip()
        self.root = Path(configured_root) if configured_root else Path.home() / ".cache" / "serena" / "test-language-servers"

    def _static_dir(self, serena_home: Path) -> Path:
        return serena_home / "language_servers" / "static"

    def link_into(self, serena_home: Path) -> None:
        """
        Makes each installed server available to the language servers that run with the given serena home.

        Each installed server is linked from a directory of the home's own, so that a server which is not installed is installed
        into the home and never into the root.

        :param serena_home: a serena home without a ``language_servers`` directory
        """
        static_dir = self._static_dir(serena_home)
        static_dir.mkdir(parents=True)
        installed_dir = self._static_dir(self.root)
        if installed_dir.is_dir():
            for installed_server in installed_dir.iterdir():
                (static_dir / installed_server.name).symlink_to(installed_server)

    def install(self, language_servers: list[str]) -> None:
        """
        Installs the given language servers into the root through ``serena download-ls-dependencies``, ending the process with
        that command's exit status.

        :param language_servers: the names of the language servers, as ``serena download-ls-dependencies`` accepts them
        """
        os.environ["SERENA_HOME"] = str(self.root)
        from serena.cli import top_level

        top_level(["download-ls-dependencies", *language_servers])


if __name__ == "__main__":
    TestLanguageServerInstallation().install(sys.argv[1:])
