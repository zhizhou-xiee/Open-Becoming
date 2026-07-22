"""Becoming adapter around the embedded Ombre Brain memory primitives."""

from __future__ import annotations

import asyncio
import os
import threading
from datetime import datetime
from typing import Iterable

import requests

from .ombre import BucketManager, DecayEngine
from .ombre.utils import strip_wikilinks


class LegacyImportError(RuntimeError):
    """Raised when a legacy Ombre dashboard cannot be imported."""


class EmbeddedMemoryService:
    """Character-isolated, Markdown-backed memory service.

    Ombre's domain remains available for compatibility, while owner_id is the
    hard Becoming partition. New records always carry both values.
    """

    def __init__(self, base_dir: str, owner_ids: Iterable[str]):
        self.base_dir = os.path.abspath(base_dir)
        self.owner_ids = frozenset(owner_ids)
        self._lock = threading.RLock()
        for folder in ("permanent", "dynamic", "archive", "feel"):
            os.makedirs(os.path.join(self.base_dir, folder), exist_ok=True)

        self.config = {
            "buckets_dir": self.base_dir,
            "matching": {"fuzzy_threshold": 50, "max_results": 5},
            "scoring_weights": {
                "topic_relevance": 4.0,
                "emotion_resonance": 2.0,
                "time_proximity": 1.5,
                "importance": 1.0,
            },
            "decay": {
                "lambda": 0.05,
                "threshold": 0.3,
                "check_interval_hours": 24,
                "emotion_weights": {"base": 1.0, "arousal_boost": 0.8},
            },
        }
        self.manager = BucketManager(self.config)
        self.decay = DecayEngine(self.config, self.manager)

    @staticmethod
    def _run(coro):
        return asyncio.run(coro)

    def _require_owner(self, owner_id: str) -> None:
        if owner_id not in self.owner_ids:
            raise ValueError("unknown memory owner")

    @staticmethod
    def _belongs(bucket: dict, owner_id: str) -> bool:
        meta = bucket.get("metadata", {})
        explicit_owner = meta.get("owner_id")
        if explicit_owner:
            return explicit_owner == owner_id
        # Legacy Ombre files did not have owner_id. Exact character domains are
        # accepted during migration, never broad or overlapping domain matches.
        return owner_id in meta.get("domain", [])

    def _all_for_owner(self, owner_id: str, include_archive: bool = True) -> list[dict]:
        self._require_owner(owner_id)
        with self._lock:
            buckets = self._run(self.manager.list_all(include_archive=include_archive))
        return [bucket for bucket in buckets if self._belongs(bucket, owner_id)]

    def save(
        self,
        content: str,
        owner_id: str,
        *,
        source: str = "becoming",
        source_key: str | None = None,
        tags: list[str] | None = None,
        importance: int = 5,
        valence: float = 0.5,
        arousal: float = 0.3,
        name: str | None = None,
        pinned: bool = False,
        protected: bool = False,
        resolved: bool = False,
        digested: bool = False,
        activation_count: float = 0,
        model_valence: float | None = None,
        bucket_type: str = "dynamic",
        legacy_id: str | None = None,
        created: str | None = None,
        last_active: str | None = None,
        enrichment_status: str | None = None,
        enrichment_error: str | None = None,
        enriched_at: str | None = None,
        embedding_status: str | None = None,
        embedding_error: str | None = None,
    ) -> tuple[str, bool]:
        self._require_owner(owner_id)
        clean_content = (content or "").strip()
        if not clean_content:
            raise ValueError("memory content is empty")

        with self._lock:
            existing = self._run(self.manager.list_all(include_archive=True))
            if source_key:
                match = next(
                    (
                        bucket for bucket in existing
                        if self._belongs(bucket, owner_id)
                        and bucket.get("metadata", {}).get("source_key") == source_key
                    ),
                    None,
                )
                if match:
                    if source == "legacy_ombre":
                        self._run(self.manager.update(
                            match["id"],
                            content=clean_content,
                            tags=tags or [],
                            importance=importance,
                            valence=valence,
                            arousal=arousal,
                            name=name or self._title(clean_content),
                            pinned=pinned,
                            protected=protected,
                            resolved=resolved,
                            digested=digested,
                            activation_count=activation_count,
                            model_valence=model_valence,
                            created=created,
                            last_active=last_active,
                            _refresh_last_active=False,
                        ))
                        return match["id"], False
                    if (match.get("content") or "").strip() == clean_content:
                        return match["id"], False
                    self._run(self.manager.update(
                        match["id"],
                        content=clean_content,
                        enrichment_status=enrichment_status,
                        enrichment_error=enrichment_error,
                        enriched_at=enriched_at,
                        embedding_status=embedding_status,
                        embedding_error=embedding_error,
                    ))
                    return match["id"], False

            bucket_id = self._run(self.manager.create(
                content=clean_content,
                tags=tags or [],
                importance=importance,
                domain=[owner_id],
                valence=valence,
                arousal=arousal,
                bucket_type=bucket_type,
                name=name or self._title(clean_content),
                pinned=pinned,
                protected=protected,
                resolved=resolved,
                digested=digested,
                activation_count=activation_count,
                model_valence=model_valence,
                owner_id=owner_id,
                source=source,
                source_key=source_key,
                legacy_id=legacy_id,
                created=created,
                last_active=last_active,
                enrichment_status=enrichment_status,
                enrichment_error=enrichment_error,
                enriched_at=enriched_at,
                embedding_status=embedding_status,
                embedding_error=embedding_error,
            ))
            if bucket_type == "archived":
                self._run(self.manager.archive(bucket_id))
            return bucket_id, True

    @staticmethod
    def _title(content: str) -> str:
        first_line = next((line.strip() for line in content.splitlines() if line.strip()), "记忆")
        return first_line[:28]

    def recall(self, owner_id: str, max_results: int = 5, max_chars: int = 4200) -> str:
        buckets = self._all_for_owner(owner_id, include_archive=False)
        visible = [
            bucket for bucket in buckets
            if not bucket.get("metadata", {}).get("digested", False)
        ]
        for bucket in visible:
            bucket["memory_score"] = self.decay.calculate_score(bucket.get("metadata", {}))
        visible.sort(key=lambda bucket: (
            bool(bucket.get("metadata", {}).get("pinned")),
            bucket["memory_score"],
            bucket.get("metadata", {}).get("created", ""),
        ), reverse=True)

        parts = []
        length = 0
        for bucket in visible[:max_results]:
            text = strip_wikilinks(bucket.get("content", "")).strip()
            if not text:
                continue
            created = str(bucket.get("metadata", {}).get("created", ""))[:10]
            part = f"[{created}] {text}" if created else text
            if parts and length + len(part) > max_chars:
                break
            parts.append(part)
            length += len(part)
        return "\n---\n".join(parts)

    def list_memories(
        self,
        owner_id: str,
        *,
        query: str = "",
        include_archive: bool = True,
        limit: int = 100,
    ) -> list[dict]:
        buckets = self._all_for_owner(owner_id, include_archive=include_archive)
        needle = query.strip().casefold()
        if needle:
            buckets = [
                bucket for bucket in buckets
                if needle in strip_wikilinks(bucket.get("content", "")).casefold()
                or needle in str(bucket.get("metadata", {}).get("name", "")).casefold()
                or any(needle in str(tag).casefold() for tag in bucket.get("metadata", {}).get("tags", []))
            ]
        buckets.sort(
            key=lambda bucket: bucket.get("metadata", {}).get("created", ""),
            reverse=True,
        )
        return [self._serialize(bucket) for bucket in buckets[:max(1, min(limit, 500))]]

    def get_memory(self, owner_id: str, bucket_id: str) -> dict | None:
        self._require_owner(owner_id)
        with self._lock:
            bucket = self._run(self.manager.get(bucket_id))
        if not bucket or not self._belongs(bucket, owner_id):
            return None
        return self._serialize(bucket)

    def update_memory(self, owner_id: str, bucket_id: str, **updates) -> dict | None:
        current = self.get_memory(owner_id, bucket_id)
        if not current:
            return None
        allowed = {
            "content", "importance", "resolved", "pinned", "tags", "name",
            "valence", "arousal", "enrichment_status", "enrichment_error",
            "enriched_at", "embedding_status", "embedding_error",
        }
        clean = {key: value for key, value in updates.items() if key in allowed}
        if not clean:
            return current
        with self._lock:
            self._run(self.manager.update(bucket_id, **clean))
        return self.get_memory(owner_id, bucket_id)

    def apply_enrichment(
        self,
        owner_id: str,
        bucket_id: str,
        **updates,
    ) -> dict | None:
        """Update generated metadata without making the memory look newly activated."""
        if not self.get_memory(owner_id, bucket_id):
            return None
        allowed = {
            "importance", "tags", "name", "valence", "arousal",
            "enrichment_status", "enrichment_error", "enriched_at",
            "embedding_status", "embedding_error",
        }
        clean = {key: value for key, value in updates.items() if key in allowed}
        if not clean:
            return self.get_memory(owner_id, bucket_id)
        with self._lock:
            self._run(self.manager.update(
                bucket_id, _refresh_last_active=False, **clean
            ))
        return self.get_memory(owner_id, bucket_id)

    def list_needing_enrichment(
        self,
        owner_id: str,
        *,
        limit: int = 50,
    ) -> list[dict]:
        sources = {
            "self_saved", "group_self_saved", "conversation_summary",
            "group_summary", "moment", "moment_comment",
        }
        memories = self.list_memories(owner_id, include_archive=True, limit=500)
        return [
            memory for memory in memories
            if memory.get("source") in sources
            and (
                memory.get("enrichment_status") != "complete"
                or memory.get("embedding_status") != "complete"
            )
        ][:max(1, min(limit, 100))]

    def delete_memory(self, owner_id: str, bucket_id: str) -> bool:
        if not self.get_memory(owner_id, bucket_id):
            return False
        with self._lock:
            return bool(self._run(self.manager.delete(bucket_id)))

    def run_decay_cycle(self) -> dict:
        with self._lock:
            return self._run(self.decay.run_decay_cycle())

    def import_legacy(self, base_url: str, password: str) -> dict:
        base_url = (base_url or "").strip().rstrip("/")
        if base_url.lower().endswith("/dashboard"):
            base_url = base_url[:-len("/dashboard")].rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise LegacyImportError("旧 Ombre 地址无效")
        if not password:
            raise LegacyImportError("请填写旧 Ombre 的 Dashboard 密码")

        client = requests.Session()
        try:
            login = client.post(
                f"{base_url}/auth/login", json={"password": password}, timeout=15
            )
            if login.status_code != 200:
                raise LegacyImportError("旧 Ombre 登录失败")
            response = client.get(f"{base_url}/api/buckets", timeout=30)
            response.raise_for_status()
            summaries = response.json()
        except LegacyImportError:
            raise
        except Exception as exc:
            raise LegacyImportError(f"无法读取旧 Ombre：{exc}") from exc

        selected = []
        for item in summaries if isinstance(summaries, list) else []:
            domains = item.get("domain", [])
            owners = [owner for owner in self.owner_ids if owner in domains]
            if len(owners) == 1:
                selected.append((owners[0], item))

        imported = 0
        skipped = 0
        errors = 0
        for owner_id, item in selected:
            legacy_id = str(item.get("id", "")).strip()
            if not legacy_id:
                skipped += 1
                continue
            try:
                detail = client.get(f"{base_url}/api/bucket/{legacy_id}", timeout=20)
                detail.raise_for_status()
                payload = detail.json()
                meta = payload.get("metadata", {})
                _, created_new = self.save(
                    payload.get("content", ""),
                    owner_id,
                    source="legacy_ombre",
                    source_key=f"legacy:{legacy_id}",
                    tags=meta.get("tags", []),
                    importance=meta.get("importance", 5),
                    valence=meta.get("valence", 0.5),
                    arousal=meta.get("arousal", 0.3),
                    name=meta.get("name"),
                    pinned=bool(meta.get("pinned", False)),
                    protected=bool(meta.get("protected", False)),
                    resolved=bool(meta.get("resolved", False)),
                    digested=bool(meta.get("digested", False)),
                    activation_count=meta.get("activation_count", 0),
                    model_valence=meta.get("model_valence"),
                    bucket_type=meta.get("type", "dynamic"),
                    legacy_id=legacy_id,
                    created=meta.get("created"),
                    last_active=meta.get("last_active"),
                )
                if created_new:
                    imported += 1
                else:
                    skipped += 1
            except Exception:
                errors += 1

        return {
            "eligible": len(selected),
            "imported": imported,
            "skipped": skipped,
            "errors": errors,
        }

    def _serialize(self, bucket: dict) -> dict:
        meta = bucket.get("metadata", {})
        content = strip_wikilinks(bucket.get("content", "")).strip()
        return {
            "id": bucket.get("id"),
            "name": meta.get("name", bucket.get("id")),
            "content": content,
            "preview": content[:160],
            "owner_id": meta.get("owner_id"),
            "source": meta.get("source", "legacy"),
            "source_key": meta.get("source_key"),
            "tags": meta.get("tags", []),
            "importance": meta.get("importance", 5),
            "valence": meta.get("valence", 0.5),
            "arousal": meta.get("arousal", 0.3),
            "type": meta.get("type", "dynamic"),
            "pinned": bool(meta.get("pinned", False)),
            "resolved": bool(meta.get("resolved", False)),
            "protected": bool(meta.get("protected", False)),
            "digested": bool(meta.get("digested", False)),
            "activation_count": meta.get("activation_count", 0),
            "model_valence": meta.get("model_valence"),
            "enrichment_status": meta.get(
                "enrichment_status",
                "complete" if meta.get("source") == "legacy_ombre" else "pending",
            ),
            "enrichment_error": meta.get("enrichment_error"),
            "enriched_at": meta.get("enriched_at"),
            "embedding_status": meta.get(
                "embedding_status",
                "complete" if meta.get("source") == "legacy_ombre" else "pending",
            ),
            "embedding_error": meta.get("embedding_error"),
            "created": meta.get("created", ""),
            "last_active": meta.get("last_active", ""),
            "score": self.decay.calculate_score(meta),
        }
