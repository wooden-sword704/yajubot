"""1日1回（JST日付基準）引けるおみくじ機能とデバッグ用リセットコマンドを提供する Cog。"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from yajubot.bot import YajuBot
from yajubot.cogs.proxy import can_manage_bot
from yajubot.omikuji_data import (
    OMIKUJI_CATEGORIES,
    OMIKUJI_RANDOM_CATEGORIES,
    OMIKUJI_TIERS,
    OmikujiRandomCategory,
    OmikujiTier,
)
from yajubot.storage import JST


@dataclass(frozen=True, slots=True)
class OmikujiDrawResult:
    """抽選されたおみくじの結果。"""

    tier: OmikujiTier
    items: tuple[tuple[str, str], ...]


def get_today_jst_str(now: datetime | None = None) -> str:
    """現在の JST 日付（YYYY-MM-DD）を返す。"""
    current = now.astimezone(JST) if now is not None else datetime.now(JST)
    return current.strftime("%Y-%m-%d")


def normalize_tier_probabilities(tiers: Sequence[OmikujiTier]) -> list[float]:
    """各ティアの確率（%）を取得し、合計が 100 でない場合は全体をスケーリングする。"""
    if not tiers:
        return []
    raw_probs = [max(0.0, float(t.probability)) for t in tiers]
    total = sum(raw_probs)
    if total <= 0:
        equal = 100.0 / len(tiers)
        return [equal] * len(tiers)
    if abs(total - 100.0) > 1e-9:
        return [(p / total) * 100.0 for p in raw_probs]
    return raw_probs


def draw_omikuji(
    tiers: Sequence[OmikujiTier] = OMIKUJI_TIERS,
    categories: Sequence[str] = OMIKUJI_CATEGORIES,
    random_categories: Sequence[OmikujiRandomCategory] = OMIKUJI_RANDOM_CATEGORIES,
    *,
    forced_tier_name: str | None = None,
) -> OmikujiDrawResult:
    """1. 確率（合計100%へスケーリング）でティアを抽選し、2. ティア項目とティア非依存項目からランダムに1つずつ選ぶ。"""
    valid_tiers = [t for t in tiers if any(t.categories.values())]
    if not valid_tiers:
        raise ValueError("有効なおみくじティア・選択肢が1つも登録されていません。")

    selected_tier: OmikujiTier | None = None
    if forced_tier_name:
        for t in valid_tiers:
            if t.name == forced_tier_name or forced_tier_name in t.name:
                selected_tier = t
                break
    if selected_tier is None:
        probabilities = normalize_tier_probabilities(valid_tiers)
        selected_tier = random.choices(valid_tiers, weights=probabilities, k=1)[0]

    chosen_items: list[tuple[str, str]] = []
    # OMIKUJI_CATEGORIES の順番で抽選し、ティア独自の項目があれば後ろに追加
    ordered_keys = list(categories)
    for extra_key in selected_tier.categories:
        if extra_key not in ordered_keys:
            ordered_keys.append(extra_key)

    for key in ordered_keys:
        pool = selected_tier.categories.get(key)
        if pool:
            chosen_items.append((key, random.choice(pool)))

    # ティアに関係なくランダムに選ばれる項目を抽選
    for random_cat in random_categories:
        if random_cat.items:
            chosen_items.append((random_cat.name, random.choice(random_cat.items)))

    return OmikujiDrawResult(
        tier=selected_tier,
        items=tuple(chosen_items),
    )


from pathlib import Path

OMIKUJI_IMAGES_DIR = Path(__file__).resolve().parent.parent / "omikuji_data" / "images"

OMIKUJI_TADOCOIN_RULES: tuple[tuple[str, int], ...] = (
    ("お　お　あ　た　り", 300000),
    ("ビリー兄貴級", 12000),
    ("野獣先輩級", 8000),
    ("MUR大先輩級", 5000),
    ("拓也さん級", 3000),
    ("お　お　は　ず　れ", -5000),
)


def get_omikuji_tadocoin_delta(tier_name: str) -> int:
    """おみくじのティアに応じたタドコイン増減量を返す。"""
    for keyword, delta in OMIKUJI_TADOCOIN_RULES:
        if keyword in tier_name:
            return delta
    return 0


def _get_tier_image_path(tier_name: str) -> Path | None:
    if "お　お　は　ず　れ" in tier_name:
        return OMIKUJI_IMAGES_DIR / "oohazure.jpg"
    if "お　お　あ　た　り" in tier_name:
        return OMIKUJI_IMAGES_DIR / "ooatari.jpg"
    return None


def build_omikuji_embed(
    user: discord.abc.User,
    result: OmikujiDrawResult,
    drawn_at: str,
    *,
    tadocoin_delta: int = 0,
    tadocoin_balance: int | None = None,
) -> tuple[discord.Embed, discord.File | None]:
    """おみくじ結果表示用の Embed と添付画像を生成する。"""
    embed = discord.Embed(
        title=f"{user.display_name}のおみくじ",
        description=f"**運勢**\n{result.tier.name}",
        color=discord.Color(result.tier.color),
    )
    for category, value in result.items:
        embed.add_field(name=category, value=value, inline=False)

    if tadocoin_delta != 0 or tadocoin_balance is not None:
        sign = "+" if tadocoin_delta >= 0 else ""
        bal_text = (
            f"（現在: **{tadocoin_balance:,}** <:tadocoin:1575197619569958942>）"
            if tadocoin_balance is not None
            else ""
        )
        embed.add_field(
            name="<:tadocoin:1575197619569958942> タドコイン",
            value=f"**{sign}{tadocoin_delta:,}** タドコイン {bal_text}".strip(),
            inline=False,
        )

    image_file: discord.File | None = None
    image_path = _get_tier_image_path(result.tier.name)
    if image_path is not None and image_path.is_file():
        image_file = discord.File(image_path, filename=image_path.name)
        embed.set_image(url=f"attachment://{image_path.name}")

    return embed, image_file


class OmikujiCog(commands.Cog):
    """おみくじコマンドを提供する Cog。"""

    # デバッグ用: True の間は1日1回制限を無効化（本番は必ず False）
    DEBUG_NO_COOLDOWN: bool = False

    def __init__(self, bot: YajuBot) -> None:
        self.bot = bot
        self._draw_locks: dict[tuple[int, int], asyncio.Lock] = {}

    @app_commands.command(
        name="omikuji",
        description="ホモみくじを引いてウン勢♂を占おう（提案）",
    )
    @app_commands.guild_only()
    async def omikuji(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "サーバー内のチャンネルでだけ利用できるからね。そして天は鳴き、大地は震えるだろうね", ephemeral=True
            )
            return

        today_jst = get_today_jst_str()
        lock_key = (guild.id, interaction.user.id)
        lock = self._draw_locks.get(lock_key)
        if lock is None:
            if len(self._draw_locks) >= 2000:
                idle_keys = [k for k, lk in self._draw_locks.items() if not lk.locked()]
                for k in idle_keys[: len(idle_keys) // 2 + 1]:
                    del self._draw_locks[k]
            lock = asyncio.Lock()
            self._draw_locks[lock_key] = lock

        async with lock:
            if not self.DEBUG_NO_COOLDOWN:
                existing = self.bot.storage.get_omikuji_today(
                    guild.id, interaction.user.id, today_jst
                )
                if existing is not None:
                    result_name, drawn_at = existing
                    await interaction.response.send_message(
                        f"今日はもうおみくじを引いたゾ！（本日の結果: **{result_name}** / {drawn_at}）\n"
                        "また明日（0:00 JST 以降）に引いてくれよな～頼むよ～",
                        ephemeral=True,
                    )
                    return

            forced_tier = getattr(self.bot, "debug_next_omikuji_tier", None)
            if forced_tier is not None:
                self.bot.debug_next_omikuji_tier = None
            result = draw_omikuji(forced_tier_name=forced_tier)
            drawn_at = self.bot.storage.record_omikuji_draw(
                guild.id, interaction.user.id, today_jst, result.tier.name
            )
            delta = get_omikuji_tadocoin_delta(result.tier.name)
            new_balance = self.bot.storage.add_tadocoin(
                guild.id, interaction.user.id, delta
            )
            embed, image_file = build_omikuji_embed(
                interaction.user,
                result,
                drawn_at,
                tadocoin_delta=delta,
                tadocoin_balance=new_balance,
            )
            if image_file is not None:
                await interaction.response.send_message(embed=embed, file=image_file)
            else:
                await interaction.response.send_message(embed=embed)


async def setup(bot: YajuBot) -> None:
    await bot.add_cog(OmikujiCog(bot))
