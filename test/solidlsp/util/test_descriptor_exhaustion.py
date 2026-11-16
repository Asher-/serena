"""
Descriptor exhaustion is recognised anywhere in a failure's chain and answered once by the installed responder.
"""

import errno
from collections.abc import Iterator

import pytest

from solidlsp.util.descriptor_exhaustion import DescriptorExhaustion


class _CountingResponder(DescriptorExhaustion.Responder):
    """A responder that counts its calls instead of ending the process."""

    def __init__(self) -> None:
        self.calls = 0

    def respond(self) -> None:
        self.calls += 1


@pytest.fixture
def responder() -> Iterator[_CountingResponder]:
    """A counting responder, installed for the test and removed after it."""
    installed = _CountingResponder()
    DescriptorExhaustion.install(installed)
    yield installed
    DescriptorExhaustion.uninstall(installed)


def _raised_from(cause: BaseException) -> RuntimeError:
    """A failure whose cause is the given exception."""
    try:
        raise RuntimeError("the operation failed") from cause
    except RuntimeError as failure:
        return failure


def _raised_while_handling(context: BaseException) -> RuntimeError:
    """A failure raised, with an unrelated cause, while the given exception was being handled."""
    try:
        raise context
    except BaseException:
        try:
            raise RuntimeError("the operation failed") from ValueError("unrelated")
        except RuntimeError as failure:
            return failure


@pytest.mark.parametrize(
    "failure",
    [
        OSError(errno.EMFILE, "Too many open files"),
        OSError(errno.ENFILE, "Too many open files in system"),
        _raised_from(OSError(errno.ENFILE, "Too many open files in system")),
        _raised_while_handling(OSError(errno.EMFILE, "Too many open files")),
    ],
    ids=["emfile", "enfile", "enfile-as-cause", "emfile-as-context-beside-a-cause"],
)
def test_a_failure_stemming_from_running_out_of_descriptors_is_exhaustion(failure: BaseException) -> None:
    assert DescriptorExhaustion.is_exhaustion(failure)


@pytest.mark.parametrize(
    "failure",
    [OSError(errno.ENOENT, "No such file or directory"), _raised_from(OSError(errno.EACCES, "Permission denied")), ValueError("bad")],
    ids=["other-errno", "other-errno-as-cause", "not-an-os-error"],
)
def test_a_failure_with_no_descriptor_exhaustion_in_its_chain_is_not_exhaustion(failure: BaseException) -> None:
    assert not DescriptorExhaustion.is_exhaustion(failure)


def test_a_failure_whose_chain_loops_back_on_itself_is_judged_without_looping() -> None:
    first = RuntimeError("first")
    second = RuntimeError("second")
    first.__context__ = second
    second.__context__ = first

    assert not DescriptorExhaustion.is_exhaustion(first)


def test_the_first_exhaustion_reported_calls_the_responder_and_later_ones_do_not(responder: _CountingResponder) -> None:
    DescriptorExhaustion.report(OSError(errno.EMFILE, "Too many open files"), "a site")
    DescriptorExhaustion.report(OSError(errno.ENFILE, "Too many open files in system"), "another site")

    assert responder.calls == 1


def test_a_responder_installed_after_a_response_is_called_on_the_next_exhaustion(responder: _CountingResponder) -> None:
    DescriptorExhaustion.report(OSError(errno.EMFILE, "Too many open files"), "a site")
    replacement = _CountingResponder()
    DescriptorExhaustion.install(replacement)
    try:
        DescriptorExhaustion.report(OSError(errno.EMFILE, "Too many open files"), "a site")
    finally:
        DescriptorExhaustion.uninstall(replacement)

    assert (responder.calls, replacement.calls) == (1, 1)


def test_a_failure_other_than_exhaustion_calls_no_responder(responder: _CountingResponder) -> None:
    DescriptorExhaustion.report(OSError(errno.ENOENT, "No such file or directory"), "a site")

    assert responder.calls == 0


def test_an_exhaustion_reported_after_the_responder_is_removed_calls_nothing(responder: _CountingResponder) -> None:
    DescriptorExhaustion.uninstall(responder)

    DescriptorExhaustion.report(OSError(errno.EMFILE, "Too many open files"), "a site")

    assert responder.calls == 0


def test_removing_a_responder_that_is_not_installed_leaves_the_installed_one(responder: _CountingResponder) -> None:
    DescriptorExhaustion.uninstall(_CountingResponder())

    DescriptorExhaustion.report(OSError(errno.EMFILE, "Too many open files"), "a site")

    assert responder.calls == 1


def test_a_watched_block_failing_for_want_of_descriptors_is_reported_and_raised_as_it_was(responder: _CountingResponder) -> None:
    raised = OSError(errno.EMFILE, "Too many open files")

    with pytest.raises(OSError) as caught, DescriptorExhaustion.watched("a site"):
        raise raised

    assert caught.value is raised
    assert responder.calls == 1


def test_a_watched_block_that_completes_calls_no_responder(responder: _CountingResponder) -> None:
    with DescriptorExhaustion.watched("a site"):
        pass

    assert responder.calls == 0
