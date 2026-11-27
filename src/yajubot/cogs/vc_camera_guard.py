"""VC でカメラがオンになったメンバーを即座に切断し、VC チャットへ警告を送信する Cog。"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from yajubot.bot import YajuBot

logger = logging.getLogger(__name__)


class VcCameraGuardCog(commands.Cog):
    """VC のカメラオンを検知して即座に VC から切断する Cog。"""

    def __init__(self, bot: YajuBot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return
        vc = after.channel
        if vc is None:
            return
        if not after.self_video or before.self_video:
            return

        try:
            await member.move_to(None, reason="VCでカメラがオンになったため自動切断")
        except discord.Forbidden:
            logger.warning(
                "VC からの切断権限（メンバーを移動）がありません: guild=%s user=%s",
                member.guild.id,
                member.id,
            )
            return
        except discord.HTTPException as exc:
            logger.warning("VC からの切断に失敗しました: %s", exc)
            return

        try:
            await vc.send(
                f"{member.mention} カメラをオンにしたため、VCから切断したゾ！（カメラはオフにしてから入り直してくれよな～）"
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("VC チャットへの警告送信に失敗しました: %s", exc)


async def setup(bot: YajuBot) -> None:
    await bot.add_cog(VcCameraGuardCog(bot))
