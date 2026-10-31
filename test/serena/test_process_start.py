"""
Serena starts a process through the brain-supervise program and no other way
(task://daemon_restart_survival.18): :mod:`solidlsp.util.supervise` is the one module under src that
starts anything directly, and what it starts is the supervisor.
"""

import ast
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).parents[2] / "src"
SUPERVISOR_CLIENT = SOURCE_ROOT / "solidlsp" / "util" / "supervise.py"

DIRECT_STARTS = {
    "subprocess": {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"},
    "os": {
        "system",
        "popen",
        "fork",
        "forkpty",
        "posix_spawn",
        "posix_spawnp",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
    },
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
    "psutil": {"Popen"},
    "pty": {"fork", "spawn"},
    "multiprocessing": {"Process"},
}
"""The attributes of each module that start a process."""


def direct_starts(source: str) -> list[str]:
    """
    :param source: the text of a Python module.
    :return: each direct process start in it, as ``module.attribute``, by call or by import.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.attr in DIRECT_STARTS.get(node.value.id, ()):
                found.append(f"{node.value.id}.{node.attr}")
        elif isinstance(node, ast.ImportFrom):
            found.extend(f"{node.module}.{alias.name}" for alias in node.names if alias.name in DIRECT_STARTS.get(node.module or "", ()))
    return found


def test_only_the_supervisor_client_starts_a_process_directly() -> None:
    offenders = {
        str(path.relative_to(SOURCE_ROOT)): starts
        for path in SOURCE_ROOT.rglob("*.py")
        if path != SUPERVISOR_CLIENT and (starts := direct_starts(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}


def test_the_supervisor_client_starts_the_program_and_nothing_else() -> None:
    assert sorted(direct_starts(SUPERVISOR_CLIENT.read_text(encoding="utf-8"))) == ["subprocess.Popen", "subprocess.run"]


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess\nsubprocess.run(['ls'])\n",
        "import subprocess\nsubprocess.Popen(['ls'])\n",
        "import os\nos.system('ls')\n",
        "import os\nos.execvp('ls', ['ls'])\n",
        "import asyncio\nasyncio.create_subprocess_exec('ls')\n",
        "from subprocess import Popen\n",
        "import psutil\npsutil.Popen(['ls'])\n",
        "import multiprocessing\nmultiprocessing.Process()\n",
    ],
)
def test_a_direct_start_is_found(source: str) -> None:
    assert direct_starts(source)


def test_an_exception_of_subprocess_is_not_a_start() -> None:
    assert direct_starts("import subprocess\ntry:\n    pass\nexcept subprocess.CalledProcessError:\n    pass\n") == []
