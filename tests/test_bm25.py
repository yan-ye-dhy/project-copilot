"""D13词项契约：真实D3身份、离线排序与只读SDK，不调用Embedding或模型。"""

from collections.abc import Callable
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from qdrant_client import QdrantClient, models

from project_copilot.rag.bm25 import (
    BM25Error,
    BM25Index,
    ChineseTokenizer,
    read_completed_chunks,
)
from project_copilot.rag.loaders import load_document
from project_copilot.rag.splitters import SplitConfig, split_document
from project_copilot.schemas.documents import DocumentChunk
from project_copilot.schemas.retrieval import CompletedDocument, DenseIndexSpec, IndexManifest


def _corpus(directory: Path, texts: dict[str, str]) -> tuple[IndexManifest, list[DocumentChunk]]:
    """所有有效文档/片段ID都来自D3真实文件加载与切分，禁用随机伪造ID。"""
    chunks: list[DocumentChunk] = []
    completed = []
    config = SplitConfig()
    for name, text in texts.items():
        path = directory / name
        path.write_text(text, encoding="utf-8")
        document = load_document(path)
        parts = split_document(document, config)
        chunks.extend(parts)
        if parts:
            completed.append(CompletedDocument(
                document_id=document.document_id,
                content_hash=document.content_hash,
                source_name=document.source_name,
                chunk_ids=tuple(part.metadata.chunk_id for part in parts),
            ))
    return IndexManifest(
        spec=DenseIndexSpec(
            collection_name="pc_test_bm25", embedding_model="embedding-3",
            dimensions=3, splitter_version=config.index_version,
        ),
        documents=tuple(completed),
    ), chunks


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> tuple[IndexManifest, list[DocumentChunk]]:
    return _corpus(tmp_path_factory.mktemp("bm25-corpus"), {
        "upload.md": "HTTP_422 /documents/upload 上传文件为空时返回错误。\n"
        "PROJECT_COPILOT_ZHIPU_API_KEY load_settings embedding-3 --env-file .env.example",
        "database.txt": "PostgreSQL 数据库事务保证工单一致性。原子提交与回滚。",
        "health.txt": "健康检查接口 /health 报告服务状态。",
    })


@pytest.fixture(scope="module")
def index(corpus: tuple[IndexManifest, list[DocumentChunk]]) -> BM25Index:
    return BM25Index.build(*corpus)


@pytest.fixture(scope="module")
def tokenizer() -> ChineseTokenizer:
    return ChineseTokenizer()


