from concurrent.futures import ThreadPoolExecutor
import hashlib
import math
import os
import random
import string

import pytest

from local_coder.context.code_index import iter_tokens, tokenize
from local_coder.memory.context_store import ContextMemory
from local_coder.memory.native import _load, lexical_counts, python_counts


def test_streaming_tokenization_keeps_existing_identifier_semantics():
    text = "parseConfigFile read_file_safe HTTPServer users walking trees tested 123 _foo_ 界"
    assert list(iter_tokens(text)) == tokenize(text)
    assert tokenize("parseConfigFile") == ["parseconfigfile", "pars", "config", "fil"]


def test_missing_native_library_falls_back_without_changing_vectors(monkeypatch):
    monkeypatch.setenv("LOCAL_CODER_NATIVE_LIBRARY", "/does/not/exist.so")
    assert lexical_counts(iter(["parser", "config"])) == python_counts(["parser", "config"])
    assert _load("relative/path.so") is None


def test_streamed_subwords_keep_exact_feature_hash_semantics_for_large_tokens():
    token = "abcdef" * 4000
    expected = [0.0] * 256
    for feature in [token] + [token[n:n + 3] for n in range(len(token) - 2)]:
        value = hashlib.blake2b(feature.encode(), digest_size=4).digest()
        expected[value[1]] += 1 if value[2] & 1 else -1
    assert python_counts(iter([token])) == expected


@pytest.mark.parametrize("scale", [1e200, 1e-200])
def test_supplied_local_vectors_normalize_without_numeric_overflow(tmp_path, scale):
    class Backend:
        name = "local-numeric-boundary-fixture"

        def __call__(self, text):
            return [3 * scale, 4 * scale]

    memory = ContextMemory(tmp_path, embedding_backend=Backend())
    assert memory._vector("fixture") == pytest.approx([.6, .8])
    saved = memory.ingest("decision", "feline behavior")[0]
    hit = memory.search("cat")[0]
    assert hit["id"] == saved["id"]
    assert "vector similarity 1.000" in hit["reasons"][0]


@pytest.mark.skipif(not os.environ.get("LOCAL_CODER_NATIVE_LIBRARY"), reason="optional Rust library not enabled")
def test_native_counts_match_python_across_batches_and_threads():
    assert _load(os.environ["LOCAL_CODER_NATIVE_LIBRARY"]) is not None
    rng = random.Random(7391)
    corpus = [[], ["a"], ["parser"] * 1000, ["", "a\nb", "界"],
              ["".join(rng.choices(string.ascii_lowercase, k=rng.randint(2, 60))) for _ in range(800)]]
    for tokens in corpus:
        assert lexical_counts(iter(tokens)) == python_counts(tokens)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lexical_counts, [corpus[-1]] * 8)) == [python_counts(corpus[-1])] * 8


@pytest.mark.skipif(not os.environ.get("LOCAL_CODER_NATIVE_LIBRARY"), reason="optional Rust library not enabled")
def test_persisted_embedding_vectors_keep_exact_compatibility(tmp_path):
    text = "parseConfigFile validates user settings; HTTPServer creates requests " * 200
    raw = python_counts(tokenize(text))
    norm = math.sqrt(sum(value * value for value in raw)) or 1
    memory = ContextMemory(tmp_path)
    assert memory._vector(text) == [value / norm for value in raw]
    memory.ingest("decision", text)
    assert ContextMemory(tmp_path).search("configuration settings")
