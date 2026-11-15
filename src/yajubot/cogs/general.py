"""基本スラッシュコマンド（/ping, /info, /help）を提供する Cog。"""

from __future__ import annotations

import math
import platform
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from yajubot.bot import YajuBot


def _format_uptime(started_at: datetime, now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    total_seconds = max(0, int((current - started_at).total_seconds()))
    days, rem = divmod(total_seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes, seconds = divmod(rem, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}日")
    if hours or parts:
        parts.append(f"{hours}時間")
    if minutes or parts:
        parts.append(f"{minutes}分")
    parts.append(f"{seconds}秒")
    return " ".join(parts)


def _iter_slash_entries(
    commands_list: list[app_commands.Command | app_commands.Group],
) -> list[tuple[str, str]]:
    """コマンド一覧（Group 内の実行可能なサブコマンド含む）を (表示名, 説明) の配列に展開する。"""
    entries: list[tuple[str, str]] = []
    for cmd in commands_list:
        if isinstance(cmd, app_commands.Group):
            for sub in cmd.walk_commands():
                if not isinstance(sub, app_commands.Group):
                    entries.append(
                        (f"/{sub.qualified_name}", sub.description or "説明なし")
                    )
        else:
            entries.append((f"/{cmd.qualified_name}", cmd.description or "説明なし"))
    return entries


class GeneralCog(commands.Cog):
    """基本的な確認用・案内用スラッシュコマンド群。"""

    def __init__(self, bot: YajuBot) -> None:
        self.bot = bot

    @app_commands.command(name="ping", description="いつレイテンシ計測を受けてもいいように…（有能参謀）")
    async def ping(self, interaction: discord.Interaction) -> None:
        latency = self.bot.latency
        latency_text = (
            "ｵｫﾝ..."
            if math.isnan(latency)
            else f"{round(latency * 1000)} ms"
        )

        embed = discord.Embed(
            title="ｵｫﾝ!ｧｫﾝ!",
            description=f"WebSocket レイテンシ: **{latency_text}**",
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="info", description="俺は、管理ボット、変態サーバー管理青年アキラ")
    async def info(self, interaction: discord.Interaction) -> None:
        bot_user = self.bot.user
        command_count = len(
            self.bot.tree.get_commands(type=discord.AppCommandType.chat_input)
        )

        embed = discord.Embed(
            title=bot_user.display_name if bot_user else "Bot 情報",
            color=discord.Color.blurple(),
        )
        if bot_user and bot_user.display_avatar:
            embed.set_thumbnail(url=bot_user.display_avatar.url)

        embed.add_field(name="稼働時間", value=_format_uptime(self.bot.started_at), inline=True)
        embed.add_field(name="参加サーバー数", value=f"{len(self.bot.guilds)}", inline=True)
        embed.add_field(name="登録コマンド数", value=f"{command_count}", inline=True)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="help", description="今日も俺、恥ずかしいコマンド集いっぱい晒すよ？")
    async def help_command(self, interaction: discord.Interaction) -> None:
        global_cmds = self.bot.tree.get_commands(type=discord.AppCommandType.chat_input)
        guild_cmds = (
            self.bot.tree.get_commands(
                guild=interaction.guild, type=discord.AppCommandType.chat_input
            )
            if interaction.guild is not None
            else []
        )

        # copy_global_to による重複を除外しつつ順序を維持する
        merged: dict[str, tuple[str, str]] = {}
        for name, desc in _iter_slash_entries([*global_cmds, *guild_cmds]):
            merged.setdefault(name, (name, desc))

        embed = discord.Embed(
            title="コマンド一覧",
            color=discord.Color.blurple(),
        )
        if merged:
            lines = [
                f"`{name}` — {desc}"
                for name, desc in sorted(merged.values(), key=lambda item: item[0])
            ]
            embed.description = "\n".join(lines)[:4096]
        else:
            embed.description = "登録されているスラッシュコマンドがないやん！どうしてくれんのこれ！"

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: YajuBot) -> None:
    await bot.add_cog(GeneralCog(bot))
