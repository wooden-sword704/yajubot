"""標準出力向けのロギング設定モジュール。"""

from __future__ import annotations

import logging
import sys

LOG_FORMAT = "[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(log_level: str = "INFO") -> None:
    """アプリケーション全体および discord.py のログ出力を設定する。"""
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    logging.basicConfig(
        level=numeric_level,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
        stream=sys.stdout,
        force=True,
    )

    # HTTP/Gateway の詳細ログは DEBUG 指定時のみ INFO 相当まで出し、通常時は静かに保つ
    discord_transport_level = (
        logging.INFO if numeric_level <= logging.DEBUG else logging.WARNING
    )
    logging.getLogger("discord.http").setLevel(discord_transport_level)
    logging.getLogger("discord.gateway").setLevel(discord_transport_level)
