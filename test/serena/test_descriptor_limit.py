"""
The daemon starts with a soft limit on open file descriptors sized for its work.
"""

import logging
import resource
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from serena.util.descriptor_limit import DescriptorLimit


@contextmanager
def _soft_limit(soft: int) -> Iterator[int]:
    """
    The process's soft descriptor limit set to a value for the block, and restored when it ends.

    :param soft: the soft limit the block starts with
    :return: the hard limit
    """
    original, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    try:
        yield hard
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (original, hard))


def _target(hard: int) -> int:
    return 10240 if hard == resource.RLIM_INFINITY else min(10240, hard)


def _limit_message(caplog: pytest.LogCaptureFixture) -> str:
    messages = [record.getMessage() for record in caplog.records if "descriptor" in record.getMessage()]
    assert len(messages) == 1, messages
    return messages[0]


def test_startup_raises_a_low_soft_descriptor_limit_to_its_target_capped_at_the_hard_limit_and_logs_both_limits(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A daemon started under a soft limit of 256 open file descriptors runs with a soft limit of 10240, or of the hard limit
    when that is lower, and logs the soft and hard limits in effect.
    """
    with _soft_limit(256) as hard:
        with caplog.at_level(logging.INFO):
            DescriptorLimit.raise_soft_limit()
        raised, _ = resource.getrlimit(resource.RLIMIT_NOFILE)

    assert raised == _target(hard)
    message = _limit_message(caplog)
    assert f"soft {_target(hard)}" in message, message
    assert f"hard {'unlimited' if hard == resource.RLIM_INFINITY else hard}" in message, message


def test_startup_leaves_a_soft_descriptor_limit_already_at_its_target_as_it_is_and_logs_it(caplog: pytest.LogCaptureFixture) -> None:
    """A daemon started under a soft limit already at its target runs with that limit, and logs it."""
    _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)

    with _soft_limit(_target(hard)):
        with caplog.at_level(logging.INFO):
            DescriptorLimit.raise_soft_limit()
        kept, _ = resource.getrlimit(resource.RLIMIT_NOFILE)

    assert kept == _target(hard)
    assert f"soft {_target(hard)}" in _limit_message(caplog)
