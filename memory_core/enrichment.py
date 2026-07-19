"""LLM metadata analysis and Gemini embeddings for Becoming memories."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone

import requests


ANALYZE_PROMPT = """你是一个长期记忆分析器。请分析文本并只输出 JSON。

规则：
1. valence: 0.0~1.0，0=极度消极，0.5=中性，1=极度积极。
2. arousal: 0.0~1.0，0=非常平静，0.5=普通，1=非常激动。
3. tags: 提取并扩展 6~12 个便于日后检索的关键词，不要使用 [[]]。
4. suggested_name: 10 个汉字以内的记忆标题。
5. importance: 1~10，根据这件事对长期关系、承诺、安全、健康和重要决定的影响判断。
6. 不得把 valence/arousal 固定写成示例值，必须依据这段文本独立判断。

输出格式：
{
  "valence": 0.7,
  "arousal": 0.4,
  "tags": ["关键词1", "关键词2"],
  "suggested_name": "简短标题",
  "importance": 5
}
"""


class MemoryEnrichmentError(RuntimeError):
    """Raised when an enrichment provider cannot produce valid metadata."""


def _clamp(value, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _json_object(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise MemoryEnrichmentError("打标模型没有返回 JSON")
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise MemoryEnrichmentError("打标模型返回了无效 JSON") from exc
    if not isinstance(payload, dict):
        raise MemoryEnrichmentError("打标结果不是对象")
    return payload


class MemoryMetadataAnalyzer:
    """OpenAI-compatible metadata analyzer, matching Ombre's DeepSeek path."""

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://api.deepseek.com/v1",
        model: str = "deepseek-chat",
        timeout: float = 60.0,
    ):
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or "").strip().rstrip("/")
        self.model = (model or "deepseek-chat").strip()
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "MemoryMetadataAnalyzer":
        direct_key = (
            os.environ.get("OMBRE_DEHYDRATION_API_KEY")
            or os.environ.get("OMBRE_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY")
            or ""
        )
        openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
        use_openrouter = not direct_key and bool(openrouter_key)
        return cls(
            api_key=direct_key or openrouter_key,
            base_url=os.environ.get(
                "OMBRE_DEHYDRATION_BASE_URL",
                "https://openrouter.ai/api/v1"
                if use_openrouter else "https://api.deepseek.com/v1",
            ),
            model=os.environ.get(
                "OMBRE_DEHYDRATION_MODEL",
                "deepseek/deepseek-chat" if use_openrouter else "deepseek-chat",
            ),
            timeout=float(os.environ.get("OMBRE_ENRICHMENT_TIMEOUT", "60")),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def analyze(self, content: str) -> dict:
        if not self.enabled:
            raise MemoryEnrichmentError("未配置记忆打标 API")
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": ANALYZE_PROMPT},
                    {"role": "user", "content": (content or "")[:4000]},
                ],
                "max_tokens": 512,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
            },
            timeout=self.timeout,
        )
        try:
            response.raise_for_status()
            raw = response.json()["choices"][0]["message"]["content"]
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
            raise MemoryEnrichmentError(f"打标 API 调用失败: {exc}") from exc

        payload = _json_object(raw)
        try:
            valence = round(_clamp(payload["valence"], 0.0, 1.0), 3)
            arousal = round(_clamp(payload["arousal"], 0.0, 1.0), 3)
        except (KeyError, TypeError, ValueError) as exc:
            raise MemoryEnrichmentError("打标结果缺少有效的 A/V") from exc

        raw_tags = payload.get("tags", [])
        if not isinstance(raw_tags, list):
            raw_tags = []
        tags = []
        for item in raw_tags:
            tag = str(item).strip().replace("[[", "").replace("]]", "")
            if tag and tag not in tags:
                tags.append(tag[:30])
        name = str(payload.get("suggested_name", "")).strip()[:20]
        try:
            importance = int(round(_clamp(payload.get("importance", 5), 1, 10)))
        except (TypeError, ValueError):
            importance = 5
        return {
            "valence": valence,
            "arousal": arousal,
            "tags": tags[:15],
            "name": name,
            "importance": importance,
        }


class GeminiEmbeddingStore:
    """Small SQLite-backed embedding store compatible with Ombre's Gemini path."""

    def __init__(
        self,
        db_path: str,
        *,
        api_key: str = "",
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        model: str = "gemini-embedding-2",
        timeout: float = 45.0,
    ):
        self.db_path = db_path
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or "").strip().rstrip("/")
        self.model = (model or "gemini-embedding-2").strip()
        self.timeout = timeout
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._init_db()

    @classmethod
    def from_env(cls, memory_dir: str) -> "GeminiEmbeddingStore":
        return cls(
            os.path.join(memory_dir, "embeddings.db"),
            api_key=(
                os.environ.get("OMBRE_EMBEDDING_API_KEY")
                or os.environ.get("GEMINI_API_KEY")
                or os.environ.get("MBRE_API_KEY")
                or ""
            ),
            base_url=os.environ.get(
                "OMBRE_EMBEDDING_BASE_URL",
                "https://generativelanguage.googleapis.com/v1beta",
            ),
            model=os.environ.get("OMBRE_EMBEDDING_MODEL", "gemini-embedding-2"),
            timeout=float(os.environ.get("OMBRE_EMBEDDING_TIMEOUT", "45")),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS embeddings ("
                "bucket_id TEXT PRIMARY KEY, embedding TEXT NOT NULL, "
                "model TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(embeddings)").fetchall()
            }
            if "model" not in columns:
                conn.execute("ALTER TABLE embeddings ADD COLUMN model TEXT NOT NULL DEFAULT ''")

    def generate_and_store(self, bucket_id: str, content: str) -> bool:
        if not self.enabled or not (content or "").strip():
            return False
        if self.base_url.endswith("/openai"):
            response = requests.post(
                f"{self.base_url}/embeddings",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": self.model, "input": content[:6000]},
                timeout=self.timeout,
            )
            vector_path = ("data", 0, "embedding")
        else:
            response = requests.post(
                f"{self.base_url}/models/{self.model}:embedContent",
                headers={
                    "x-goog-api-key": self.api_key,
                    "Content-Type": "application/json",
                },
                json={
                    "content": {"parts": [{"text": content[:6000]}]},
                    "outputDimensionality": 768,
                },
                timeout=self.timeout,
            )
            vector_path = ("embedding", "values")
        try:
            response.raise_for_status()
            vector = response.json()
            for key in vector_path:
                vector = vector[key]
        except (
            requests.RequestException, KeyError, IndexError, TypeError, ValueError
        ) as exc:
            raise MemoryEnrichmentError(f"Gemini embedding 失败: {exc}") from exc
        if not isinstance(vector, list) or not vector:
            raise MemoryEnrichmentError("Gemini embedding 返回空向量")
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO embeddings "
                "(bucket_id, embedding, model, updated_at) VALUES (?, ?, ?, ?)",
                (bucket_id, json.dumps(vector), self.model, now),
            )
        return True

    def delete(self, bucket_id: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM embeddings WHERE bucket_id = ?", (bucket_id,))
