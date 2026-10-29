"""Explicitly enabled Rust feature hashing with a compatible Python fallback."""
from __future__ import annotations

import ctypes
from functools import lru_cache
import hashlib
import os
from pathlib import Path
from collections.abc import Iterable
from itertools import islice


@lru_cache(maxsize=4)
def _load(path: str):
    if not path or not Path(path).is_absolute():
        return None
    try:
        library = ctypes.CDLL(path)
        library.local_coder_abi_version.argtypes = []
        library.local_coder_abi_version.restype = ctypes.c_uint32
        if library.local_coder_abi_version() != 1:
            return None
        function = library.local_coder_lexical_counts
        function.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_double), ctypes.c_size_t]
        function.restype = ctypes.c_int
        return function
    except (OSError, AttributeError):
        return None


def python_counts(tokens: Iterable[str]) -> list[float]:
    vector = [0.0] * 256
    for token in tokens:
        features = [token] + [token[n:n + 3] for n in range(max(0, len(token) - 2))]
        for feature in features:
            value = hashlib.blake2b(feature.encode(), digest_size=4).digest()
            vector[value[1]] += 1 if value[2] & 1 else -1
    return vector


def lexical_counts(tokens: Iterable[str]) -> list[float]:
    """Keep Python tokenization and exact persisted vector semantics unchanged.

    The path is trusted host configuration: loading a shared library executes
    native code. Never resolve it from retrieved data or a model proposal.
    """
    function = _load(os.environ.get("LOCAL_CODER_NATIVE_LIBRARY", ""))
    if function is None:
        return python_counts(tokens)
    # Bound transient token strings and native input bytes rather than joining
    # the whole document. Both implementations accumulate exact integer counts.
    result = [0.0] * 256
    iterator = iter(tokens)
    while batch := list(islice(iterator, 256)):
        vector = None
        try:
            data = None if any(not token or "\n" in token for token in batch) else "\n".join(batch).encode("ascii")
        except UnicodeEncodeError:
            data = None
        if data is not None and len(data) <= 16 * 1024 * 1024:
            output = (ctypes.c_double * 256)()
            if function(data, len(data), output, 256) == 0:
                vector = output
        if vector is None:
            vector = python_counts(batch)
        for index, value in enumerate(vector):
            result[index] += value
    return result
