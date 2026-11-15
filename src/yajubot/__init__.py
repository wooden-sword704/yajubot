"""yajubot パッケージのエントリーポイント。"""

from __future__ import annotations

import logging
import sys

import discord

from yajubot.bot import YajuBot
from yajubot.config import ConfigError, load_config
from yajubot.logging_setup import setup_logging


def main() -> None:
    """設定を読み込み、Discord ボットを起動する。"""
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"設定エラー: {exc}", file=sys.stderr)
        sys.exit(1)

    setup_logging(config.log_level)
    logger = logging.getLogger(__name__)
    logger.info("yajubot を起動しています...")

    bot = YajuBot(config)
    try:
        bot.run(config.token, log_handler=None)
    except discord.LoginFailure:
        logger.error(
            "Discord へのログインに失敗しました。"
            ".env の DISCORD_TOKEN が正しいか確認してください。"
        )
        sys.exit(1)
    except discord.PrivilegedIntentsRequired:
        logger.error(
            "特権インテント（Message Content Intent）の許可が必要です。\n"
            "Discord Developer Portal の該当アプリ -> 'Bot' -> 'Privileged Gateway Intents' で\n"
            "'MESSAGE CONTENT INTENT' のスイッチをオンにして保存してください。"
        )
        sys.exit(1)
