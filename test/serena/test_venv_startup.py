import subprocess
import sys


class TestVenvStartup:
    """
    Guarantees about what an interpreter started from serena's environment does while ``site`` starts up.
    """

    def test_startup_imports_no_docs_toolchain(self) -> None:
        """
        A fresh interpreter from serena's environment imports no part of the Sphinx docs toolchain at startup.
        """
        # fresh interpreter reporting what its startup imported
        completed = subprocess.run(
            [sys.executable, "-c", "import sys; print('sphinx_jinja2_compat' in sys.modules)"],
            capture_output=True,
            text=True,
            check=True,
        )

        assert completed.stdout.strip() == "False"
