import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from memory_core import (
    EmbeddedMemoryService,
    GeminiEmbeddingStore,
    MemoryEnrichmentError,
    MemoryMetadataAnalyzer,
)


class _ImmediateExecutor:
    def submit(self, fn, *args):
        fn(*args)


class _Analyzer:
    enabled = True

    def analyze(self, _content):
        return {
            "valence": 0.12,
            "arousal": 0.88,
            "tags": ["害怕", "安抚"],
            "name": "被安抚的夜晚",
            "importance": 8,
        }


class _Embeddings:
    enabled = True

    def __init__(self):
        self.saved = []

    def generate_and_store(self, bucket_id, content):
        self.saved.append((bucket_id, content))
        return True


class _Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class MemoryEnrichmentTests(unittest.TestCase):
    def test_analyzer_parses_and_sanitizes_metadata(self):
        payload = {
            "choices": [{
                "message": {
                    "content": "```json\n" + json.dumps({
                        "valence": 0.18,
                        "arousal": 0.91,
                        "tags": ["[[争执]]", "担心", "担心"],
                        "suggested_name": "深夜争执后的担心",
                        "importance": 11,
                    }, ensure_ascii=False) + "\n```"
                }
            }]
        }
        analyzer = MemoryMetadataAnalyzer(api_key="test-key")
        with patch("memory_core.enrichment.requests.post", return_value=_Response(payload)):
            result = analyzer.analyze("一段明显焦虑而激烈的记忆")

        self.assertEqual(result["valence"], 0.18)
        self.assertEqual(result["arousal"], 0.91)
        self.assertEqual(result["tags"], ["争执", "担心"])
        self.assertEqual(result["importance"], 10)

    def test_analyzer_rejects_missing_emotion_instead_of_using_defaults(self):
        payload = {
            "choices": [{"message": {"content": '{"tags": ["普通"]}'}}]
        }
        analyzer = MemoryMetadataAnalyzer(api_key="test-key")
        with patch("memory_core.enrichment.requests.post", return_value=_Response(payload)):
            with self.assertRaises(MemoryEnrichmentError):
                analyzer.analyze("不能假装有情感向量")

    def test_analyzer_reuses_openrouter_when_direct_key_is_absent(self):
        with patch.dict(
            os.environ,
            {"OPENROUTER_API_KEY": "openrouter-key"},
            clear=True,
        ):
            analyzer = MemoryMetadataAnalyzer.from_env()
        self.assertEqual(analyzer.api_key, "openrouter-key")
        self.assertEqual(analyzer.base_url, "https://openrouter.ai/api/v1")
        self.assertEqual(analyzer.model, "deepseek/deepseek-chat")

    def test_embedding_is_stored_with_its_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = GeminiEmbeddingStore(
                os.path.join(temp_dir, "embeddings.db"), api_key="gemini-key"
            )
            payload = {"embedding": {"values": [0.1, 0.2, 0.3]}}
            with patch(
                "memory_core.enrichment.requests.post", return_value=_Response(payload)
            ):
                self.assertTrue(store.generate_and_store("bucket-1", "记忆正文"))
            with sqlite3.connect(store.db_path) as conn:
                row = conn.execute(
                    "SELECT embedding, model FROM embeddings WHERE bucket_id=?",
                    ("bucket-1",),
                ).fetchone()
            self.assertEqual(json.loads(row[0]), [0.1, 0.2, 0.3])
            self.assertEqual(row[1], "gemini-embedding-2")

    def test_pending_memory_becomes_real_vector_without_reactivation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = EmbeddedMemoryService(temp_dir, ["char1"])
            bucket_id, _ = service.save(
                "需要补打标的记忆",
                "char1",
                source="self_saved",
                enrichment_status="pending",
                embedding_status="pending",
                created="2026-07-16T01:00:00+00:00",
                last_active="2026-07-16T01:00:00+00:00",
            )
            enriched = service.apply_enrichment(
                "char1",
                bucket_id,
                valence=0.22,
                arousal=0.84,
                tags=["担心"],
                name="担心",
                enrichment_status="complete",
                enrichment_error=None,
            )

            self.assertEqual(enriched["valence"], 0.22)
            self.assertEqual(enriched["arousal"], 0.84)
            self.assertEqual(enriched["enrichment_status"], "complete")
            self.assertEqual(enriched["last_active"], "2026-07-16T01:00:00+00:00")

    def test_app_write_pipeline_replaces_defaults_and_finishes_embedding(self):
        import app as app_module

        with tempfile.TemporaryDirectory() as temp_dir:
            service = EmbeddedMemoryService(temp_dir, ["char1"])
            embeddings = _Embeddings()
            with patch.object(app_module, "MEMORY_SERVICE", service), patch.object(
                app_module, "MEMORY_ANALYZER", _Analyzer()
            ), patch.object(
                app_module, "MEMORY_EMBEDDINGS", embeddings
            ), patch.object(
                app_module, "_MEMORY_ENRICHMENT_EXECUTOR", _ImmediateExecutor()
            ):
                app_module.push_summary_to_ombre(
                    "User受惊后被认真安抚下来。", "char1"
                )

            memory = service.list_memories("char1")[0]
            self.assertEqual(memory["valence"], 0.12)
            self.assertEqual(memory["arousal"], 0.88)
            self.assertEqual(memory["enrichment_status"], "complete")
            self.assertEqual(memory["embedding_status"], "complete")
            self.assertEqual(memory["tags"], ["害怕", "安抚"])
            self.assertEqual(len(embeddings.saved), 1)


if __name__ == "__main__":
    unittest.main()
