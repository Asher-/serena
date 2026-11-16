"""
The process's response to running out of file descriptors.
"""

import errno
import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from typing import ClassVar

log = logging.getLogger(__name__)


class DescriptorExhaustion:
    """
    Descriptor exhaustion, met anywhere in the process: recognised in a failure's chain of causes and contexts, and answered
    once by the responder the process has installed. A process out of file descriptors fails every operation that opens
    one, so the responder ends it and lets its supervisor start a fresh one.
    """

    class Responder(ABC):
        """The process's answer to running out of file descriptors."""

        @abstractmethod
        def respond(self) -> None:
            """
            Ends the process, which can no longer do its work.
            """

    _lock: ClassVar[threading.Lock] = threading.Lock()
    _responder: ClassVar["DescriptorExhaustion.Responder | None"] = None
    _responded: ClassVar[bool] = False

    @classmethod
    def install(cls, responder: "DescriptorExhaustion.Responder") -> None:
        """
        Makes a responder the process's, in place of any installed before it. It is called on the first exhaustion
        reported after it is installed, and on no later one.

        :param responder: the responder
        """
        with cls._lock:
            cls._responder = responder
            cls._responded = False

    @classmethod
    def uninstall(cls, responder: "DescriptorExhaustion.Responder") -> None:
        """
        Removes a responder, if it is the installed one.

        :param responder: the responder
        """
        with cls._lock:
            if cls._responder is responder:
                cls._responder = None

    @staticmethod
    def is_exhaustion(exception: BaseException) -> bool:
        """
        Whether a failure stems from the process or the system running out of file descriptors.

        :param exception: the exception an operation failed with
        :return: whether the exception, or any exception reachable from it through causes and contexts, is an OS error
            with errno EMFILE or ENFILE
        """
        visited: set[int] = set()
        pending: list[BaseException] = [exception]
        while pending:
            current = pending.pop()
            if id(current) in visited:
                continue
            visited.add(id(current))
            if isinstance(current, OSError) and current.errno in (errno.EMFILE, errno.ENFILE):
                return True
            for linked in (current.__cause__, current.__context__):
                if linked is not None:
                    pending.append(linked)
        return False

    @classmethod
    def report(cls, exception: BaseException, site: str) -> None:
        """
        Answers a failure met at a site: one that stems from descriptor exhaustion is logged, and calls the installed
        responder if no exhaustion reported since its installation has called it.

        :param exception: the exception the operation at the site failed with
        :param site: the operation that failed, as the log names it
        """
        if not cls.is_exhaustion(exception):
            return
        log.critical("Serena ran out of file descriptors in %s: %s", site, exception, exc_info=exception)

        # the responder, called once and outside the lock, since ending the process may meet exhaustion again
        with cls._lock:
            responder = None if cls._responded else cls._responder
            if responder is not None:
                cls._responded = True
        if responder is not None:
            responder.respond()

    @classmethod
    @contextmanager
    def watched(cls, site: str) -> Iterator[None]:
        """
        A block whose failure is reported (see :meth:`report`) and then raised as it was.

        :param site: the operation the block performs, as the log names it
        """
        try:
            yield
        except BaseException as exception:
            cls.report(exception, site)
            raise
