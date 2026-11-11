import os
import threading
from pathlib import Path

import pytest

from solidlsp.util.cache import load_cache, save_cache

_THREAD_COUNT = 8
_SAVES_PER_THREAD = 50


def test_concurrent_saves_of_one_cache_leave_a_loadable_cache_and_no_other_file(tmp_path: Path) -> None:
    cache_path = str(tmp_path / "symbols.pkl")
    saved_objects = [{"thread": i, "payload": list(range(2000))} for i in range(_THREAD_COUNT)]
    errors: list[BaseException] = []

    def save_repeatedly(obj: dict) -> None:
        try:
            for _ in range(_SAVES_PER_THREAD):
                save_cache(cache_path, 1, obj)
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=save_repeatedly, args=(obj,)) for obj in saved_objects]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert load_cache(cache_path, 1) in saved_objects
    assert os.listdir(tmp_path) == ["symbols.pkl"]


def test_a_failed_save_leaves_the_destination_directory_as_it_was(tmp_path: Path) -> None:
    cache_path = tmp_path / "symbols.pkl"
    save_cache(str(cache_path), 1, {"payload": "saved before"})
    contents_before = cache_path.read_bytes()

    with pytest.raises(TypeError, match="pickle"):
        save_cache(str(cache_path), 1, {"payload": threading.Lock()})

    assert os.listdir(tmp_path) == ["symbols.pkl"]
    assert cache_path.read_bytes() == contents_before
