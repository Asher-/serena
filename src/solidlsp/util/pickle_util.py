import builtins
import io
import pickle
from typing import Any


class _SafeUnpickler(pickle.Unpickler):
    _SAFE_BUILTINS = {
        "bool",
        "bytes",
        "bytearray",
        "complex",
        "dict",
        "float",
        "frozenset",
        "int",
        "list",
        "range",
        "set",
        "slice",
        "str",
        "tuple",
    }

    def __init__(self, file):
        super().__init__(file)

        self._allowed_globals = {("builtins", name): getattr(builtins, name) for name in self._SAFE_BUILTINS}

    def find_class(self, module, name):
        try:
            return self._allowed_globals[(module, name)]
        except KeyError:
            raise pickle.UnpicklingError(f"forbidden global: {module}.{name}") from None

    def persistent_load(self, pid):
        raise pickle.UnpicklingError("persistent IDs are not allowed")


class SafePickleLoader:
    def loads(self, data: bytes) -> Any:
        """
        :param data: a pickled object
        :return: the object, unpickled with only safe builtins allowed as globals
        """
        return _SafeUnpickler(io.BytesIO(data)).load()
