"""
Serena starts a process through the brain-supervise program and no other way
(task://daemon_restart_survival.18): :mod:`solidlsp.util.supervise` is the one module under src that
starts anything directly, and what it starts is the supervisor. The one exemption is the editor a person opens
from serena's command line, which needs the terminal (decision://brain/daemon_restart_survival/serena/cli/editor).
"""

import ast
from dataclasses import dataclass
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
        "startfile",
    },
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
    "asyncio.subprocess": {"create_subprocess_exec", "create_subprocess_shell"},
    "anyio": {"open_process", "run_process"},
    "psutil": {"Popen"},
    "pty": {"fork", "spawn"},
    "multiprocessing": {"Process", "Pool", "Manager", "get_context"},
    "multiprocessing.pool": {"Pool"},
    "multiprocessing.context": {"Process", "SpawnProcess", "ForkProcess", "ForkServerProcess"},
    "multiprocessing.process": {"BaseProcess"},
    "multiprocessing.managers": {"BaseManager", "SyncManager"},
    "concurrent.futures": {"ProcessPoolExecutor"},
    "concurrent.futures.process": {"ProcessPoolExecutor"},
}
"""The attributes of each module that start a process."""

EVENT_LOOP_STARTS = {"subprocess_exec", "subprocess_shell"}
"""The methods of an asyncio event loop that start a process, found by name on whatever object they are called."""

EXEMPT = {("serena/cli.py", "_open_in_editor")}
"""The functions, by file under src, whose starts are exempt: those a person runs at a terminal and no server does."""


@dataclass(frozen=True)
class Start:
    """A direct process start in a module's source."""

    name: str
    """the start, as ``module.attribute``, or ``.method`` for an event loop's method."""
    function: str
    """the name of the function holding it, or empty at module level."""


class _StartFinder(ast.NodeVisitor):
    """
    Finds the direct starts in a module: by attribute of a module under any name the module is imported as anywhere in
    it, or under its own name, and by import.
    """

    def __init__(self, tree: ast.Module) -> None:
        """
        :param tree: the module.
        """
        self.starts: list[Start] = []
        self._functions: list[str] = []

        # the module each name stands for, wherever in the module it is imported
        self._modules: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    head = alias.name.split(".")[0]
                    self._modules[alias.asname or head] = alias.name if alias.asname else head
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    self._modules[alias.asname or alias.name] = f"{node.module or ''}.{alias.name}"

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            if alias.name in DIRECT_STARTS.get(module, ()):
                self._found(f"{module}.{alias.name}")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._within(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._within(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        dotted = self._dotted(node.value)
        if dotted is not None and node.attr in DIRECT_STARTS.get(dotted, ()):
            self._found(f"{dotted}.{node.attr}")
        elif node.attr in EVENT_LOOP_STARTS:
            self._found(f".{node.attr}")
        self.generic_visit(node)

    def _within(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._functions.append(node.name)
        self.generic_visit(node)
        self._functions.pop()

    def _dotted(self, node: ast.expr) -> str | None:
        """
        :param node: an expression.
        :return: the module it names, through the names this module imported, or None when it names none.
        """
        if isinstance(node, ast.Name):
            return self._modules.get(node.id, node.id if node.id in DIRECT_STARTS else None)
        if isinstance(node, ast.Attribute):
            base = self._dotted(node.value)
            return None if base is None else f"{base}.{node.attr}"
        return None

    def _found(self, name: str) -> None:
        self.starts.append(Start(name, self._functions[0] if self._functions else ""))


def find_starts(source: str) -> list[Start]:
    """
    :param source: the text of a Python module.
    :return: each direct process start in it, by call or by import.
    """
    tree = ast.parse(source)
    finder = _StartFinder(tree)
    finder.visit(tree)
    return finder.starts


def direct_starts(source: str) -> list[str]:
    """
    :param source: the text of a Python module.
    :return: each direct process start in it, as ``module.attribute``, by call or by import.
    """
    return [start.name for start in find_starts(source)]


def test_only_the_supervisor_client_starts_a_process_directly() -> None:
    offenders = {}
    for path in SOURCE_ROOT.rglob("*.py"):
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        if path == SUPERVISOR_CLIENT:
            continue
        starts = [start.name for start in find_starts(path.read_text(encoding="utf-8")) if (relative, start.function) not in EXEMPT]
        if starts:
            offenders[relative] = starts
    assert offenders == {}


def test_the_supervisor_client_starts_the_program_and_nothing_else() -> None:
    assert sorted(direct_starts(SUPERVISOR_CLIENT.read_text(encoding="utf-8"))) == ["subprocess.Popen", "subprocess.run"]


def test_the_command_line_editor_starts_the_editor_directly_and_is_the_one_exemption() -> None:
    starts = find_starts((SOURCE_ROOT / "serena" / "cli.py").read_text(encoding="utf-8"))
    assert {start.function for start in starts} == {"_open_in_editor"}
    assert sorted({start.name for start in starts}) == ["os.startfile", "subprocess.run"]


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
        "import subprocess as sp\nsp.run(['ls'])\n",
        "import subprocess as sp\nsp.Popen(['ls'])\n",
        "import multiprocessing as mp\nmp.Pool()\n",
        "import multiprocessing\nmultiprocessing.get_context('spawn').Process()\n",
        "from multiprocessing import Pool\n",
        "import multiprocessing.pool\nmultiprocessing.pool.Pool()\n",
        "import concurrent.futures\nconcurrent.futures.ProcessPoolExecutor()\n",
        "from concurrent.futures import ProcessPoolExecutor\n",
        "from concurrent import futures\nfutures.ProcessPoolExecutor()\n",
        "import concurrent.futures as cf\ncf.ProcessPoolExecutor()\n",
        "import asyncio\nasyncio.create_subprocess_shell('ls')\n",
        "import asyncio.subprocess\nasyncio.subprocess.create_subprocess_exec('ls')\n",
        "from asyncio import create_subprocess_exec\n",
        "from asyncio import subprocess as asp\nasp.create_subprocess_shell('ls')\n",
        "import asyncio\nasyncio.get_running_loop().subprocess_exec(object, 'ls')\n",
        "import anyio\nanyio.open_process(['ls'])\n",
        "from anyio import open_process\n",
        "import anyio as a\na.run_process(['ls'])\n",
        "import os\nos.startfile('x')\n",
    ],
)
def test_a_direct_start_is_found(source: str) -> None:
    assert direct_starts(source)


def test_an_exception_of_subprocess_is_not_a_start() -> None:
    assert direct_starts("import subprocess\ntry:\n    pass\nexcept subprocess.CalledProcessError:\n    pass\n") == []


def test_a_thread_pool_is_not_a_start() -> None:
    assert direct_starts("import concurrent.futures\nconcurrent.futures.ThreadPoolExecutor()\nconcurrent.futures.Future()\n") == []


def test_a_start_in_an_exempt_function_of_another_file_is_still_found() -> None:
    starts = find_starts("import subprocess\ndef _open_in_editor():\n    subprocess.run(['vi'])\n")
    assert [(start.name, start.function) for start in starts] == [("subprocess.run", "_open_in_editor")]
    assert ("serena/other.py", "_open_in_editor") not in EXEMPT
