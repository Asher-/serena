"""
The JVM family starts its programs through brain-supervise: the Clojure CLI check of :mod:`clojure_lsp`, the Metals
bootstrap of :class:`ScalaLanguageServer`, and the ``java_home`` lookup and the bridge build of the Java structural
backend. Each runs against stand-ins on PATH, apart from ``/usr/libexec/java_home``, which is started by its absolute
path and so is run as it is and its record watched for under the restart root.
"""

import os
import pathlib
import shlex
import subprocess
from pathlib import Path

import pytest

from solidlsp.language_servers.clojure_lsp import run_command
from solidlsp.language_servers.scala_language_server import ScalaLanguageServer
from solidlsp.ls_config import LanguageServerConfig, LanguageServerId
from solidlsp.settings import SolidLSPSettings
from solidlsp.structural.backends import java as java_backend
from solidlsp.structural.errors import ParseError
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import RecordWatcher, StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")

_JAVA_HOME_PROGRAM = "/usr/libexec/java_home"
_HOMEBREW_JAVAS = {"/opt/homebrew/opt/openjdk/bin/java", "/usr/local/opt/openjdk/bin/java"}
"""The Homebrew java binaries :func:`java_backend._java_binary` prefers to what ``java_home`` names."""


def test_a_clojure_command_answers_with_its_stdout_and_stderr_together(stand_ins: StandIns) -> None:
    clojure = stand_ins.answering("clojure", stdout="Usage: clojure [clj-opt*] -Aaliases\n", stderr="WARNING: no deps.edn\n")

    done = run_command(["clojure", "--help"])

    assert done.stdout == "Usage: clojure [clj-opt*] -Aaliases\nWARNING: no deps.edn\n"
    assert_started_through_brain_supervise(clojure)


def test_metals_is_launched_from_where_cs_bootstrapped_it_once_coursier_set_cs_up(stand_ins: StandIns, tmp_path: Path) -> None:
    # a cs that bootstraps metals to the -o path, kept off PATH until coursier setup puts it there
    cs = stand_ins.add(
        "cs",
        """
        while [ $# -gt 0 ]; do
            if [ "$1" = -o ]; then echo 'bootstrapped by cs' > "$2"; fi
            shift
        done
        """,
    )
    staged = tmp_path / "cs-before-setup"
    cs.path.rename(staged)
    stand_ins.answering("java")
    coursier = stand_ins.add("coursier", f'if [ "$1" = setup ]; then /bin/mv {shlex.quote(str(staged))} {shlex.quote(str(cs.path))}; fi')
    mkdir = stand_ins.add("mkdir", 'exec /bin/mkdir "$@"')
    settings = SolidLSPSettings(solidlsp_dir=str(tmp_path / "solidlsp"))

    command = ScalaLanguageServer._setup_runtime_dependencies(LanguageServerConfig(ls_id=LanguageServerId.SCALA), settings)

    assert Path(command[0]).read_text() == "bootstrapped by cs\n"
    assert_started_through_brain_supervise(coursier, mkdir, cs)


@pytest.mark.skipif(not Path(_JAVA_HOME_PROGRAM).exists(), reason="no /usr/libexec/java_home where this runs")
def test_java_is_the_one_in_the_home_java_home_names(stand_ins: StandIns, monkeypatch: pytest.MonkeyPatch) -> None:
    # neither JAVA_HOME nor a Homebrew java, so that java_home is asked
    monkeypatch.delenv("JAVA_HOME", raising=False)
    exists = pathlib.Path.exists
    monkeypatch.setattr(pathlib.Path, "exists", lambda path: str(path) not in _HOMEBREW_JAVAS and exists(path))

    # what java_home names here, asked directly
    answer = subprocess.run([_JAVA_HOME_PROGRAM], capture_output=True, text=True, check=False)
    home_java = Path(answer.stdout.strip()) / "bin" / "java"
    expected = str(home_java) if answer.returncode == 0 and answer.stdout.strip() and exists(home_java) else "java"

    with RecordWatcher(stand_ins.root) as watcher:
        java = java_backend._java_binary()

    assert java == expected
    assert _JAVA_HOME_PROGRAM in {record["path"] for record in watcher.records()}


def test_a_failed_java_bridge_build_raises_with_what_mvn_package_wrote(
    stand_ins: StandIns, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(java_backend, "_BRIDGE_PACKAGE_DIR", tmp_path / "java_bridge")
    mvn = stand_ins.answering("mvn", stdout="[ERROR] COMPILATION ERROR : Bridge.java:[3,1] ';' expected\n", exit_code=1)

    with pytest.raises(ParseError) as caught:
        java_backend._build_bridge_if_needed()
    assert "[ERROR] COMPILATION ERROR : Bridge.java:[3,1] ';' expected" in caught.value.detail
    assert_started_through_brain_supervise(mvn)
