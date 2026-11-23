"""便乗機能（設定チャンネルで一定確率で便乗メッセージを Webhook 送信する Cog）。"""

from __future__ import annotations

import logging
import random
from pathlib import Path

import discord
from discord.ext import commands

from yajubot.bot import YajuBot

logger = logging.getLogger(__name__)

BINJOU_WEBHOOK_NAME: str = "そうだよ(便乗)BOT[仮]"
BINJOU_AVATAR_PATH: Path = (
    Path(__file__).resolve().parent.parent / "assets" / "binjou_avatar.jpg"
)

BINJOU_MESSAGES: tuple[str, ...] = (
    "そうだよ（便乗）",
    "そうっすね～（便乗）",
    "そうですよ（便乗）",
    "あっ、そうだ（唐突）",
    "せやな（便乗）",
    "そうかなぁ・・・？（懐疑心）",
    "早くしろ〜",
    "おっ、そうだな...あっ、そうだ！（唐突）",
    "そうだっつってんだろうが！(逆上)",
    "争だよ（戦場）",
    "ソーダよ（瓶状）",
    "葬だよ（葬式）",
    "宋だよ（王朝）",
    "曹だよ（粉状）",
    "掃だよ（洗浄）",
    "層だよ（層状）",
    "SAWだよ（過去形）",
    "嘘つけ絶対見てたぞ（ホモは嘘つき）",
    "ランドセルすっごい重かったゾ～",
    "さうだよ(びんぜう)",
    "腹減ったなぁ",
    "おっ、そうだな",
)


async def _get_or_create_binjou_webhook(
    channel: discord.TextChannel,
    bot_user_id: int,
) -> discord.Webhook:
    webhooks = await channel.webhooks()
    for wh in webhooks:
        if wh.user is not None and wh.user.id == bot_user_id and wh.name == BINJOU_WEBHOOK_NAME:
            return wh

    avatar_bytes: bytes | None = None
    if BINJOU_AVATAR_PATH.is_file():
        avatar_bytes = BINJOU_AVATAR_PATH.read_bytes()

    return await channel.create_webhook(
        name=BINJOU_WEBHOOK_NAME,
        avatar=avatar_bytes,
        reason="便乗機能用 Webhook",
    )


class BinjouCog(commands.Cog):
    """設定されたチャンネルで確率に応じて便乗メッセージを送信する Cog。"""

    def __init__(self, bot: YajuBot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.webhook_id is not None:
            return
        if message.guild is None:
            return

        settings = self.bot.storage.get_guild_settings(message.guild.id)
        if message.channel.id not in settings.binjou_channel_ids:
            return
        if settings.binjou_probability <= 0.0:
            return
        if random.random() * 100.0 >= settings.binjou_probability:
            return

        bot_user = self.bot.user
        if bot_user is None:
            return

        target_channel = message.channel
        thread: discord.abc.Snowflake = discord.utils.MISSING
        if isinstance(target_channel, discord.Thread):
            if target_channel.parent is None or not isinstance(
                target_channel.parent, discord.TextChannel
            ):
                return
            thread = target_channel
            webhook_channel = target_channel.parent
        elif isinstance(target_channel, discord.TextChannel):
            webhook_channel = target_channel
        else:
            return

        try:
            webhook = await _get_or_create_binjou_webhook(webhook_channel, bot_user.id)
            await webhook.send(
                content=random.choice(BINJOU_MESSAGES),
                username=BINJOU_WEBHOOK_NAME,
                thread=thread,
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.debug(
                "便乗メッセージの送信に失敗しました (channel_id=%s)",
                message.channel.id,
                exc_info=True,
            )


async def setup(bot: YajuBot) -> None:
    await bot.add_cog(BinjouCog(bot))
