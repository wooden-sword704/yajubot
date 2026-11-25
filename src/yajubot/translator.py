"""完全無料 API を組み合わせた多段階逆翻訳（ガバガバ翻訳）モジュール。"""

from __future__ import annotations

import logging
import random
import re
from collections.abc import Callable, Sequence
from typing import Any, Protocol

import aiohttp
import pykakasi

logger = logging.getLogger(__name__)

# ランダム逆翻訳で使用するマイナー言語プール（文字体系・語順・低リソース度が高い言語群）
MINOR_LANGUAGE_POOL: tuple[str, ...] = (
    "la",   # ラテン語
    "am",   # アムハラ語
    "km",   # クメール語
    "ay",   # アイマラ語
    "gn",   # グアラニー語
    "dv",   # ディベヒ語
    "ti",   # ティグリニャ語
    "eo",   # エスペラント
    "zu",   # ズールー語
    "mn",   # モンゴル語
    "ht",   # ハイチ・クレオール語
    "yi",   # イディッシュ語
    "haw",  # ハワイ語
    "sm",   # サモア語
    "mg",   # マダガスカル語
    "sn",   # ショナ語
    "xh",   # コサ語
    "mi",   # マオリ語
)
DEFAULT_RELAY_COUNT: int = 4

DEFAULT_TIMEOUT_SECONDS: float = 10.0
MYMEMORY_MAX_QUERY_CHARS: int = 500

_KAKASI = pykakasi.kakasi()
_PUNCTUATION_RE = re.compile(
    r"[、。，．,.!！?？…・「」『』（）()［］\[\]【】｛｝{}〈〉《》〜~]+"
)


def preprocess_for_gabagaba(text: str) -> str:
    """日本語テキストをローカルでひらがな化し、句読点を削除して誤訳を誘発する。"""
    converted = "".join(item["hira"] for item in _KAKASI.convert(text))
    stripped = _PUNCTUATION_RE.sub("", converted)
    normalized = re.sub(r"\s+", " ", stripped).strip()
    return normalized or text.strip()


def build_random_route(
    pool: Sequence[str] = MINOR_LANGUAGE_POOL,
    count: int = DEFAULT_RELAY_COUNT,
) -> tuple[str, ...]:
    """言語プールからランダムに指定数を選び、`ja -> ... -> ja` のルートを生成する。"""
    k = max(1, min(count, len(pool)))
    selected = random.sample(list(pool), k=k)
    return ("ja", *selected, "ja")


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
        }
        try:
            async with session.post(
                self.ENDPOINT,
                params=params,
                data={"q": text},
                timeout=self._timeout,
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


# 一時フラグ: True の間は MyMemory API をバイパスして直接 Google 非公式翻訳を使う（False で元に戻る）
BYPASS_MYMEMORY: bool = True


def build_default_translator(
    session_factory: Callable[[], aiohttp.ClientSession],
    *,
    bypass_mymemory: bool | None = None,
) -> TranslatorBackend:
    """デフォルトの翻訳バックエンドを構築する。"""
    use_bypass = BYPASS_MYMEMORY if bypass_mymemory is None else bypass_mymemory
    if use_bypass:
        return FallbackTranslator((GoogleUnofficialTranslator(session_factory),))
    return FallbackTranslator(
        (
            MyMemoryTranslator(session_factory),
            GoogleUnofficialTranslator(session_factory),
        )
    )


async def gabagaba_translate(
    text: str,
    backend: TranslatorBackend,
    *,
    route: Sequence[str] | None = None,
    preprocess: bool = True,
) -> str:
    """指定された言語ルート（省略時はランダム）に沿って多段階逆翻訳を実行する。"""
    cleaned = text.strip()
    if not cleaned:
        raise TranslationError("翻訳対象のテキストが空です。")

    active_route = build_random_route() if route is None else tuple(route)
    if len(active_route) < 2:
        raise ValueError("route には少なくとも2つの言語コード（開始・終了）が必要です。")

    current = preprocess_for_gabagaba(cleaned) if preprocess else cleaned
    for source, target in zip(active_route, active_route[1:]):
        if source == target:
            continue
        current = await backend.translate(current, source, target)
    return current
