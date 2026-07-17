import sys
import tempfile
import types
import unittest

from memory_backend import MemoryBackendError, load_memory_backend


class _MinimalExternalMemory:
    def __init__(self, memory_dir, owner_ids):
        self.memory_dir = memory_dir
        self.owner_ids = owner_ids
        self.saved = []

    def recall(self, owner_id):
        return f"memory for {owner_id}"

    def save(self, content, owner_id, **metadata):
        self.saved.append((content, owner_id, metadata))
        return {"id": "external-1", "created": True}


class MemoryBackendTests(unittest.TestCase):
    def test_embedded_backend_keeps_full_feature_set(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backend = load_memory_backend(
                "embedded", memory_dir=temp_dir, owner_ids=("char1",)
            )
            self.assertTrue(backend.enabled)
            for capability in (
                "read", "write", "admin", "enrichment", "decay", "legacy_import"
            ):
                self.assertTrue(backend.supports(capability))
            memory_id, created = backend.save("记住这件事", "char1")
            self.assertTrue(created)
            self.assertTrue(memory_id)
            self.assertIn("记住这件事", backend.recall("char1"))

    def test_disabled_backend_is_an_explicit_noop_for_recall(self):
        backend = load_memory_backend(
            "disabled", memory_dir="unused", owner_ids=("char1",)
        )
        self.assertFalse(backend.enabled)
        self.assertEqual(backend.recall("char1"), "")
        self.assertEqual(backend.capabilities, ())

    def test_custom_backend_only_needs_read_and_write(self):
        module_name = "test_external_memory_adapter"
        module = types.ModuleType(module_name)
        module.create_backend = lambda **kwargs: _MinimalExternalMemory(**kwargs)
        sys.modules[module_name] = module
        try:
            backend = load_memory_backend(
                f"{module_name}:create_backend",
                memory_dir="/tmp/custom-memory",
                owner_ids=("char1", "char2"),
            )
        finally:
            sys.modules.pop(module_name, None)

        self.assertEqual(backend.recall("char2"), "memory for char2")
        self.assertTrue(backend.supports("write"))
        self.assertFalse(backend.supports("admin"))
        memory_id, created = backend.save(
            "外置记忆", "char1", source="self_saved", source_key="one"
        )
        self.assertEqual((memory_id, created), ("external-1", True))

    def test_custom_backend_rejects_incomplete_contract(self):
        module_name = "test_incomplete_memory_adapter"
        module = types.ModuleType(module_name)
        module.create_backend = lambda **_kwargs: object()
        sys.modules[module_name] = module
        try:
            with self.assertRaises(MemoryBackendError):
                load_memory_backend(
                    f"{module_name}:create_backend",
                    memory_dir="unused",
                    owner_ids=("char1",),
                )
        finally:
            sys.modules.pop(module_name, None)


if __name__ == "__main__":
    unittest.main()
