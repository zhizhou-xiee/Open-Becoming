import os
import tempfile
import unittest
from unittest.mock import patch

from memory_core import EmbeddedMemoryService


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _LegacySession:
    def post(self, url, **_kwargs):
        return _Response(200, {"ok": True})

    def get(self, url, **_kwargs):
        if url.endswith("/api/buckets"):
            return _Response(200, [
                {"id": "owned", "domain": ["char1"]},
                {"id": "chat", "domain": ["claude-chat"]},
                {"id": "ambiguous", "domain": ["char1", "char2"]},
            ])
        if url.endswith("/api/bucket/owned"):
            return _Response(200, {
                "content": "从旧往生道带回来的记忆",
                "metadata": {
                    "name": "旧记忆",
                    "domain": ["char1"],
                    "importance": 7,
                    "valence": 0.2,
                    "arousal": 0.8,
                    "model_valence": 0.35,
                    "activation_count": 12.5,
                    "resolved": True,
                    "digested": True,
                    "created": "2026-01-02T03:04:05",
                    "last_active": "2026-02-03T04:05:06",
                },
            })
        return _Response(404, {})


class EmbeddedMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = EmbeddedMemoryService(
            self.temp_dir.name, ["char1", "char2"]
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_character_memories_are_strictly_isolated(self):
        self.service.save("Char 1的记忆", "char1", source="self_saved")
        self.service.save("Char 2的记忆", "char2", source="self_saved")

        jiang = self.service.list_memories("char1")
        ping = self.service.list_memories("char2")

        self.assertEqual([item["content"] for item in jiang], ["Char 1的记忆"])
        self.assertEqual([item["content"] for item in ping], ["Char 2的记忆"])
        self.assertNotIn("Char 2", self.service.recall("char1"))
        self.assertTrue(os.path.isdir(os.path.join(self.temp_dir.name, "dynamic")))

    def test_source_key_updates_instead_of_copying_summary(self):
        first_id, first_created = self.service.save(
            "第一版摘要",
            "char1",
            source="conversation_summary",
            source_key="summary:default",
        )
        second_id, second_created = self.service.save(
            "第二版摘要",
            "char1",
            source="conversation_summary",
            source_key="summary:default",
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first_id, second_id)
        memories = self.service.list_memories("char1")
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["content"], "第二版摘要")

    def test_pinned_memory_returns_to_dynamic_storage_when_unpinned(self):
        memory_id, _ = self.service.save(
            "先固定，再放回普通记忆",
            "char1",
            source="self_saved",
            pinned=True,
        )
        pinned = self.service.get_memory("char1", memory_id)
        self.assertTrue(pinned["pinned"])
        self.assertEqual(pinned["type"], "permanent")

        unpinned = self.service.update_memory(
            "char1", memory_id, pinned=False
        )
        self.assertFalse(unpinned["pinned"])
        self.assertEqual(unpinned["type"], "dynamic")
        dynamic_files = []
        for root, _dirs, files in os.walk(os.path.join(self.temp_dir.name, "dynamic")):
            dynamic_files.extend(files)
        self.assertTrue(any(memory_id in filename for filename in dynamic_files))

    def test_legacy_import_only_accepts_one_exact_character_owner(self):
        with patch("memory_core.service.requests.Session", return_value=_LegacySession()):
            first = self.service.import_legacy("https://old.example.com/dashboard", "secret")
            imported_id = self.service.list_memories("char1")[0]["id"]
            self.service._run(self.service.manager.update(
                imported_id,
                activation_count=0,
                last_active="2026-07-01T00:00:00",
                _refresh_last_active=False,
            ))
            second = self.service.import_legacy("https://old.example.com", "secret")

        self.assertEqual(first["eligible"], 1)
        self.assertEqual(first["imported"], 1)
        self.assertEqual(second["imported"], 0)
        self.assertEqual(second["skipped"], 1)
        memories = self.service.list_memories("char1")
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["source"], "legacy_ombre")
        self.assertEqual(memories[0]["valence"], 0.2)
        self.assertEqual(memories[0]["arousal"], 0.8)
        self.assertEqual(memories[0]["model_valence"], 0.35)
        self.assertEqual(memories[0]["activation_count"], 12.5)
        self.assertTrue(memories[0]["resolved"])
        self.assertTrue(memories[0]["digested"])
        self.assertEqual(memories[0]["created"], "2026-01-02T03:04:05")
        self.assertEqual(memories[0]["last_active"], "2026-02-03T04:05:06")
        self.assertEqual(self.service.list_memories("char2"), [])


if __name__ == "__main__":
    unittest.main()
