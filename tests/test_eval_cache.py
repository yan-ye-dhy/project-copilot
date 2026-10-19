"""D10向量缓存离线验收：避免重复收费、契约隔离与损坏门禁。"""

import json
from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path

import pytest

from evals.cache import CachedEmbeddings, EmbeddingCacheError


class FakeEmbeddings:
    model = "embedding-test"
    expected_dimensions = 2
    batch_size = 2

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.closed = False

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(len(text)), float(ord(text[0]))] for text in texts]

    def close(self) -> None:
        self.closed = True


def cache(delegate: FakeEmbeddings, directory: Path, **kwargs) -> CachedEmbeddings:
    return CachedEmbeddings(delegate, directory, base_url=kwargs.get("base_url", "https://embed.example/v4/"))


def rewrite(path: Path, change) -> None:
    payload = json.loads(path.read_text())
    change(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_cache_deduplicates_batches_and_restores_input_order(tmp_path: Path) -> None:
    delegate = FakeEmbeddings()
    subject = cache(delegate, tmp_path)
    vectors = subject.embed_documents(["甲", "BB", "甲", "CCC", "BB"])

    assert vectors == [[1.0, ord("甲")], [2.0, ord("B")], [1.0, ord("甲")], [3.0, ord("C")], [2.0, ord("B")]]
    assert delegate.calls == [["甲", "BB"], ["CCC"]]
    assert subject.stats() == {"hit_texts": 0, "miss_texts": 3, "api_calls": 2}
    assert len(list(tmp_path.glob("*.json"))) == 3
    vectors[0][0] = -999.0
    assert vectors[2][0] == 1.0
    assert subject.embed_query("甲") == [1.0, ord("甲")]
    assert subject.stats() == {"hit_texts": 1, "miss_texts": 3, "api_calls": 2}
    assert not delegate.closed


def test_cache_persists_across_client_instances_and_shares_query_document(tmp_path: Path) -> None:
    original = cache(FakeEmbeddings(), tmp_path)
    expected = original.embed_documents(["same document or question"])[0]
    delegate = FakeEmbeddings()
    resumed = cache(delegate, tmp_path, base_url="https://embed.example/v4")

    assert resumed.embed_query("same document or question") == expected
    assert delegate.calls == []
    assert resumed.stats() == {"hit_texts": 1, "miss_texts": 0, "api_calls": 0}
    snapshot = resumed.stats()
    snapshot["api_calls"] = 20
    assert resumed.stats()["api_calls"] == 0


def test_cache_only_embeds_new_text_and_preserves_exact_whitespace(tmp_path: Path) -> None:
    delegate = FakeEmbeddings()
    subject = cache(delegate, tmp_path)
    subject.embed_documents(["a"])
    before = subject.stats()
    subject.embed_documents(["a", " a", " a"])
    delta = {key: subject.stats()[key] - before[key] for key in before}

    assert delegate.calls == [["a"], [" a"]]
    assert delta == {"hit_texts": 1, "miss_texts": 1, "api_calls": 1}


@pytest.mark.parametrize("alter", ["model", "dimensions", "base_url"])
def test_provider_model_and_dimensions_have_independent_cache_keys(tmp_path: Path, alter: str) -> None:
    cache(FakeEmbeddings(), tmp_path).embed_query("shared")
    delegate = FakeEmbeddings()
    base_url = "https://embed.example/v4/"
    if alter == "model":
        delegate.model = "another-embedding"
    elif alter == "dimensions":
        delegate.expected_dimensions = 3
        delegate.embed_documents = lambda texts: [[1.0, 2.0, 3.0] for _ in texts]
    else:
        base_url = "https://other.example/v4/"
    subject = cache(delegate, tmp_path, base_url=base_url)
    subject.embed_query("shared")

    assert subject.stats() == {"hit_texts": 0, "miss_texts": 1, "api_calls": 1}
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_cache_file_excludes_text_and_credentials_and_has_content_hash(tmp_path: Path) -> None:
    text = "SYNTHETIC_UNIQUE_TEXT_DO_NOT_PERSIST"
    subject = cache(FakeEmbeddings(), tmp_path)
    subject.embed_query(text)
    files = list(tmp_path.glob("*.json"))
    raw = files[0].read_text()
    payload = json.loads(raw)

    assert text not in raw
    assert payload["text_sha256"] == sha256(text.encode()).hexdigest()
    assert payload["contract"]["encoding_format"] == "float"
    assert set(payload) == {"schema_version", "contract", "text_sha256", "vector", "content_sha256"}
    content = {key: value for key, value in payload.items() if key != "content_sha256"}
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    assert payload["content_sha256"] == sha256(encoded).hexdigest()
    assert len(files[0].stem) == 64
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("texts", ["abc", b"abc", None, True, [""], ["  "], [True], ["valid", 7], ["valid", "\t"]])
def test_all_inputs_validated_before_any_request_or_cache_write(tmp_path: Path, texts) -> None:
    delegate = FakeEmbeddings()
    subject = cache(delegate, tmp_path / "not-created")
    with pytest.raises(ValueError, match="Embedding"):
        subject.embed_documents(texts)
    assert delegate.calls == []
    assert not subject.cache_dir.exists()


def test_empty_sequence_does_not_create_cache_or_make_request(tmp_path: Path) -> None:
    delegate = FakeEmbeddings()
    subject = cache(delegate, tmp_path / "not-created")
    assert subject.embed_documents([]) == []
    assert subject.stats() == {"hit_texts": 0, "miss_texts": 0, "api_calls": 0}
    assert delegate.calls == []
    assert not subject.cache_dir.exists()


@pytest.mark.parametrize("broken", [
    [1.0], [1.0, 2.0, 3.0], [True, 1.0], ["1", 2.0],
    [float("nan"), 2.0], [float("inf"), 2.0], [10**400, 2.0], (1.0, 2.0),
])
def test_bad_live_vectors_are_rejected_before_cache_persistence(tmp_path: Path, broken) -> None:
    delegate = FakeEmbeddings()
    delegate.embed_documents = lambda texts: [[1.0, 2.0], broken]
    subject = cache(delegate, tmp_path)
    with pytest.raises(EmbeddingCacheError, match="向量"):
        subject.embed_documents(["one", "two"])
    assert not list(tmp_path.glob("*.json"))
    assert subject.stats()["api_calls"] == 1


@pytest.mark.parametrize("response", [None, [], [[1.0, 2.0], [1.0, 2.0]], ([1.0, 2.0],)])
def test_bad_live_response_count_or_container_is_rejected(tmp_path: Path, response) -> None:
    delegate = FakeEmbeddings()
    delegate.embed_documents = lambda texts: response
    with pytest.raises(EmbeddingCacheError, match="条数"):
        cache(delegate, tmp_path).embed_query("one")
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.parametrize("change", [
    lambda value: value["vector"].__setitem__(0, 99.0),
    lambda value: value["vector"].__setitem__(0, True),
    lambda value: value["vector"].append(1.0),
    lambda value: value.__setitem__("text_sha256", "0" * 64),
    lambda value: value["contract"].__setitem__("model", "different"),
    lambda value: value["contract"].__setitem__("dimensions", float("nan")),
    lambda value: value.__setitem__("schema_version", True),
    lambda value: value.__setitem__("extra", "not accepted"),
    lambda value: value.__setitem__("content_sha256", "0" * 64),
])
def test_corrupt_cached_content_never_falls_back_to_paid_request(tmp_path: Path, change) -> None:
    cache(FakeEmbeddings(), tmp_path).embed_query("cached")
    path = next(tmp_path.glob("*.json"))
    rewrite(path, change)
    delegate = FakeEmbeddings()
    with pytest.raises(EmbeddingCacheError):
        cache(delegate, tmp_path).embed_documents(["new text before corrupt entry", "cached"])
    assert delegate.calls == []
    assert len(list(tmp_path.glob("*.json"))) == 1


@pytest.mark.parametrize("raw", [b"{", b"null", b"[]", b"\xff"])
def test_unreadable_cache_json_rejected_before_request(tmp_path: Path, raw: bytes) -> None:
    cache(FakeEmbeddings(), tmp_path).embed_query("cached")
    next(tmp_path.glob("*.json")).write_bytes(raw)
    delegate = FakeEmbeddings()
    with pytest.raises(EmbeddingCacheError):
        cache(delegate, tmp_path).embed_query("cached")
    assert delegate.calls == []


def test_cache_symlink_is_refused(tmp_path: Path) -> None:
    subject = cache(FakeEmbeddings(), tmp_path)
    subject.embed_query("cached")
    path = next(tmp_path.glob("*.json"))
    target = tmp_path / "outside.json"
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(EmbeddingCacheError, match="符号链接"):
        subject.embed_query("cached")


def test_atomic_save_failure_cleans_temp_file_and_does_not_claim_hit(tmp_path: Path, monkeypatch) -> None:
    subject = cache(FakeEmbeddings(), tmp_path)
    original = Path.replace

    def fail_replace(self, target):
        if self.name.startswith(".embedding-"):
            raise OSError("simulated disk problem")
        return original(self, target)

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(EmbeddingCacheError, match="原子保存"):
        subject.embed_query("cached")
    assert list(tmp_path.iterdir()) == []
    assert subject.stats() == {"hit_texts": 0, "miss_texts": 1, "api_calls": 1}


def test_failed_batch_counts_attempt_and_preserves_previous_successful_batch(tmp_path: Path) -> None:
    delegate = FakeEmbeddings()
    original = delegate.embed_documents

    def fail_second(texts):
        if texts == ["three"]:
            raise RuntimeError("synthetic provider failure")
        return original(texts)

    delegate.embed_documents = fail_second
    subject = cache(delegate, tmp_path)
    with pytest.raises(RuntimeError, match="synthetic provider failure"):
        subject.embed_documents(["one", "two", "three"])
    assert subject.stats() == {"hit_texts": 0, "miss_texts": 3, "api_calls": 2}
    assert len(list(tmp_path.glob("*.json"))) == 2
    assert subject.embed_documents(["one", "two"]) == [[3.0, ord("o")], [3.0, ord("t")]]
    assert subject.stats()["api_calls"] == 2


@pytest.mark.parametrize("attribute,value", [
    ("model", ""), ("model", True), ("expected_dimensions", True),
    ("expected_dimensions", 0), ("batch_size", True), ("batch_size", 0),
])
def test_bad_delegate_contract_rejected_without_request(tmp_path: Path, attribute: str, value) -> None:
    delegate = FakeEmbeddings()
    setattr(delegate, attribute, value)
    with pytest.raises(EmbeddingCacheError):
        cache(delegate, tmp_path)
    assert delegate.calls == []


@pytest.mark.parametrize("base_url", [
    "", True, "https://user:secret@embed.example/v4", "https://embed.example/v4?api_key=secret",
    "https://embed.example/v4#secret", "file:///tmp/cache", "https://embed.example/ with-space",
])
def test_provider_url_rejects_embedded_credentials_or_non_http(tmp_path: Path, base_url) -> None:
    delegate = FakeEmbeddings()
    with pytest.raises(EmbeddingCacheError):
        cache(delegate, tmp_path, base_url=base_url)
    assert delegate.calls == []


def test_protocol_without_batch_size_uses_default_64(tmp_path: Path) -> None:
    class MinimalEmbeddings:
        model = "minimal"
        expected_dimensions = 1

        def __init__(self):
            self.batches = []

        def embed_documents(self, texts):
            self.batches.append(len(texts))
            return [[1.0] for _ in texts]

    delegate = MinimalEmbeddings()
    subject = cache(delegate, tmp_path)
    subject.embed_documents([f"text-{index}" for index in range(65)])
    assert delegate.batches == [64, 1]
    assert subject.stats() == {"hit_texts": 0, "miss_texts": 65, "api_calls": 2}
