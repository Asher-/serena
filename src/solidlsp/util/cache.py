import logging
from typing import Any, Optional

from sensai.util.pickle import load_pickle

log = logging.getLogger(__name__)


import contextlib
import os
import pickle


def load_cache(path: str, version: Any) -> Optional[Any]:
    data = load_pickle(path)
    if not isinstance(data, dict) or "__cache_version" not in data:
        log.info("Cache is outdated (expected version %s). Ignoring cache at %s", version, path)
        return None
    saved_version = data["__cache_version"]
    if saved_version != version:
        log.info("Cache is outdated (expected version %s, got %s). Ignoring cache at %s", version, saved_version, path)
        return None
    return data["obj"]


def save_cache(path: str, version: Any, obj: Any) -> None:
    """
    Atomically persist ``obj`` to ``path`` under ``version``.

    A partially-written pickle previously produced a corrupt cache after a mid-write
    crash because the destination was written in place. We now write to a sibling
    ``<path>.tmp``, fsync it, and replace the destination in a single atomic rename.
    The temporary file is removed on failure so orphans do not accumulate.
    """
    data = {"__cache_version": version, "obj": obj}
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "wb") as f:
            pickle.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise
