"""
A safe pickle loader unpickles pickled bytes as it unpickles a pickled file: allowed objects are returned, and any other
global is refused with the same error.
"""

import pickle
from pathlib import Path

import pytest

from solidlsp.util.pickle_util import SafePickleLoader


class _Allowed:
    def __init__(self, value: int) -> None:
        self.value = value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Allowed) and other.value == self.value

    def __hash__(self) -> int:
        return hash(self.value)


def test_loads_returns_an_entry_of_allowed_types() -> None:
    entry = {"symbols": [{"name": "f", "kind": 12}], "allowed": _Allowed(3)}

    assert SafePickleLoader(allowed_classes=[_Allowed]).loads(pickle.dumps(entry)) == entry


def test_loads_refuses_a_disallowed_class_with_the_error_load_raises(tmp_path: Path) -> None:
    data = pickle.dumps({"disallowed": _Allowed(3)})
    path = tmp_path / "entry.pkl"
    path.write_bytes(data)
    loader = SafePickleLoader()

    with pytest.raises(pickle.UnpicklingError) as from_file:
        loader.load(str(path))
    with pytest.raises(pickle.UnpicklingError) as from_bytes:
        loader.loads(data)

    assert str(from_bytes.value) == str(from_file.value)
