import os
import shutil
import subprocess
from pathlib import Path


class FixtureRepositories:
    """
    The fixture repositories of one process: copies of the directories under ``test/resources/repos``, so that no test writes into
    the checked-in repositories and no two processes share a repository.

    A directory is copied on its first use and holds the files git tracks in the checked-in directory, so that build output and
    project data which earlier runs left in the checked-in directory never reach a test.
    """

    _SOURCE_DIR = Path(__file__).parent / "resources" / "repos"

    def __init__(self, root: Path) -> None:
        """
        :param root: the empty directory to hold the copies
        """
        self._root = root
        self._copied: set[str] = set()

    def get_dir(self, name: str) -> Path:
        """
        :param name: the name of a directory under ``test/resources/repos``, e.g. ``python``
        :return: this process's copy of the directory
        """
        copy_dir = self._root / name
        if name not in self._copied:
            self._copy(self._SOURCE_DIR / name, copy_dir)
            self._copied.add(name)
        return copy_dir

    @staticmethod
    def _copy(source_dir: Path, copy_dir: Path) -> None:
        listing = subprocess.run(["git", "ls-files", "-z"], cwd=source_dir, check=True, capture_output=True).stdout
        for relative_path in filter(None, listing.split(b"\0")):
            source_file = source_dir / os.fsdecode(relative_path)
            copy_file = copy_dir / os.fsdecode(relative_path)
            copy_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, copy_file, follow_symlinks=False)
