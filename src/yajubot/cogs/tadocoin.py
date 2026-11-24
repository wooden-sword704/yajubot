"""タドコイン通貨システム・サーバー絵文字スロット・リーダーボードを提供する Cog。"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from dataclasses import dataclass
from datetime import datetime

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from yajubot.bot import YajuBot
from yajubot.storage import JST

CHAT_REWARD_AMOUNT = 2
CHAT_REWARD_COOLDOWN_SECONDS = 15.0
SLOT_COOLDOWN_SECONDS = 10.0

FALLBACK_SLOT_EMOJIS: tuple[str, ...] = ("🍒", "🍋", "🍇", "🔔", "💎")
SLOT_SYMBOL_COUNT = 5

LEADERBOARD_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("tadocoin", "💰 タドコイン所持量"),
    ("shinkei_easy", "🃏 神経衰弱 最速クリア（かんたん）"),
    ("shinkei_normal", "🃏 神経衰弱 最速クリア（ふつう）"),
    ("shinkei_hard", "🃏 神経衰弱 最速クリア（むずかしい）"),
    ("shinkei_ikisugi", "🃏 神経衰弱 最速クリア（ｲｷｽｷﾞ）"),
)


@dataclass(frozen=True, slots=True)
class SlotOutcome:
    """スロットの抽選結果と配当。"""

    reels: tuple[str, str, str]
    payout: int
    tier_label: str
    multiplier_text: str
    is_jackpot: bool = False
    is_win: bool = False


def select_slot_symbols(
    guild: discord.Guild | None,
    *,
    day_key: str | None = None,
) -> list[str]:
    """サーバー絵文字から日替わり（JST日付・全ユーザー固定）でスロット用シンボルを5種類選ぶ。"""
    today_str = day_key or datetime.now(JST).strftime("%Y-%m-%d")
    guild_id = guild.id if guild is not None else 0
    seed_bytes = hashlib.sha256(f"{guild_id}:{today_str}".encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(seed_bytes, "big"))

    usable_emojis = (
        sorted((e for e in guild.emojis if e.available), key=lambda e: e.id)
        if guild is not None
        else []
    )
    usable = [str(e) for e in usable_emojis]
    if len(usable) >= SLOT_SYMBOL_COUNT:
        return rng.sample(usable, SLOT_SYMBOL_COUNT)

    symbols = list(usable)
    fallbacks = list(FALLBACK_SLOT_EMOJIS)
    rng.shuffle(fallbacks)
    for fallback in fallbacks:
        if len(symbols) >= SLOT_SYMBOL_COUNT:
            break
        if fallback not in symbols:
            symbols.append(fallback)
    return symbols


# 期待値: 0.01*100 + 0.09*2 + 0.40*1 + 0.50*0 = 1.58 (RTP 158.0%)
SLOT_OUTCOME_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("jackpot", 0.01),
    ("three_kind", 0.09),
    ("two_kind", 0.40),
    ("loss", 0.50),
)


def _draw_slot_reels(symbols: list[str]) -> tuple[str, str, str]:
    """95.0% RTP の確率分布に従って3リールの出目を抽選する。"""
    tiers = [t for t, _ in SLOT_OUTCOME_WEIGHTS]
    weights = [w for _, w in SLOT_OUTCOME_WEIGHTS]
    outcome_tier = random.choices(tiers, weights=weights, k=1)[0]

    if outcome_tier == "jackpot":
        sym = symbols[0]
        return (sym, sym, sym)
    if outcome_tier == "three_kind":
        pool = symbols[1:] if len(symbols) > 1 else symbols
        sym = random.choice(pool)
        return (sym, sym, sym)
    if outcome_tier == "two_kind" and len(symbols) >= 2:
        pair_sym, other_sym = random.sample(symbols, 2)
        reels = [pair_sym, pair_sym, other_sym]
        random.shuffle(reels)
        return (reels[0], reels[1], reels[2])
    if len(symbols) >= 3:
        picked = random.sample(symbols, 3)
        return (picked[0], picked[1], picked[2])
    return (symbols[0], symbols[0], symbols[0])


def evaluate_slot_spin(
    symbols: list[str],
    bet: int,
    *,
    reels: tuple[str, str, str] | None = None,
) -> SlotOutcome:
    """3リールの出目を抽選・判定し、配当を返す。"""
    if reels is None:
        r1, r2, r3 = _draw_slot_reels(symbols)
    else:
        r1, r2, r3 = reels

    if r1 == r2 == r3:
        if symbols and r1 == symbols[0]:
            payout = bet * 100
            return SlotOutcome(
                reels=(r1, r2, r3),
                payout=payout,
                tier_label="**ｲ　ｷ　ｽ　ｷﾞ　ｼﾞ　ｬ　ｯ　ｸ　ﾎﾟ　ｯ　ﾄ**",
                multiplier_text="掛け金は100倍で帰ってくるゾ。俺ノンケちゃうかもしれんな（覚醒）",
                is_jackpot=True,
                is_win=True,
            )
        payout = bet * 3
        return SlotOutcome(
            reels=(r1, r2, r3),
            payout=payout,
            tier_label="**ヤジュヤジュ勝利**",
            multiplier_text="当たり。掛け金は3倍で帰ってくるゾ",
            is_win=True,
        )

    if r1 == r2 or r2 == r3 or r1 == r3:
        payout = max(bet+1, int(bet * 1.1))   # 1.1x
        return SlotOutcome(
            reels=(r1, r2, r3),
            payout=payout,
            tier_label="**気持ちいいですね（建前）気持ちよくはない！（本音）**",
            multiplier_text="小当たり。掛け金は1.1倍で帰ってくるゾ",
            is_win=True,
        )

    return SlotOutcome(
        reels=(r1, r2, r3),
        payout=0,
        tier_label="<a:AikyouTakuya:1574817132863168543> あーつまんね",
        multiplier_text="ハズレ、掛け金は消滅。TKGW様逃げてはダメですよ？",
    )


def _build_slot_frame(r1: str, r2: str, r3: str) -> str:
    return f"{r1}  │  {r2}  │  {r3}"


def build_slot_spin_embed(
    user: discord.abc.User,
    bet: int,
    r1: str,
    r2: str,
    r3: str,
    status_text: str,
    *,
    color: discord.Color | None = None,
) -> discord.Embed:
    """スロット回転中・停止途中の Embed を生成する。"""
    embed = discord.Embed(
        title="INMU CASINO",
        description=f"{_build_slot_frame(r1, r2, r3)}\n\n{status_text}",
        color=color or discord.Color.blurple(),
    )
    embed.add_field(name="賭け金", value=f"**{bet:,}** タドコイン", inline=True)
    embed.set_footer(text=f"挑戦者: {user.display_name}")
    return embed


def build_slot_result_embed(
    user: discord.abc.User,
    bet: int,
    outcome: SlotOutcome,
    new_balance: int,
    special_symbol: str,
) -> discord.Embed:
    """スロット最終結果の Embed を生成する。"""
    r1, r2, r3 = outcome.reels
    if outcome.is_jackpot:
        color = discord.Color.gold()
    elif outcome.is_win:
        color = discord.Color.green()
    else:
        color = discord.Color.dark_red()

    net = outcome.payout - bet
    net_text = f"+{net:,}" if net >= 0 else f"{net:,}"

    embed = discord.Embed(
        title="INMU CASINO",
        description=f"{_build_slot_frame(r1, r2, r3)}\n\n{outcome.tier_label}",
        color=color,
    )
    embed.add_field(name="賭け金", value=f"**{bet:,}** <:TADOCOIN_TEMP:1574811641042698320>", inline=True)
    embed.add_field(
        name="配当",
        value=f"**{outcome.payout:,}** <:TADOCOIN_TEMP:1574811641042698320>（{outcome.multiplier_text}）",
        inline=True,
    )
    embed.add_field(name="収支", value=f"**{net_text}** <:TADOCOIN_TEMP:1574811641042698320>", inline=True)
    embed.add_field(
        name="現在の所持数",
        value=f"**{new_balance:,}** タドコイン",
        inline=True,
    )
    embed.add_field(
        name="本日の特賞シンボル（15倍）",
        value=special_symbol,
        inline=True,
    )
    embed.set_footer(text=f"挑戦者: {user.display_name}")
    return embed


def build_leaderboard_embed(
    bot: YajuBot,
    guild: discord.Guild,
    category: str,
) -> discord.Embed:
    """指定カテゴリのリーダーボード Embed を生成する。"""
    label_map = dict(LEADERBOARD_CATEGORIES)
    title_label = label_map.get(category, "💰 タドコイン所持量")

    if category == "tadocoin":
        rows = bot.storage.get_tadocoin_leaderboard(guild.id, limit=10)
        embed = discord.Embed(
            title=f"🏆 リーダーボード：{title_label}",
            color=discord.Color.gold(),
        )
        if not rows:
            embed.description = "まだタドコインを持っている人がいないゾ。"
            return embed
        medals = ("🥇", "🥈", "🥉")
        lines = []
        for idx, (uid, balance) in enumerate(rows, start=1):
            prefix = medals[idx - 1] if idx <= 3 else f"**{idx}.**"
            lines.append(f"{prefix} <@{uid}> — **{balance:,}** <:TADOCOIN_TEMP:1574811641042698320>")
        embed.description = "\n".join(lines)
        return embed

    if category.startswith("shinkei_"):
        diff_key = category.removeprefix("shinkei_")
        rows = bot.storage.get_shinkei_leaderboard(guild.id, diff_key, limit=10)
        embed = discord.Embed(
            title=f"🏆 リーダーボード：{title_label}",
            color=discord.Color.blurple(),
        )
        if not rows:
            embed.description = "この難易度のクリア記録はまだないゾ。"
            return embed
        medals = ("🥇", "🥈", "🥉")
        lines = []
        for idx, (uid, best_time, moves) in enumerate(rows, start=1):
            prefix = medals[idx - 1] if idx <= 3 else f"**{idx}.**"
            lines.append(
                f"{prefix} <@{uid}> — **{best_time:.2f}秒**（{moves}手）"
            )
        embed.description = "\n".join(lines)
        return embed

    return discord.Embed(title="🏆 リーダーボード", description="不明なカテゴリだゾ。")


class LeaderboardView(discord.ui.View):
    """リーダーボードの種類を切り替える View。"""

    def __init__(
        self, bot: YajuBot, guild: discord.Guild, current_category: str = "tadocoin"
    ) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.guild = guild
        options = [
            discord.SelectOption(
                label=label[:100],
                value=key,
                default=(key == current_category),
            )
            for key, label in LEADERBOARD_CATEGORIES
        ]
        select = discord.ui.Select(
            placeholder="表示するリーダーボードを選択",
            options=options,
            row=0,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        values = interaction.data.get("values", []) if interaction.data else []
        category = values[0] if values else "tadocoin"
        new_view = LeaderboardView(self.bot, self.guild, current_category=category)
        await interaction.response.edit_message(
            embed=build_leaderboard_embed(self.bot, self.guild, category),
            view=new_view,
        )


class TadocoinCog(commands.Cog):
    """タドコイン通貨・チャット報酬・スロット・リーダーボードを提供する Cog。"""

    def __init__(self, bot: YajuBot) -> None:
        self.bot = bot
        self._chat_cooldowns: dict[tuple[int, int], float] = {}
        self._slot_cooldowns: dict[tuple[int, int], float] = {}

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        settings = self.bot.storage.get_guild_settings(message.guild.id)
        excluded = set(settings.tadocoin_excluded_channel_ids)
        if message.channel.id in excluded:
            return
        parent_id = getattr(message.channel, "parent_id", None)
        if parent_id is not None and parent_id in excluded:
            return

        now = time.monotonic()
        key = (message.guild.id, message.author.id)
        last_awarded = self._chat_cooldowns.get(key, 0.0)
        if now - last_awarded < CHAT_REWARD_COOLDOWN_SECONDS:
            return
        self._chat_cooldowns[key] = now
        self.bot.storage.add_tadocoin(
            message.guild.id, message.author.id, CHAT_REWARD_AMOUNT
        )

    @app_commands.command(
        name="tadocoin",
        description="現在のタドコイン所持数を確認するゾ",
    )
    @app_commands.describe(member="確認したいメンバー（省略時は自分）")
    @app_commands.guild_only()
    async def tadocoin_balance(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        assert interaction.guild is not None
        target = member or interaction.user
        balance = self.bot.storage.get_tadocoin_balance(
            interaction.guild.id, target.id
        )
        embed = discord.Embed(
            title="<:TADOCOIN_TEMP:1574811641042698320> タドコイン残高",
            description=f"{target.mention} の所持数: **{balance:,}** タドコイン",
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="slot",
        description="タドコインを賭けてサーバー絵文字スロットを回すゾ",
    )
    @app_commands.describe(bet="賭けるタドコインの枚数（1以上）")
    @app_commands.guild_only()
    async def slot(
        self,
        interaction: discord.Interaction,
        bet: app_commands.Range[int, 1, 1_000_000],
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "サーバー内のチャンネルでのみ利用できるゾ。", ephemeral=True
            )
            return

        now = time.monotonic()
        cooldown_key = (guild.id, interaction.user.id)
        last_used = self._slot_cooldowns.get(cooldown_key)
        if last_used is not None:
            remaining = SLOT_COOLDOWN_SECONDS - (now - last_used)
            if remaining > 0:
                await interaction.response.send_message(
                    f"冷えてるか～？（クールダウン中） あと **{remaining:.1f}秒** 待ってから回してくれよな～",
                    ephemeral=True,
                )
                return

        ok, current_balance = self.bot.storage.try_spend_tadocoin(
            guild.id, interaction.user.id, bet
        )
        if not ok:
            await interaction.response.send_message(
                f"タドコインが足りないゾ！（賭け金: **{bet:,}** / 所持数: **{current_balance:,}**）",
                ephemeral=True,
            )
            return

        self._slot_cooldowns[cooldown_key] = now

        symbols = select_slot_symbols(guild)
        outcome = evaluate_slot_spin(symbols, bet)
        r1, r2, r3 = outcome.reels
        new_balance = (
            self.bot.storage.add_tadocoin(
                guild.id, interaction.user.id, outcome.payout
            )
            if outcome.payout > 0
            else current_balance
        )

        spin_pool = [str(e) for e in guild.emojis if e.available] or symbols

        def _spin_emoji() -> str:
            return random.choice(spin_pool)

        # 演出 1: 全リール回転開始
        await interaction.response.send_message(
            embed=build_slot_spin_embed(
                interaction.user,
                bet,
                _spin_emoji(),
                _spin_emoji(),
                _spin_emoji(),
                f"イキますよ〜イクイク",
            )
        )

        try:
            await asyncio.sleep(0.85)
            # 演出 2: 第1リール停止
            await interaction.edit_original_response(
                embed=build_slot_spin_embed(
                    interaction.user,
                    bet,
                    r1,
                    _spin_emoji(),
                    _spin_emoji(),
                    "いいよ、来いよ！",
                )
            )

            await asyncio.sleep(0.85)
            # 演出 3: 第2リール停止（同じ絵柄ならリーチ演出！）
            if r1 == r2:
                await interaction.edit_original_response(
                    embed=build_slot_spin_embed(
                        interaction.user,
                        bet,
                        r1,
                        r2,
                        "⚡",
                        "アーイキソ",
                        color=discord.Color.orange(),
                    )
                )
                await asyncio.sleep(1.35)
            else:
                await interaction.edit_original_response(
                    embed=build_slot_spin_embed(
                        interaction.user,
                        bet,
                        r1,
                        r2,
                        _spin_emoji(),
                        "出…出ますよ…",
                    )
                )
                await asyncio.sleep(0.75)

            # 演出 4: 最終結果表示
            await interaction.edit_original_response(
                embed=build_slot_result_embed(
                    interaction.user,
                    bet,
                    outcome,
                    new_balance,
                    symbols[0],
                )
            )
        except (discord.NotFound, discord.HTTPException, aiohttp.ClientError):
            pass

    @app_commands.command(
        name="leaderboard",
        description="タドコイン所持数や神経衰弱タイムなどのリーダーボードを見るゾ",
    )
    @app_commands.describe(category="最初に表示するランキングの種類")
    @app_commands.choices(
        category=[
            app_commands.Choice(name=label, value=key)
            for key, label in LEADERBOARD_CATEGORIES
        ]
    )
    @app_commands.guild_only()
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        category: str = "tadocoin",
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "サーバー内のチャンネルでのみ利用できるゾ。", ephemeral=True
            )
            return
        embed = build_leaderboard_embed(self.bot, guild, category)
        view = LeaderboardView(self.bot, guild, current_category=category)
        await interaction.response.send_message(embed=embed, view=view)


async def setup(bot: YajuBot) -> None:
    await bot.add_cog(TadocoinCog(bot))
