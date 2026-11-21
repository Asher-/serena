# SPDX-License-Identifier: GPL-3.0-or-later

import logging
from collections.abc import Callable, Iterator
from typing import TypeVar

T = TypeVar("T")

log = logging.getLogger(__name__)


def iter_subclasses(
    cls: type[T], recursive: bool = True, inclusion_predicate: Callable[[type[T]], bool] = lambda t: True
) -> Iterator[type[T]]:
    """Iterate over all subclasses of a class, yielding each subclass once (even if it is reachable via multiple base classes).

    :param cls: The class whose subclasses to iterate over.
    :param recursive: If True, also iterate over all subclasses of all subclasses.
    :param inclusion_predicate: a predicate function to decide whether to include a subclass in the result
    """
    seen: set[type] = set()

    def iterate(c: type[T]) -> Iterator[type[T]]:
        for subclass in c.__subclasses__():
            if subclass in seen:
                continue
            seen.add(subclass)
            if inclusion_predicate(subclass):
                yield subclass
            if recursive:
                yield from iterate(subclass)

    yield from iterate(cls)
