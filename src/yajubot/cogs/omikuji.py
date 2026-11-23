"""1日1回（JST日付基準）引けるおみくじ機能とデバッグ用リセットコマンドを提供する Cog。"""

from __future__ import annotations

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
) -> OmikujiDrawResult:
    """1. 確率（合計100%へスケーリング）でティアを抽選し、2. ティア項目とティア非依存項目からランダムに1つずつ選ぶ。"""
    valid_tiers = [t for t in tiers if any(t.categories.values())]
    if not valid_tiers:
        raise ValueError("有効なおみくじティア・選択肢が1つも登録されていません。")

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
) -> tuple[discord.Embed, discord.File | None]:
    """おみくじ結果表示用の Embed と添付画像を生成する。"""
    embed = discord.Embed(
        title=f"{user.display_name}のおみくじ",
        color=discord.Color(result.tier.color),
    )
    embed.add_field(name="運勢", value=result.tier.name, inline=False)
    for category, value in result.items:
        embed.add_field(name=category, value=value, inline=False)

    image_file: discord.File | None = None
    image_path = _get_tier_image_path(result.tier.name)
    if image_path is not None and image_path.is_file():
        image_file = discord.File(image_path, filename=image_path.name)
        embed.set_image(url=f"attachment://{image_path.name}")

    return embed, image_file


class OmikujiCog(commands.Cog):
    """おみくじコマンドを提供する Cog。"""

    # デバッグ用: True の間は1日1回制限を無効化（False に戻すと制限が復活）
    DEBUG_NO_COOLDOWN: bool = True

    def __init__(self, bot: YajuBot) -> None:
        self.bot = bot

    @app_commands.command(
        name="omikuji",
        description="おみくじを引きます",
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

        result = draw_omikuji()
        drawn_at = self.bot.storage.record_omikuji_draw(
            guild.id, interaction.user.id, today_jst, result.tier.name
        )
        embed, image_file = build_omikuji_embed(interaction.user, result, drawn_at)
        if image_file is not None:
            await interaction.response.send_message(embed=embed, file=image_file)
        else:
            await interaction.response.send_message(embed=embed)


async def setup(bot: YajuBot) -> None:
    await bot.add_cog(OmikujiCog(bot))
