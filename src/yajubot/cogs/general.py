"""基本スラッシュコマンド（/ping, /info, /help）を提供する Cog。"""

from __future__ import annotations

import math
import platform
import random
import time
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


CHINPOKO_DANCE_TEXT = (
    "またチンポコダンスを踊るぜ？（初耳）\n"
    "チンチンブラブラソーセージ♪おっとこの股間で揺れている♪\n"
    "チンチンブラブラソーセージ♪おっとこの股間で揺れている♪\n"
    "チン！チン！ブラ！ブラ！ソー！セー！ジ！"
)
CHINPOKO_KENJA_RESPONSES: tuple[str, ...] = (
    "ﾁﾝﾎﾟﾁﾝﾎﾟオォ、これ以上やるとオォ俺のチンポが爆発するぜ\n（オナアキはあと約 {minutes} 分賢者タイムです。さっさと爆発しろ）",
    "俺、恥ずかしい思いするのが俺の仕事だから…こんなこともう、嫌で、嫌でしょうがないけど…はっきり言って俺後悔してる…だけどもう…今更手遅れだから、俺は…！もう覚悟を決めて、一生、聡ずかしい思いをしながら、生きていくぜ？俺の生き様を、しっかりと…アッ見てくれ\n（オナアキはあと約 {minutes} 分賢者タイムです。は～つっかえ！）",
    "俺、後悔してるのに、もう、手遅れだなんて…こんなの辛いよぉ\n（オナアキはあと約 {minutes} 分賢者タイムです。は～つっかえ！）",
    "俺はこんなに汚い尻をしているんだぞ！（脅迫）\n（オナアキはあと約 {minutes} 分賢者タイムです。は～つっかえ！）",
)
IKISUGI_TEXT = "ｾﾞｪﾊｧ…ｾﾞｪﾊｧ(ﾎｫﾝ!)…ｱｱｯ!ﾊｧｯ…ﾊｯ　ｲｷｽｷﾞｨ!(ﾎｫﾝ!)ｲｸｩｲｸｲｸｩｨｸ…ｱｯﾊｯ、\n## ﾝｱｯｰ!!\nｱｧｯｱｯ…ｱｯ…ﾊﾝ、ｳｯ!!…ｯｱ…ｯｱｧﾝ…ｯｱｧ…ｱｧ…ｯｱ…"
IKISUGI_KENJA_RESPONSES: tuple[str, ...] = (
    "ｸｩｰﾝ...\n（あと約 {minutes} 分賢者タイムです。は～つっかえ！）",
    "やめたくなりますよなんか部っ活ぅ～\n（あと約 {minutes} 分賢者タイムです。さっさと休めおしゃべりうんち）",
    "ぬわあああああん疲れたもおおおおおん\n（あと約 {minutes} 分賢者タイムです。は～つっかえ！）",
)


class GeneralCog(commands.Cog):
    """基本的な確認用・案内用スラッシュコマンド群。"""

    def __init__(self, bot: YajuBot) -> None:
        self.bot = bot
        # (until_monotonic, last_reward_tadocoin)
        self._kenja_until: dict[tuple[int, int], tuple[float, int]] = {}
        self._ikisugi_until: dict[tuple[int, int], tuple[float, int]] = {}

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

    async def _try_enter_kenja(
        self,
        cooldown_map: dict[tuple[int, int], tuple[float, int]],
        interaction: discord.Interaction,
        kenja_responses: tuple[str, ...],
        reward_reason: str,
    ) -> int | None:
        """賢者タイム中なら警告と前回獲得量を返して None、そうでなければ新たな賢者タイムを設定し獲得タドコイン量を返す。"""
        now = time.monotonic()
        key = (interaction.guild_id or 0, interaction.user.id)
        entry = cooldown_map.get(key)
        if entry is not None and now < entry[0]:
            until, last_reward = entry
            rem_min = max(1, math.ceil((until - now) / 60.0))
            msg = random.choice(kenja_responses).format(minutes=rem_min)
            if last_reward > 0:
                msg += f"\n（前回の獲得: **{last_reward:,}** タドコイン — {reward_reason}）"
            await interaction.response.send_message(msg, ephemeral=True)
            return None

        kenja_minutes = random.randint(10, 240)
        # CT が短いほど少なく(10分→100)、長いほど多く(240分→500)
        reward = round(100 + (kenja_minutes - 10) * 400 / (240 - 10))
        if interaction.guild_id is not None:
            self.bot.storage.add_tadocoin(interaction.guild_id, interaction.user.id, reward)
        else:
            reward = 0

        cooldown_map[key] = (now + kenja_minutes * 60.0, reward)
        if len(cooldown_map) > 5000:
            stale = [k for k, (ts, _) in cooldown_map.items() if ts <= now]
            for k in stale:
                del cooldown_map[k]
            while len(cooldown_map) > 5000:
                del cooldown_map[next(iter(cooldown_map))]
        return reward

    @app_commands.command(name="chinpokodance", description="またチンポコダンスを踊るぜ？（初耳）")
    async def chinpokodance(self, interaction: discord.Interaction) -> None:
        reason = "オナアキのチンポコダンスの衝撃で財布から飛んで行った"
        reward = await self._try_enter_kenja(
            self._kenja_until, interaction, CHINPOKO_KENJA_RESPONSES, reason
        )
        if reward is None:
            return
        text = CHINPOKO_DANCE_TEXT
        if reward > 0:
            text += f"\n（{reason}ため **+{reward:,}** タドコイン入手！）"
        await interaction.response.send_message(text)

    @app_commands.command(name="ikisugi", description="まくらが　でかすぎる　よかんがする")
    async def ikisugi(self, interaction: discord.Interaction) -> None:
        reason = "野獣先輩が屋上に忘れた財布から取ってきた"
        reward = await self._try_enter_kenja(
            self._ikisugi_until, interaction, IKISUGI_KENJA_RESPONSES, reason
        )
        if reward is None:
            return
        text = IKISUGI_TEXT
        # if reward > 0:
        #   text += f"\n（{reason}ため **+{reward:,}** タドコイン入手！）"
        await interaction.response.send_message(text)


async def setup(bot: YajuBot) -> None:
    await bot.add_cog(GeneralCog(bot))
