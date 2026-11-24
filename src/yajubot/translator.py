"""完全無料 API を組み合わせた多段階逆翻訳（ガバガバ翻訳）モジュール。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any, Protocol

import aiohttp

logger = logging.getLogger(__name__)

# 逆翻訳の経由言語ルート（先頭から末尾へ順に翻訳する）
# のちにガバガバ具合や速度を見ながら言語コードを追加・変更して調整可能。
# 例:
#   ("ja", "en", "ja")                -> 軽め・高速
#   ("ja", "th", "ru", "ja")          -> 3ステップ・語順/文字体系差で崩れやすい
#   ("ja", "zu", "fi", "mn", "ja")    -> 4ステップ・さらに崩壊（マイナー言語含む）
DEFAULT_TRANSLATION_ROUTE: tuple[str, ...] = ("ja", "th", "ru", "ja")

DEFAULT_TIMEOUT_SECONDS: float = 10.0
MYMEMORY_MAX_QUERY_CHARS: int = 500


class TranslationError(RuntimeError):
    """翻訳処理の失敗を表す例外。"""


class TranslatorBackend(Protocol):
    """翻訳バックエンドの共通インターフェース。"""

    async def translate(self, text: str, source: str, target: str) -> str:
        """`source` 言語の `text` を `target` 言語へ翻訳して返す。"""


class MyMemoryTranslator:
    """MyMemory 公式無料 REST API（APIキー不要・課金なし）を用いる翻訳バックエンド。"""

    ENDPOINT = "https://api.mymemory.translated.net/get"

    def __init__(
        self,
        session_provider: Callable[[], aiohttp.ClientSession],
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._session_provider = session_provider
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def translate(self, text: str, source: str, target: str) -> str:
        if len(text) > MYMEMORY_MAX_QUERY_CHARS:
            raise TranslationError(
                f"MyMemory の1回あたりの文字数上限（{MYMEMORY_MAX_QUERY_CHARS}文字）を超えています。"
            )

        session = self._session_provider()
        params = {
            "q": text,
            "langpair": f"{source}|{target}",
        }
        try:
            async with session.get(
                self.ENDPOINT, params=params, timeout=self._timeout
            ) as resp:
                if resp.status != 200:
                    raise TranslationError(
                        f"MyMemory API が HTTP {resp.status} を返しました。"
                    )
                payload: dict[str, Any] = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise TranslationError(f"MyMemory API 通信エラー: {exc}") from exc
        except TimeoutError as exc:
            raise TranslationError("MyMemory API がタイムアウトしました。") from exc
        except ValueError as exc:
            raise TranslationError(f"MyMemory API 応答の JSON 解析に失敗しました: {exc}") from exc

        if payload.get("quotaFinished"):
            raise TranslationError("MyMemory API の無料枠上限に達しました。")

        status = payload.get("responseStatus")
        if status != 200:
            details = payload.get("responseDetails", "")
            raise TranslationError(
                f"MyMemory API エラー (status={status}): {details}"
            )

        response_data = payload.get("responseData")
        if not isinstance(response_data, dict):
            raise TranslationError("MyMemory API 応答に responseData がありません。")

        translated = response_data.get("translatedText")
        if not isinstance(translated, str) or not translated.strip():
            raise TranslationError("MyMemory API から空の翻訳結果が返されました。")

        upper_text = translated.strip().upper()
        if upper_text.startswith("MYMEMORY WARNING:") or upper_text.startswith(
            "QUERY LENGTH LIMIT EXCEEDED"
        ):
            raise TranslationError(f"MyMemory API 制限応答: {translated.strip()}")

        return translated.strip()


class GoogleUnofficialTranslator:
    """Google 翻訳の無料非公式エンドポイント（APIキー不要・課金なし）を用いるフォールバック用バックエンド。"""

    ENDPOINT = "https://translate.googleapis.com/translate_a/single"

    def __init__(
        self,
        session_provider: Callable[[], aiohttp.ClientSession],
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._session_provider = session_provider
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def translate(self, text: str, source: str, target: str) -> str:
        session = self._session_provider()
        params = {
            "client": "gtx",
            "sl": source,
            "tl": target,
            "dt": "t",
            "q": text,
        }
        try:
            async with session.get(
                self.ENDPOINT, params=params, timeout=self._timeout
            ) as resp:
                if resp.status != 200:
                    raise TranslationError(
                        f"Google 非公式翻訳が HTTP {resp.status} を返しました。"
                    )
                payload: Any = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise TranslationError(f"Google 非公式翻訳 通信エラー: {exc}") from exc
        except TimeoutError as exc:
            raise TranslationError("Google 非公式翻訳がタイムアウトしました。") from exc
        except ValueError as exc:
            raise TranslationError(
                f"Google 非公式翻訳 応答の JSON 解析に失敗しました: {exc}"
            ) from exc

        if not isinstance(payload, list) or not payload or not isinstance(payload[0], list):
            raise TranslationError("Google 非公式翻訳の応答形式が不正です。")

        parts: list[str] = []
        for segment in payload[0]:
            if isinstance(segment, list) and segment and isinstance(segment[0], str):
                parts.append(segment[0])

        result = "".join(parts).strip()
        if not result:
            raise TranslationError("Google 非公式翻訳から空の翻訳結果が返されました。")

        return result


class FallbackTranslator:
    """複数の翻訳バックエンドを順番に試行し、成功した結果を返すラッパー。"""

    def __init__(self, backends: Sequence[TranslatorBackend]) -> None:
        if not backends:
            raise ValueError("backends には1つ以上の翻訳バックエンドを指定してください。")
        self._backends = tuple(backends)

    async def translate(self, text: str, source: str, target: str) -> str:
        errors: list[str] = []
        for idx, backend in enumerate(self._backends):
            try:
                return await backend.translate(text, source, target)
            except TranslationError as exc:
                errors.append(f"{type(backend).__name__}: {exc}")
                if idx + 1 < len(self._backends):
                    logger.warning(
                        "翻訳バックエンド %s で失敗したため次へフォールバックします (%s -> %s): %s",
                        type(backend).__name__,
                        source,
                        target,
                        exc,
                    )
        raise TranslationError("すべての翻訳バックエンドが失敗しました: " + " | ".join(errors))


async def gabagaba_translate(
    text: str,
    backend: TranslatorBackend,
    *,
    route: Sequence[str] = DEFAULT_TRANSLATION_ROUTE,
) -> str:
    """指定された言語ルートに沿って多段階逆翻訳を実行する。"""
    cleaned = text.strip()
    if not cleaned:
        raise TranslationError("翻訳対象のテキストが空です。")
    if len(route) < 2:
        raise ValueError("route には少なくとも2つの言語コード（開始・終了）が必要です。")

    current = cleaned
    for source, target in zip(route, route[1:]):
        if source == target:
            continue
        current = await backend.translate(current, source, target)
    return current
