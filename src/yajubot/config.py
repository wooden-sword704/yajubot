"""環境変数の読み込みと設定バリデーションを担当するモジュール。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

VALID_LOG_LEVELS: frozenset[str] = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)
_TOKEN_PLACEHOLDER = "your_discord_bot_token_here"


class ConfigError(ValueError):
    """環境変数・設定値の不備を表す例外。"""


@dataclass(frozen=True, slots=True)
class BotConfig:
    """Bot の動作に必要な設定値を保持するイミュータブルな設定オブジェクト。"""

    token: str = field(repr=False)
    guild_id: int | None = None
    log_level: str = "INFO"
    aicord_client_id: str | None = None
    aicord_client_secret: str | None = field(default=None, repr=False)
    aicord_redirect_uri: str | None = None


def _parse_guild_id(raw_value: str | None) -> int | None:
    if raw_value is None:
        return None
    stripped = raw_value.strip()
    if not stripped:
        return None
    try:
        guild_id = int(stripped)
    except ValueError as exc:
        raise ConfigError(
            "DISCORD_GUILD_ID は正の整数（Discord サーバー ID）で指定してください。"
        ) from exc
    if guild_id <= 0:
        raise ConfigError(
            "DISCORD_GUILD_ID は正の整数（Discord サーバー ID）で指定してください。"
        )
    return guild_id


def _parse_log_level(raw_value: str | None) -> str:
    level = (raw_value or "INFO").strip().upper()
    if not level:
        return "INFO"
    if level not in VALID_LOG_LEVELS:
        allowed = ", ".join(sorted(VALID_LOG_LEVELS))
        raise ConfigError(
            f"LOG_LEVEL '{level}' は無効です。次のいずれかを指定してください: {allowed}"
        )
    return level


def load_config(
    environ: Mapping[str, str] | None = None,
    *,
    env_file: Path | str | None = ".env",
) -> BotConfig:
    """環境変数（および `.env` ファイル）を読み込み、検証済み `BotConfig` を返す。

    Args:
        environ: テスト等で環境変数を差し替えるためのマッピング。省略時は `.env` 読み込み後に `os.environ` を参照する。
        env_file: `environ` 省略時に読み込む `.env` ファイルのパス。`None` の場合は `.env` を読み込まない。

    Raises:
        ConfigError: 必須項目が未設定、または形式が不正な場合。
    """
    if environ is None:
        if env_file is not None:
            load_dotenv(dotenv_path=env_file, override=False)
        source: Mapping[str, str] = os.environ
    else:
        source = environ

    raw_token = source.get("DISCORD_TOKEN", "").strip()
    if not raw_token or raw_token == _TOKEN_PLACEHOLDER:
        raise ConfigError(
            "DISCORD_TOKEN が設定されていません。.env.example を .env にコピーし、"
            "Discord Developer Portal で取得した Bot トークンを設定してください。"
        )

    guild_id = _parse_guild_id(source.get("DISCORD_GUILD_ID"))
    log_level = _parse_log_level(source.get("LOG_LEVEL"))
    aicord_client_id = source.get("AICORD_CLIENT_ID", "").strip() or None
    aicord_client_secret = source.get("AICORD_CLIENT_SECRET", "").strip() or None
    aicord_redirect_uri = source.get("AICORD_REDIRECT_URI", "").strip() or None

    return BotConfig(
        token=raw_token,
        guild_id=guild_id,
        log_level=log_level,
        aicord_client_id=aicord_client_id,
        aicord_client_secret=aicord_client_secret,
        aicord_redirect_uri=aicord_redirect_uri,
    )
