"""タドコイン通貨システム・サーバー絵文字スロット・リーダーボードを提供する Cog。"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from yajubot.bot import YajuBot
from yajubot.cogs.roles import inspect_self_role
from yajubot.storage import JST

_SUB_ROLE_USER_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}


def _get_sub_role_lock(guild_id: int, user_id: int) -> asyncio.Lock:
    key = (guild_id, user_id)
    lock = _SUB_ROLE_USER_LOCKS.get(key)
    if lock is None:
        if len(_SUB_ROLE_USER_LOCKS) >= 2000:
            idle_keys = [k for k, lk in _SUB_ROLE_USER_LOCKS.items() if not lk.locked()]
            for k in idle_keys[: len(idle_keys) // 2 + 1]:
                del _SUB_ROLE_USER_LOCKS[k]
        lock = asyncio.Lock()
        _SUB_ROLE_USER_LOCKS[key] = lock
    return lock

CHAT_REWARD_AMOUNT = 2
CHAT_REWARD_COOLDOWN_SECONDS = 15.0
SLOT_COOLDOWN_SECONDS = 10.0
SLOT_MIN_BET = 500
SUB_ROLE_BILLING_SECONDS = 7 * 24 * 60 * 60

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


def _daily_rng(guild_id: int, day_str: str) -> random.Random:
    seed_bytes = hashlib.sha256(f"{guild_id}:{day_str}".encode("utf-8")).digest()
    return random.Random(int.from_bytes(seed_bytes, "big"))


def select_slot_symbols(
    guild: discord.Guild | None,
    *,
    day_key: str | None = None,
) -> list[str]:
    """サーバー絵文字から日替わり（JST日付・全ユーザー固定・2日連続防止）でスロット用シンボルを5種類選ぶ。"""
    if day_key is not None:
        target_date = date.fromisoformat(day_key)
    else:
        target_date = datetime.now(JST).date()
    guild_id = guild.id if guild is not None else 0

    usable_emojis = (
        sorted((e for e in guild.emojis if e.available), key=lambda e: e.id)
        if guild is not None
        else []
    )
    usable = [str(e) for e in usable_emojis]
    if len(usable) >= SLOT_SYMBOL_COUNT:
        pool = usable
    else:
        pool = list(usable)
        for fallback in FALLBACK_SLOT_EMOJIS:
            if fallback not in pool:
                pool.append(fallback)

    # 年初10日前から順に特賞シンボルを決定し、2日以上連続で同じ特賞にならないようにする
    start_date = date(target_date.year, 1, 1) - timedelta(days=10)
    prev_special: str | None = None
    cur_date = start_date
    special = pool[0]
    while cur_date <= target_date:
        day_str = cur_date.strftime("%Y-%m-%d")
        rng = _daily_rng(guild_id, day_str)
        candidates = (
            [s for s in pool if s != prev_special] if len(pool) > 1 else pool
        )
        special = candidates[rng.randrange(len(candidates))]
        prev_special = special
        cur_date += timedelta(days=1)

    today_rng = _daily_rng(guild_id, target_date.strftime("%Y-%m-%d"))
    remaining_pool = [s for s in pool if s != special]
    others = today_rng.sample(
        remaining_pool, min(SLOT_SYMBOL_COUNT - 1, len(remaining_pool))
    )
    return [special, *others]


# 期待値: 0.003*100 + 0.070*3 + 0.400*1.1 + 0.527*0 = 0.950 (RTP 95.0% / 当たり率 47.3%)
SLOT_OUTCOME_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("jackpot", 0.003),
    ("three_kind", 0.070),
    ("two_kind", 0.400),
    ("loss", 0.527),
)


def _draw_slot_reels(
    symbols: list[str], *, forced_outcome: str | None = None
) -> tuple[str, str, str]:
    """SLOT_OUTCOME_WEIGHTS の確率分布（または強制指定）に従って3リールの出目を抽選する。"""
    tiers = [t for t, _ in SLOT_OUTCOME_WEIGHTS]
    weights = [w for _, w in SLOT_OUTCOME_WEIGHTS]
    outcome_tier = (
        forced_outcome
        if forced_outcome in tiers
        else random.choices(tiers, weights=weights, k=1)[0]
    )

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
    forced_outcome: str | None = None,
) -> SlotOutcome:
    """3リールの出目を抽選・判定し、配当を返す。"""
    if reels is None:
        r1, r2, r3 = _draw_slot_reels(symbols, forced_outcome=forced_outcome)
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
    embed.add_field(name="賭け金", value=f"**{bet:,}** <:tadocoin:1575197619569958942>", inline=True)
    embed.add_field(
        name="配当",
        value=f"**{outcome.payout:,}** <:tadocoin:1575197619569958942>（{outcome.multiplier_text}）",
        inline=True,
    )
    embed.add_field(name="収支", value=f"**{net_text}** <:tadocoin:1575197619569958942>", inline=True)
    embed.add_field(
        name="現在の所持数",
        value=f"**{new_balance:,}** タドコイン",
        inline=True,
    )
    embed.add_field(
        name="本日の特賞シンボル（当たれば100倍）",
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
            lines.append(f"{prefix} <@{uid}> — **{balance:,}** <:tadocoin:1575197619569958942>")
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
        if message.author.bot:
            return
        if message.guild is None:
            if self.bot.storage.unmute_user_sub_notifications(message.author.id) > 0:
                try:
                    await message.channel.send(
                        "🔔 ロールの更新通知を再開したゾ。"
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass
            return
        if self.bot.user is not None and self.bot.user in message.mentions:
            if self.bot.storage.unmute_user_sub_notifications(message.author.id) > 0:
                try:
                    await message.author.send(
                        "🔔 ロールの更新通知を再開したゾ。"
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass

        now = time.monotonic()
        key = (message.guild.id, message.author.id)
        last_awarded = self._chat_cooldowns.get(key, 0.0)
        if now - last_awarded < CHAT_REWARD_COOLDOWN_SECONDS:
            return

        settings = self.bot.storage.get_guild_settings(message.guild.id)
        excluded = set(settings.tadocoin_excluded_channel_ids)
        if message.channel.id in excluded:
            return
        parent_id = getattr(message.channel, "parent_id", None)
        if parent_id is not None and parent_id in excluded:
            return

        self._chat_cooldowns[key] = now
        if len(self._chat_cooldowns) > 5000:
            cutoff = now - CHAT_REWARD_COOLDOWN_SECONDS
            stale = [k for k, ts in self._chat_cooldowns.items() if ts < cutoff]
            for k in stale:
                del self._chat_cooldowns[k]
            while len(self._chat_cooldowns) > 5000:
                del self._chat_cooldowns[next(iter(self._chat_cooldowns))]
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
            title="<:tadocoin:1575197619569958942> タドコイン残高",
            description=f"{target.mention} の所持数: **{balance:,}** タドコイン",
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="slot",
        description="タドコインを賭けてサーバー絵文字スロットを回すゾ",
    )
    @app_commands.describe(bet=f"賭けるタドコインの枚数（{SLOT_MIN_BET}以上）")
    @app_commands.guild_only()
    async def slot(
        self,
        interaction: discord.Interaction,
        bet: app_commands.Range[int, SLOT_MIN_BET, 1_000_000],
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "サーバー内のチャンネルでのみ利用できるゾ。", ephemeral=True
            )
            return

        if bet < SLOT_MIN_BET:
            await interaction.response.send_message(
                f"最低ベット額は **{SLOT_MIN_BET:,}** タドコインだゾ。",
                ephemeral=True,
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
        if len(self._slot_cooldowns) > 5000:
            cutoff = now - SLOT_COOLDOWN_SECONDS
            stale = [k for k, ts in self._slot_cooldowns.items() if ts < cutoff]
            for k in stale:
                del self._slot_cooldowns[k]
            while len(self._slot_cooldowns) > 5000:
                del self._slot_cooldowns[next(iter(self._slot_cooldowns))]

        symbols = select_slot_symbols(guild)
        forced_outcome = getattr(self.bot, "debug_next_slot_outcome", None)
        if forced_outcome is not None:
            self.bot.debug_next_slot_outcome = None
        outcome = evaluate_slot_spin(symbols, bet, forced_outcome=forced_outcome)
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

    @app_commands.command(
        name="subrole",
        description="タドコインで毎週更新のロールを購入するゾ。14万てぼったくりやろコレェ！",
    )
    @app_commands.guild_only()
    async def subrole(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "サーバー内のチャンネルでのみ利用できるゾ。", ephemeral=True
            )
            return
        plans = self.bot.storage.get_sub_role_plans(guild.id)
        if not plans:
            await interaction.response.send_message(
                "現在、購入可能なロールが設定されていないゾ。", ephemeral=True
            )
            return
        subs = self.bot.storage.get_user_subscriptions(guild.id, interaction.user.id)
        balance = self.bot.storage.get_tadocoin_balance(guild.id, interaction.user.id)
        await interaction.response.send_message(
            embed=build_sub_role_embed(guild, plans, subs, balance),
            view=SubRoleView(self.bot, guild, interaction.user.id, plans, subs),
            ephemeral=True,
        )

    async def cog_load(self) -> None:
        if not self.sub_billing_loop.is_running():
            self.sub_billing_loop.start()

    async def cog_unload(self) -> None:
        if self.sub_billing_loop.is_running():
            self.sub_billing_loop.cancel()

    @commands.Cog.listener("on_interaction")
    async def _handle_sub_mute_button(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        data = interaction.data or {}
        custom_id = str(data.get("custom_id", ""))
        if not custom_id.startswith("sub_mute:"):
            return
        parts = custom_id.split(":")
        if len(parts) != 4:
            return
        try:
            guild_id, user_id, role_id = int(parts[1]), int(parts[2]), int(parts[3])
        except ValueError:
            return
        if interaction.user.id != user_id:
            await interaction.response.send_message(
                "本人のみ操作できるゾ。", ephemeral=True
            )
            return
        self.bot.storage.set_sub_notify_muted(
            guild_id, user_id, True, role_id=role_id
        )
        disabled_view = discord.ui.View(timeout=None)
        disabled_view.add_item(
            discord.ui.Button(
                label="ミュート済み",
                style=discord.ButtonStyle.secondary,
                custom_id=custom_id,
                disabled=True,
            )
        )
        await interaction.response.edit_message(view=disabled_view)
        await interaction.followup.send(
            "更新通知をミュートしたゾ。（残高不足で更新が解除されるか、Bot に何かメッセージを送ると再開するゾ）",
            ephemeral=True,
        )

    @tasks.loop(minutes=10)
    async def sub_billing_loop(self) -> None:
        try:
            await process_sub_role_billing(self.bot, force=False)
        except Exception:
            pass

    @sub_billing_loop.before_loop
    async def _before_billing_loop(self) -> None:
        await self.bot.wait_until_ready()


class SubNotifyMuteView(discord.ui.View):
    """DM 通知に添付するステートレスな更新通知ミュートボタン View（メモリリーク防止）。"""

    def __init__(
        self, bot: YajuBot, guild_id: int, user_id: int, role_id: int
    ) -> None:
        # timeout=0 ではなくコールバック未登録の custom_id ボタンを使うことで ViewStore への永久登録を防ぐ
        super().__init__(timeout=None)
        self.bot = bot
        self.guild_id = guild_id
        self.user_id = user_id
        self.role_id = role_id
        self.add_item(
            discord.ui.Button(
                label="ミュート",
                style=discord.ButtonStyle.secondary,
                custom_id=f"sub_mute:{guild_id}:{user_id}:{role_id}",
            )
        )


async def _send_sub_dm(
    bot: YajuBot,
    guild: discord.Guild | None,
    user_id: int,
    *,
    embed: discord.Embed,
    view: discord.ui.View | None = None,
) -> None:
    target: discord.abc.Messageable | None = (
        guild.get_member(user_id) if guild is not None else None
    )
    if target is None:
        target = bot.get_user(user_id)
    if target is None:
        try:
            target = await bot.fetch_user(user_id)
        except (discord.NotFound, discord.HTTPException):
            return
    try:
        if view is not None:
            await target.send(embed=embed, view=view)
        else:
            await target.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass


async def process_sub_role_billing(
    bot: YajuBot,
    *,
    force: bool = False,
    guild_id: int | None = None,
) -> tuple[int, int, int]:
    """サブスクロールの週次引き落とし（または強制引き落とし）を実行する。

    戻り値: (更新件数, 残高不足等で剥奪・中止した件数, スキップ件数)
    """
    now_ts = int(time.time())
    targets = (
        bot.storage.get_all_subscriptions(guild_id=guild_id)
        if force
        else bot.storage.get_due_subscriptions(now_ts, guild_id=guild_id)
    )
    renewed = 0
    revoked = 0
    skipped = 0

    for gid, uid, rid, next_ts in targets:
        plans = bot.storage.get_sub_role_plans(gid)
        cost = plans.get(rid)
        guild = bot.get_guild(gid)
        if guild is None:
            skipped += 1
            continue
        role = guild.get_role(rid)

        if cost is None or cost <= 0:
            bot.storage.unsubscribe_user_role(gid, uid, rid)
            if guild is not None and role is not None:
                member = guild.get_member(uid)
                if member is None:
                    try:
                        member = await guild.fetch_member(uid)
                    except (discord.NotFound, discord.HTTPException):
                        member = None
                if member is not None and role in member.roles:
                    try:
                        await member.remove_roles(
                            role, reason="ロール購入終了のため解除"
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        pass
            revoked += 1
            continue

        ok, new_bal = bot.storage.try_spend_tadocoin(gid, uid, cost)
        if ok:
            new_next_ts = (now_ts if force else next_ts) + SUB_ROLE_BILLING_SECONDS
            bot.storage.update_subscription_next_billing(gid, uid, rid, new_next_ts)
            renewed += 1
            if not bot.storage.is_sub_notify_muted(gid, uid, role_id=rid):
                guild_name = guild.name if guild is not None else f"サーバー({gid})"
                role_name = role.name if role is not None else f"ロール({rid})"
                dm_embed = discord.Embed(
                    title="ロール更新通知",
                    description=(
                        f"**{guild_name}** のロール **{role_name}** を更新したゾ\n"
                        f"引き落とし: **-{cost:,}** <:tadocoin:1575197619569958942> / 残高: **{new_bal:,}** <:tadocoin:1575197619569958942>\n"
                        f"次回更新: <t:{new_next_ts}:R>"
                    ),
                    color=discord.Color.gold(),
                )
                await _send_sub_dm(
                    bot,
                    guild,
                    uid,
                    embed=dm_embed,
                    view=SubNotifyMuteView(bot, gid, uid, rid),
                )
            continue

        # 残高不足 -> ロール剥奪後に引き落とし中止（剥奪失敗時は孤立ロール防止のため契約を残して次回再試行）
        if role is not None:
            member = guild.get_member(uid)
            if member is None:
                try:
                    member = await guild.fetch_member(uid)
                except (discord.NotFound, discord.HTTPException):
                    member = None
            if member is not None and role in member.roles:
                try:
                    await member.remove_roles(
                        role, reason="タドコイン残高不足"
                    )
                except (discord.Forbidden, discord.HTTPException):
                    skipped += 1
                    continue
        bot.storage.unsubscribe_user_role(gid, uid, rid)
        guild_name = guild.name if guild is not None else f"サーバー({gid})"
        role_name = role.name if role is not None else f"ロール({rid})"
        revoke_embed = discord.Embed(
            title="⚠️ ロール更新止まっちゃっ...たぁ！",
            description=(
                f"**{guild_name}** のロール **{role_name}** は、タドコイン残高不足（必要: **{cost:,}** / 残高: **{new_bal:,}**）のため剥奪し、以降の引き落としを中止したゾ。\n"
                f"しかし、修行やってます。いつの日か世界を救うと信じて。"
            ),
            color=discord.Color.red(),
        )
        await _send_sub_dm(bot, guild, uid, embed=revoke_embed)
        revoked += 1

    return (renewed, revoked, skipped)


def build_sub_role_embed(
    guild: discord.Guild,
    plans: dict[int, int],
    subs: dict[int, int],
    balance: int,
    *,
    status_note: str | None = None,
) -> discord.Embed:
    """ユーザー向けロール一覧 Embed を生成する。"""
    embed = discord.Embed(
        title="ロール購入、しよう！（週次更新）",
        description=(
            "タドコインを毎週消費してロールを維持できるゾ。\n"
            "下の選択メニューから加入・解約を切り替えられるゾ（更新時に残高不足だとロールが消去され引き落としは中止されるゾ）。"
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="現在の所持タドコイン",
        value=f"**{balance:,}** <:tadocoin:1575197619569958942>",
        inline=False,
    )
    lines: list[str] = []
    for rid, cost in plans.items():
        role = guild.get_role(rid)
        if role is None:
            continue
        if rid in subs:
            next_ts = subs[rid]
            lines.append(
                f"✅ {role.mention} — **{cost:,}** タドコイン/週（次回引き落とし: <t:{next_ts}:R>）"
            )
        else:
            lines.append(f"⬜ {role.mention} — **{cost:,}** タドコイン/週（未加入）")
    plan_text = "\n".join(lines) if lines else "有効なロールがありません"
    if len(plan_text) > 1024:
        plan_text = plan_text[:1021] + "..."
    embed.add_field(
        name="プラン一覧",
        value=plan_text,
        inline=False,
    )
    if status_note:
        embed.add_field(name="処理結果", value=status_note, inline=False)
    return embed


class SubRoleView(discord.ui.View):
    """ユーザーがサブスクロールへ加入・解約する View。"""

    def __init__(
        self,
        bot: YajuBot,
        guild: discord.Guild,
        user_id: int,
        plans: dict[int, int],
        subs: dict[int, int],
    ) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.guild = guild
        self.user_id = user_id

        options: list[discord.SelectOption] = []
        for rid, cost in list(plans.items())[:25]:
            role = guild.get_role(rid)
            if role is None:
                continue
            is_sub = rid in subs
            options.append(
                discord.SelectOption(
                    label=f"{'解約: ' if is_sub else '加入: '}{role.name}"[:100],
                    description=f"週額 {cost:,} タドコイン",
                    value=str(rid),
                )
            )
        if options:
            select = discord.ui.Select(
                placeholder="購入・解除するロールを選択",
                options=options,
                min_values=1,
                max_values=1,
                row=0,
            )
            select.callback = self._on_select
            self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if (
            interaction.guild is None
            or not isinstance(interaction.user, discord.Member)
            or interaction.user.id != self.user_id
        ):
            await interaction.response.send_message(
                "このパネルは操作できないゾ。", ephemeral=True
            )
            return

        rid = int(interaction.data["values"][0])  # type: ignore[index]
        member = interaction.user
        lock = _get_sub_role_lock(interaction.guild.id, member.id)
        async with lock:
            plans = self.bot.storage.get_sub_role_plans(interaction.guild.id)
            subs = self.bot.storage.get_user_subscriptions(
                interaction.guild.id, member.id
            )
            role = interaction.guild.get_role(rid)
            cost = plans.get(rid)
            if role is None or cost is None:
                await interaction.response.send_message(
                    "そのロールは現在利用できないゾ。", ephemeral=True
                )
                return

            if rid in subs:
                # 解約
                try:
                    if role in member.roles:
                        await member.remove_roles(role, reason="ロール解約")
                except (discord.Forbidden, discord.HTTPException):
                    await interaction.response.send_message(
                        "ロールの消去に失敗したゾ（Bot権限・順位を確認してくれよな～）。",
                        ephemeral=True,
                    )
                    return
                self.bot.storage.unsubscribe_user_role(
                    interaction.guild.id, member.id, rid
                )
                note = f"{role.mention} の購入を解約したゾ。"
            else:
                # 権限昇格防止のため購入直前にも危険権限を再検証
                inspection = inspect_self_role(role)
                if inspection.is_blocked:
                    await interaction.response.send_message(
                        f"{role.mention} は高度な権限が付与されているため購入できないゾ。",
                        ephemeral=True,
                    )
                    return
                # 加入（初回分を即時引き落とし）
                ok, current = self.bot.storage.try_spend_tadocoin(
                    interaction.guild.id, member.id, cost
                )
                if not ok:
                    await interaction.response.send_message(
                        f"タドコインが足りないゾ！（必要: **{cost:,}** / 所持: **{current:,}**）",
                        ephemeral=True,
                    )
                    return
                try:
                    await member.add_roles(role, reason="ロール加入")
                except (discord.Forbidden, discord.HTTPException):
                    # ロール付与失敗時は返金
                    self.bot.storage.add_tadocoin(interaction.guild.id, member.id, cost)
                    await interaction.response.send_message(
                        "ロールの付与に失敗したため返金したゾ（Bot権限・順位を確認してくれよな～）。",
                        ephemeral=True,
                    )
                    return
                next_ts = int(time.time()) + SUB_ROLE_BILLING_SECONDS
                self.bot.storage.subscribe_user_role(
                    interaction.guild.id, member.id, rid, next_ts
                )
                note = f"{role.mention} 工事完了です...（初回 **{cost:,}** タドコイン引き落とし）"

            new_subs = self.bot.storage.get_user_subscriptions(
                interaction.guild.id, member.id
            )
            new_bal = self.bot.storage.get_tadocoin_balance(
                interaction.guild.id, member.id
            )
            await interaction.response.edit_message(
                embed=build_sub_role_embed(
                    interaction.guild, plans, new_subs, new_bal, status_note=note
                ),
                view=SubRoleView(
                    self.bot, interaction.guild, member.id, plans, new_subs
                ),
            )


async def setup(bot: YajuBot) -> None:
    await bot.add_cog(TadocoinCog(bot))
