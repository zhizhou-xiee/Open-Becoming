"""Pluggable long-term memory backends for Open-Becoming.

The bundled Markdown-backed store remains the default. Operators can disable
long-term memory or provide a Python factory without changing the chat engine.
"""

from __future__ import annotations

import importlib
from typing import Iterable

from memory_core import EmbeddedMemoryService


class MemoryBackendError(RuntimeError):
    """Raised when a configured memory backend cannot be loaded."""


class MemoryCapabilityError(MemoryBackendError):
    """Raised when a backend does not implement an optional capability."""


_CAPABILITY_METHODS = {
    "read": ("recall",),
    "write": ("save",),
    "admin": ("list_memories", "get_memory", "update_memory", "delete_memory"),
    "enrichment": ("get_memory", "apply_enrichment", "list_needing_enrichment"),
    "decay": ("run_decay_cycle",),
    "legacy_import": ("import_legacy",),
}


class MemoryBackend:
    """Small compatibility facade around bundled or third-party memory stores."""

    def __init__(self, backend=None, *, name: str = "disabled"):
        self.backend = backend
        self.name = name
        self.enabled = backend is not None
        self.capabilities = tuple(
            capability
            for capability, methods in _CAPABILITY_METHODS.items()
            if backend is not None and all(callable(getattr(backend, method, None)) for method in methods)
        )

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def describe(self) -> dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "capabilities": list(self.capabilities),
        }

    def _method(self, method: str, capability: str):
        if not self.supports(capability):
            raise MemoryCapabilityError(
                f"memory backend '{self.name}' does not support {capability}"
            )
        return getattr(self.backend, method)

    def recall(self, owner_id: str) -> str:
        if not self.enabled:
            return ""
        value = self._method("recall", "read")(owner_id)
        return str(value or "")

    def save(self, content: str, owner_id: str, **metadata) -> tuple[str, bool]:
        result = self._method("save", "write")(
            content, owner_id, **metadata
        )
        if isinstance(result, tuple) and len(result) == 2:
            return str(result[0] or ""), bool(result[1])
        if isinstance(result, dict):
            return str(result.get("id") or result.get("memory_id") or ""), bool(
                result.get("created", True)
            )
        if isinstance(result, str):
            return result, True
        return "", True

    def list_memories(self, *args, **kwargs):
        return self._method("list_memories", "admin")(*args, **kwargs)

    def get_memory(self, *args, **kwargs):
        capability = "enrichment" if self.supports("enrichment") else "admin"
        return self._method("get_memory", capability)(*args, **kwargs)

    def update_memory(self, *args, **kwargs):
        return self._method("update_memory", "admin")(*args, **kwargs)

    def delete_memory(self, *args, **kwargs):
        return self._method("delete_memory", "admin")(*args, **kwargs)

    def apply_enrichment(self, *args, **kwargs):
        return self._method("apply_enrichment", "enrichment")(*args, **kwargs)

    def list_needing_enrichment(self, *args, **kwargs):
        return self._method("list_needing_enrichment", "enrichment")(*args, **kwargs)

    def run_decay_cycle(self, *args, **kwargs):
        return self._method("run_decay_cycle", "decay")(*args, **kwargs)

    def import_legacy(self, *args, **kwargs):
        return self._method("import_legacy", "legacy_import")(*args, **kwargs)


def load_memory_backend(
    spec: str,
    *,
    memory_dir: str,
    owner_ids: Iterable[str],
) -> MemoryBackend:
    """Load the built-in, disabled, or ``module:factory`` memory backend."""

    normalized = (spec or "embedded").strip()
    lowered = normalized.casefold()
    if lowered in {"embedded", "ombre", "builtin"}:
        return MemoryBackend(
            EmbeddedMemoryService(memory_dir, owner_ids), name="embedded"
        )
    if lowered in {"none", "disabled", "off"}:
        return MemoryBackend(name="disabled")
    if ":" not in normalized:
        raise MemoryBackendError(
            "MEMORY_BACKEND must be embedded, disabled, or module.path:factory"
        )

    module_name, factory_name = normalized.rsplit(":", 1)
    if not module_name or not factory_name:
        raise MemoryBackendError("custom memory backend must use module.path:factory")
    try:
        module = importlib.import_module(module_name)
        factory = getattr(module, factory_name)
        backend = factory(memory_dir=memory_dir, owner_ids=tuple(owner_ids))
    except Exception as exc:
        raise MemoryBackendError(
            f"cannot load memory backend '{normalized}': {exc}"
        ) from exc
    if backend is None:
        raise MemoryBackendError(f"memory backend factory '{normalized}' returned None")
    facade = MemoryBackend(backend, name=f"external:{normalized}")
    if not facade.supports("read") or not facade.supports("write"):
        raise MemoryBackendError(
            "custom memory backend must implement recall(owner_id) and "
            "save(content, owner_id, **metadata)"
        )
    return facade
