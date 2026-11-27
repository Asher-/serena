"""
A safe pickle loader unpickles pickled bytes made of safe builtins, and refuses any other global.
"""

import pickle

import pytest

from solidlsp.util.pickle_util import SafePickleLoader


class _Disallowed:
    def __init__(self, value: int) -> None:
        self.value = value


def test_loads_returns_an_entry_of_safe_builtins() -> None:
    entry = {"symbols": [{"name": "f", "kind": 12}], "range": (1, 2)}

    assert SafePickleLoader().loads(pickle.dumps(entry)) == entry


def test_loads_refuses_a_disallowed_class() -> None:
    data = pickle.dumps({"disallowed": _Disallowed(3)})

    with pytest.raises(pickle.UnpicklingError):
        SafePickleLoader().loads(data)
