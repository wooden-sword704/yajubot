"""Bot の接続状態やサーバー参加・離脱などのライフサイクルイベントを処理する Cog。"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from yajubot.bot import YajuBot

logger = logging.getLogger(__name__)


class LifecycleEvents(commands.Cog):
    """起動完了・サーバー参加などの基本イベントハンドラ。"""

    def __init__(self, bot: YajuBot) -> None:
        self.bot = bot
        self._initialized = False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        user = self.bot.user
        if user is None:
            return

        if not self._initialized:
            self._initialized = True
            logger.info(
                "ログイン完了: %s (ID: %s) / 参加サーバー数: %d",
                user,
                user.id,
                len(self.bot.guilds),
            )
            target_guild_id = self.bot.config.guild_id
            if (
                target_guild_id is not None
                and self.bot.get_guild(target_guild_id) is None
            ):
                joined = ", ".join(
                    f"{g.name} (ID: {g.id})" for g in self.bot.guilds
                ) or "なし"
                logger.warning(
                    "DISCORD_GUILD_ID (%s) に一致する参加サーバーが見つかりません。"
                    "チャンネル ID 等を指定していないか確認してください。参加中サーバー: %s",
                    target_guild_id,
                    joined,
                )
            await self.bot.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name="/help",
                )
            )
        else:
            logger.info("Gateway に再接続しました: %s", user)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        logger.info(
            "新しいサーバーに参加しました: %s (ID: %s, メンバー数: %s)",
            guild.name,
            guild.id,
            guild.member_count,
        )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        logger.info("サーバーから退出しました: %s (ID: %s)", guild.name, guild.id)


async def setup(bot: YajuBot) -> None:
    await bot.add_cog(LifecycleEvents(bot))
