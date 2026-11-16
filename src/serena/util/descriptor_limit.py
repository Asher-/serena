"""
The daemon's limit on open file descriptors.
"""

import logging
import resource

log = logging.getLogger(__name__)


class DescriptorLimit:
    """
    The soft limit on the daemon's open file descriptors, sized for a daemon that holds the pipes of many language servers
    at once.
    """

    _TARGET = 10240
    """the soft limit the daemon runs with, unless the hard limit is lower."""

    @classmethod
    def raise_soft_limit(cls) -> None:
        """
        Raises the soft limit on open file descriptors to its target, or to the hard limit where that is lower, and logs
        the soft and hard limits in effect. A soft limit already at least that high is left as it is.
        """
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = cls._TARGET if hard == resource.RLIM_INFINITY else min(cls._TARGET, hard)
        if soft != resource.RLIM_INFINITY and soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))

        # the limits in effect
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        log.info("Open file descriptor limits in effect: soft %s, hard %s", cls._shown(soft), cls._shown(hard))

    @staticmethod
    def _shown(limit: int) -> str:
        return "unlimited" if limit == resource.RLIM_INFINITY else str(limit)