def _rewrite_snapshot(path: Path, change: Callable[[dict], None]) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    change(payload["snapshot"])
    # 模拟有一致内容哈希但规则/依赖已经变化的快照；仍必须拒绝复用。
    encoded = json.dumps(payload["snapshot"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload["fingerprint"] = "bm25-v1-" + sha256(encoded.encode("utf-8")).hexdigest()
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _payload(chunk: DocumentChunk, manifest: IndexManifest) -> dict[str, object]:
    return {
        "text": chunk.text,
        "metadata": chunk.metadata.model_dump(mode="json"),
        "dense_index_version": manifest.spec.index_version,
    }


def _insert(client: QdrantClient, manifest: IndexManifest, chunks: list[DocumentChunk]) -> None:
    client.upsert(manifest.spec.collection_name, points=[
        models.PointStruct(id=str(chunk.metadata.chunk_id), vector={"dense": [1.0, 0.0, 0.0]},
                           payload=_payload(chunk, manifest))
        for chunk in chunks
    ])


def test_tokenizer_preserves_technical_terms_and_normalizes_unicode(tokenizer: ChineseTokenizer) -> None:
    assert tokenizer.tokenize(
        "HTTP_422 /health /documents/upload load_settings embedding-3 embedding-4 "
        "--env-file PROJECT_COPILOT_ZHIPU_API_KEY ＬＯＡＤ＿ＳＥＴＴＩＮＧＳ"
    ) == [
        "http_422", "/health", "/documents/upload", "load_settings", "embedding-3",
        "embedding-4", "--env-file", "project_copilot_zhipu_api_key", "load_settings",
    ]


def test_tokenizer_segments_chinese_without_losing_embedded_identifiers(tokenizer: ChineseTokenizer) -> None:
    tokens = tokenizer.tokenize("上传文件发生HTTP_422，使用load_settings读取配置。")
    assert "上传" in tokens and "文件" in tokens and "配置" in tokens
    assert "http_422" in tokens and "load_settings" in tokens
    assert tokenizer.tokenize(" \n，。！？——！ ") == []
    with pytest.raises(BM25Error, match="文本"):
        tokenizer.tokenize(None)


def test_chinese_retrieval_returns_real_metadata_and_no_unknown_matches(index: BM25Index) -> None:
    hits = index.search("数据库事务")
    assert hits and hits[0].metadata.source_name == "database.txt"
    assert "原子提交" in hits[0].text
    assert hits[0].metadata == next(chunk.metadata for chunk in index.snapshot.chunks
                                     if chunk.metadata.source_name == "database.txt")
    assert index.search("quantumunrelated987") == []
    assert index.search("。。。") == []


@pytest.mark.parametrize("query", ["HTTP_422", "/documents/upload", "load_settings", "embedding-3", "--env-file"])
def test_exact_identifiers_match_but_other_embedding_version_does_not(index: BM25Index, query: str) -> None:
    assert [hit.metadata.source_name for hit in index.search(query)] == ["upload.md"]
    assert index.search("embedding-4") == []


def test_query_and_document_share_normalization_rules(index: BM25Index) -> None:
    assert index.search("ＨＴＴＰ＿４２２") == index.search("http_422")
    assert index.search("/HEALTH")[0].metadata.source_name == "health.txt"


@pytest.mark.parametrize("top_k", [0, 101, True, 1.5, "5"])
def test_top_k_requires_bounded_integer(index: BM25Index, top_k: object) -> None:
    with pytest.raises(BM25Error, match="top_k"):
        index.search("配置", top_k=top_k)


@pytest.mark.parametrize("query", ["", "  \n", None])
def test_empty_or_nontext_queries_fail_before_scoring(index: BM25Index, query: object) -> None:
    with pytest.raises(BM25Error, match="非空文本"):
        index.search(query)


def test_tiny_all_common_corpus_scores_are_finite_positive_and_can_exceed_one(tmp_path: Path) -> None:
    manifest, chunks = _corpus(tmp_path, {"only.txt": "shared shared shared"})
    result = BM25Index.build(manifest, chunks).search("shared shared shared shared")
    assert len(result) == 1 and result[0].score > 1
    # BM25L原始词频相关分数无概率上限，不按置信度裁剪到[0,1]。
    manifest, chunks = _corpus(tmp_path, {"a.txt": "shared", "b.txt": "shared\n"})
    results = BM25Index.build(manifest, chunks).search("shared")
    assert len(results) == 2 and all(hit.score > 0 for hit in results)


def test_ties_and_snapshot_identity_ignore_input_order(tmp_path: Path) -> None:
    manifest, chunks = _corpus(tmp_path, {"a.txt": "shared", "b.txt": "shared\n"})
    first = BM25Index.build(manifest, chunks)
    second = BM25Index.build(manifest, list(reversed(chunks)))
    hits = first.search("shared")
    assert hits[0].score == pytest.approx(hits[1].score)
    assert [str(hit.metadata.chunk_id) for hit in hits] == sorted(str(chunk.metadata.chunk_id) for chunk in chunks)
    assert second.search("shared") == hits and second.fingerprint == first.fingerprint
    assert len(first.search("shared", top_k=1)) == 1


def test_empty_and_punctuation_corpora_have_no_division_by_zero(tmp_path: Path) -> None:
    manifest, chunks = _corpus(tmp_path, {"empty.txt": ""})
    empty = BM25Index.build(manifest, chunks)
    assert empty.search("query") == []
    manifest, chunks = _corpus(tmp_path, {"punctuation.txt": "。。。！！，？？"})
    assert len(chunks) == 1
    assert BM25Index.build(manifest, chunks).search("query") == []


def test_b_one_rejects_mixed_empty_tokens_before_scoring_but_all_terms_remain_supported(tmp_path: Path) -> None:
    manifest, chunks = _corpus(tmp_path, {"terms.txt": "shared", "punctuation.txt": "。。。！？"})
    default = BM25Index.build(manifest, chunks)
    assert [hit.metadata.source_name for hit in default.search("shared")] == ["terms.txt"]
    snapshot = default.snapshot.model_copy(update={
        "config": default.snapshot.config.model_copy(update={"b": 1.0}),
    })
    # 库对空词项片段在b=1时会计算0/0；在建索引阶段给出可行动的领域错误。
    with pytest.raises(BM25Error, match="存在空词项片段时不支持b=1"):
        BM25Index(snapshot)

    manifest, chunks = _corpus(tmp_path, {"normal.txt": "shared shared"})
    supported = BM25Index.build(manifest, chunks)
    snapshot = supported.snapshot.model_copy(update={
        "config": supported.snapshot.config.model_copy(update={"b": 1.0}),
    })
    assert BM25Index(snapshot).search("shared")[0].score > 0


def test_build_rejects_missing_duplicate_and_outside_chunks(corpus, tmp_path: Path) -> None:
    manifest, chunks = corpus
    _, outside = _corpus(tmp_path, {"outside.txt": "未完成文档"})
    for parts in (chunks[:-1], [*chunks, chunks[0]], [*chunks, *outside]):
        with pytest.raises(BM25Error):
            BM25Index.build(manifest, parts)


@pytest.mark.parametrize("field,value", [("source_name", "renamed.txt"), ("content_hash", "a" * 64), ("index_version", "other-split")])
def test_build_rejects_chunk_metadata_drift(corpus, field: str, value: str) -> None:
    manifest, chunks = corpus
    changed = chunks[0].model_copy(update={"metadata": chunks[0].metadata.model_copy(update={field: value})})
    with pytest.raises(BM25Error, match="身份、来源或切分版本"):
        BM25Index.build(manifest, [changed, *chunks[1:]])


def test_build_rejects_text_mutation_even_with_unchanged_metadata(corpus) -> None:
    manifest, chunks = corpus
    changed = chunks[0].model_copy(update={"text": chunks[0].text + " 改动"})
    with pytest.raises(BM25Error, match="原文与稳定chunk_id"):
        BM25Index.build(manifest, [changed, *chunks[1:]])


def test_json_snapshot_roundtrip_has_no_vectors_and_never_overwrites(index: BM25Index, tmp_path: Path) -> None:
    path = tmp_path / "nested" / "bm25.json"
    index.save(path)
    original = path.read_bytes()
    restored = BM25Index.load(path, index.snapshot.manifest)
    assert restored.fingerprint == index.fingerprint
    assert restored.search("HTTP_422") == index.search("HTTP_422")
    payload = json.loads(original)
    assert set(payload) == {"snapshot", "fingerprint"}
    assert set(payload["snapshot"]) == {"format_version", "manifest", "tokenizer", "config", "chunks"}
    assert all(set(chunk) == {"text", "metadata"} for chunk in payload["snapshot"]["chunks"])
    with pytest.raises(BM25Error, match="目标文件须不存在"):
        index.save(path)
    assert path.read_bytes() == original


@pytest.mark.parametrize("change", ["remove", "add", "rename", "hash", "collection", "dimensions", "model", "splitter"])
def test_manifest_drift_invalidates_snapshot(index: BM25Index, tmp_path: Path, change: str) -> None:
    path = tmp_path / "bm25.json"
    index.save(path)
    manifest = index.snapshot.manifest
    documents = list(manifest.documents)
    if change == "remove":
        changed = manifest.model_copy(update={"documents": tuple(documents[:-1])})
    elif change == "add":
        extra, _ = _corpus(tmp_path, {"new.txt": "新增文档"})
        changed = manifest.model_copy(update={"documents": (*manifest.documents, *extra.documents)})
    elif change in {"rename", "hash"}:
        field, value = ("source_name", "new-name.txt") if change == "rename" else ("content_hash", "a" * 64)
        documents[0] = documents[0].model_copy(update={field: value})
        changed = manifest.model_copy(update={"documents": tuple(documents)})
    else:
        field, value = {
            "collection": ("collection_name", "pc_other"), "dimensions": ("dimensions", 4),
            "model": ("embedding_model", "embedding-4"), "splitter": ("splitter_version", "new-split"),
        }[change]
        changed = manifest.model_copy(update={"spec": manifest.spec.model_copy(update={field: value})})
    with pytest.raises(BM25Error, match="成功清单已变化"):
        BM25Index.load(path, changed)


def test_snapshot_tamper_is_rejected_before_rebuilding(index: BM25Index, tmp_path: Path) -> None:
    path = tmp_path / "bm25.json"
    index.save(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["snapshot"]["chunks"][0]["text"] += " 篡改"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(BM25Error, match="内容指纹不匹配"):
        BM25Index.load(path, index.snapshot.manifest)
    _rewrite_snapshot(path, lambda snapshot: None)
    with pytest.raises(BM25Error, match="原文与稳定chunk_id"):
        BM25Index.load(path, index.snapshot.manifest)


@pytest.mark.parametrize("change", ["dictionary", "tokenizer", "dependency", "numpy"])
def test_coherent_old_snapshot_rejects_tokenizer_and_dependency_drift(index: BM25Index, tmp_path: Path, change: str) -> None:
    path = tmp_path / "bm25.json"
    index.save(path)

    def alter(snapshot: dict) -> None:
        if change == "dependency":
            snapshot["config"]["implementation"] = "old-library-version"
        elif change == "numpy":
            snapshot["config"]["numpy_version"] = "old-library-version"
        else:
            key = "dictionary_sha256" if change == "dictionary" else "rule"
            snapshot["tokenizer"][key] = "old-rule-or-dictionary"

    _rewrite_snapshot(path, alter)
    with pytest.raises(BM25Error, match="规则、词典或依赖版本已变化"):
        BM25Index.load(path, index.snapshot.manifest)


@pytest.mark.parametrize("content", ["not-json", "[]", "{}", '{"snapshot":{},"fingerprint":"bad"}'])
def test_snapshot_bad_or_missing_file_has_domain_error(index: BM25Index, tmp_path: Path, content: str) -> None:
    path = tmp_path / "broken.json"
    with pytest.raises(BM25Error):
        BM25Index.load(path, index.snapshot.manifest)
    path.write_text(content, encoding="utf-8")
    with pytest.raises(BM25Error):
        BM25Index.load(path, index.snapshot.manifest)


def test_real_local_sdk_reads_exact_completed_ids_and_never_fetches_vectors(corpus, tmp_path: Path) -> None:
    manifest, chunks = corpus
    _, outside = _corpus(tmp_path, {"unfinished.txt": "成功清单外的未完成点"})
    with closing(QdrantClient(":memory:")) as client:
        client.create_collection(manifest.spec.collection_name, vectors_config={
            "dense": models.VectorParams(size=3, distance=models.Distance.COSINE),
        })
        _insert(client, manifest, [*chunks, *outside])
        proxy = Mock(wraps=client)
        actual = read_completed_chunks(proxy, manifest)
        assert actual == sorted(chunks, key=lambda chunk: str(chunk.metadata.chunk_id))
        assert set(proxy.method_calls[0].args) == {manifest.spec.collection_name}
        assert [call[0] for call in proxy.method_calls] == ["get_collection", "retrieve"]
        kwargs = proxy.retrieve.call_args.kwargs
        assert set(kwargs["ids"]) == {str(chunk.metadata.chunk_id) for chunk in chunks}
        assert kwargs["with_payload"] is True and kwargs["with_vectors"] is False
        assert str(outside[0].metadata.chunk_id) not in kwargs["ids"]
        assert client.count(manifest.spec.collection_name, exact=True).count == len(chunks) + 1


@pytest.mark.parametrize("mutation", ["missing", "metadata", "dense_version", "point_id", "text"])
def test_qdrant_completed_points_must_match_manifest(corpus, mutation: str) -> None:
    manifest, chunks = corpus
    with closing(QdrantClient(":memory:")) as client:
        client.create_collection(manifest.spec.collection_name, vectors_config={
            "dense": models.VectorParams(size=3, distance=models.Distance.COSINE),
        })
        _insert(client, manifest, chunks if mutation != "missing" else chunks[1:])
        if mutation != "missing":
            records = client.retrieve(manifest.spec.collection_name,
                                      ids=[str(chunk.metadata.chunk_id) for chunk in chunks], with_payload=True)
            first = records[0]
            if mutation == "metadata":
                first.payload["metadata"]["source_name"] = "wrong.txt"
            elif mutation == "dense_version":
                first.payload["dense_index_version"] = "other-version"
            elif mutation == "point_id":
                first.id = str(chunks[-1].metadata.document_id)
            else:
                first.payload["text"] += " changed"
            proxy = Mock(wraps=client)
            proxy.retrieve.return_value = records
        else:
            proxy = client
        with pytest.raises(BM25Error):
            read_completed_chunks(proxy, manifest)


@pytest.mark.parametrize("dimension,distance,sparse", [
    (4, models.Distance.COSINE, None),
    (3, models.Distance.DOT, None),
    (3, models.Distance.COSINE, {"sparse": models.SparseVectorParams()}),
])
def test_qdrant_dense_only_contract_is_checked_before_point_reads(corpus, dimension, distance, sparse) -> None:
    manifest, _ = corpus
    with closing(QdrantClient(":memory:")) as client:
        client.create_collection(manifest.spec.collection_name, vectors_config={
            "dense": models.VectorParams(size=dimension, distance=distance),
        }, sparse_vectors_config=sparse)
        proxy = Mock(wraps=client)
        with pytest.raises(BM25Error, match="维度、距离或Dense-only"):
            read_completed_chunks(proxy, manifest)
        proxy.retrieve.assert_not_called()


def test_empty_manifest_reads_no_points_and_never_builds_collection(corpus) -> None:
    manifest, _ = corpus
    manifest = manifest.model_copy(update={"documents": ()})
    with closing(QdrantClient(":memory:")) as client:
        client.create_collection(manifest.spec.collection_name, vectors_config={
            "dense": models.VectorParams(size=3, distance=models.Distance.COSINE),
        })
        proxy = Mock(wraps=client)
        assert read_completed_chunks(proxy, manifest) == []
        assert [call[0] for call in proxy.method_calls] == ["get_collection"]
