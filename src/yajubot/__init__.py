"""yajubot パッケージのエントリーポイント。"""

from __future__ import annotations

import logging
import subprocess
import sys

import discord

from yajubot.bot import YajuBot
from yajubot.config import ConfigError, load_config
from yajubot.debug_console import start_debug_console_thread
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
    console_handle = start_debug_console_thread(bot)
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
    finally:
        if console_handle is not None:
            _, stop_event = console_handle
            stop_event.set()
        if getattr(bot, "debug_restart_requested", False):
            logger.info("デバッグコンソールからの指示により Bot を再起動します...")
            print("\n🔄 Bot を再起動しています...", flush=True)
            # Windows では os.execv が親プロセスを即終了してコンソール窓が閉じるため、
            # 同じコンソール上で子プロセスを同期実行して引き継ぐ。
            # console_scripts (.exe) 経由起動時は sys.argv[0] が .py ではないため -m yajubot で起動する。
            result = subprocess.run(
                [sys.executable, "-m", "yajubot", *sys.argv[1:]],
                check=False,
            )
            sys.exit(result.returncode)
