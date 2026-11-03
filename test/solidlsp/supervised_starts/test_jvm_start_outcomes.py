"""
The JVM family's runs through brain-supervise, where a program fails, and where the bridge build succeeds: the ``cs``
setup of :class:`ScalaLanguageServer`, the ``java_home`` lookup and the bridge build of the Java structural backend. The
programs run against stand-ins on PATH, apart from ``/usr/libexec/java_home``, which is started by its absolute path
and so cannot be stood in for: the test gives its answer.
"""

import os
import pathlib
import shlex
from pathlib import Path

import pytest

from solidlsp.language_servers.scala_language_server import ScalaLanguageServer
from solidlsp.ls_config import LanguageServerConfig, LanguageServerId
from solidlsp.settings import SolidLSPSettings
from solidlsp.structural.backends import java as java_backend
from solidlsp.util import supervise
from test.solidlsp.util.supervised_stand_in import StandIns, assert_started_through_brain_supervise

pytestmark = pytest.mark.skipif(not os.environ.get(supervise.PROGRAM_ENV), reason="BRAIN_SUPERVISE names no program")

_JAVA_HOME_PROGRAM = "/usr/libexec/java_home"
_HOMEBREW_JAVAS = {"/opt/homebrew/opt/openjdk/bin/java", "/usr/local/opt/openjdk/bin/java"}
"""The Homebrew java binaries :func:`java_backend._java_binary` prefers to what ``java_home`` names."""


def _asking_java_home(monkeypatch: pytest.MonkeyPatch, answer: str) -> list[object]:
    """
    Leaves :func:`java_backend._java_binary` neither JAVA_HOME nor a Homebrew java, so that it asks ``java_home``, and
    makes ``java_home`` answer as given.

    :param monkeypatch: the test's monkeypatch.
    :param answer: what ``java_home`` prints.
    :return: the commands it was asked for, which grows as the test runs.
    """
    monkeypatch.delenv("JAVA_HOME", raising=False)
    exists = pathlib.Path.exists
    monkeypatch.setattr(pathlib.Path, "exists", lambda path: str(path) not in _HOMEBREW_JAVAS and exists(path))
    asked: list[object] = []

    def check_output(args: list[str], **kwargs: object) -> str:
        asked.append(args)
        return answer

    monkeypatch.setattr(supervise, "check_output", check_output)
    return asked


def test_metals_setup_fails_with_what_a_failing_coursier_setup_wrote(stand_ins: StandIns, tmp_path: Path) -> None:
    stand_ins.answering("java")
    coursier = stand_ins.answering("coursier", stderr="Error: cannot download the cs launcher\n", exit_code=1)
    settings = SolidLSPSettings(solidlsp_dir=str(tmp_path / "solidlsp"))

    with pytest.raises(RuntimeError) as caught:
        ScalaLanguageServer._setup_runtime_dependencies(LanguageServerConfig(ls_id=LanguageServerId.SCALA), settings)
    assert "Failed to set up 'cs' command with 'coursier setup'. Stderr: Error: cannot download the cs launcher" in str(caught.value)
    assert_started_through_brain_supervise(coursier)


def test_java_is_the_one_in_the_home_java_home_prints(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "jdk"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "java").write_text("#!/bin/sh\n")
    asked = _asking_java_home(monkeypatch, f"{home}\n")

    assert java_backend._java_binary() == str(home / "bin" / "java")
    assert asked == [[_JAVA_HOME_PROGRAM]]


def test_java_is_the_one_on_path_where_java_home_prints_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _asking_java_home(monkeypatch, "\n")

    assert java_backend._java_binary() == "java"
    assert asked == [[_JAVA_HOME_PROGRAM]]


def test_the_java_bridge_is_the_jar_mvn_package_wrote_in_the_package_directory(
    stand_ins: StandIns, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(java_backend, "_BRIDGE_PACKAGE_DIR", tmp_path / "java_bridge")
    # an mvn that writes the jar under the directory it runs in
    relative = java_backend._BRIDGE_JAR_RELATIVE
    mvn = stand_ins.add("mvn", f"/bin/mkdir -p {shlex.quote(str(relative.parent))}; /usr/bin/touch {shlex.quote(str(relative))}")

    jar = java_backend._build_bridge_if_needed()

    assert jar == tmp_path / "java_bridge" / relative
    assert jar.exists()
    assert_started_through_brain_supervise(mvn)
