"""「MURはんがきめる」— 議題に対して5段階評価を下す Cog。"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from yajubot.bot import YajuBot
from yajubot.cogs.proxy import REPLY1_EMOJI, REPLY2_EMOJI
from yajubot.storage import JST

logger = logging.getLogger(__name__)

MUR_WEBHOOK_NAME = "MUR大先輩"
MUR_AVATAR_PATH = (
    Path(__file__).resolve().parent.parent / "assets" / "mur" / "image.png"
)
MAX_TOPIC_LENGTH = 500

MUR_RATINGS: dict[int, tuple[str, tuple[str, ...]]] = {
    1: (
        "賛成",
        (
            "当たり前だよなぁ？",
            "あっ、行きてぇなぁ",
            "いいゾ〜これ",
        ),
    ),
    2: (
        "賛成気味",
        (
            "そうだよ（便乗）",
            "おっそうだな",
            "おっ、いいゾ～",
        ),
    ),
    3: (
        "中立",
        (
            "そうかなぁ・・・？（懐疑心）",
            "腹減ったなぁ",
            "どうすっかな～俺もな～",
        ),
    ),
    4: (
        "反対気味",
        (
            "ちがうよ（否定）",
            "あっ、おい待てい",
            "ﾁｶﾚﾀ...（小声）",
        ),
    ),
    5: (
        "反対",
        (
            "ﾎﾟｯﾁｬﾏ...",
            "嘘つけ絶対良くないゾ",
            "こ無ゾ",
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class MurDecision:
    """MURはんの判定結果。"""

    level: int  # 1..5
    label: str
    comment: str


_TOPIC_TRAILING_PATTERN = re.compile(
    r"(?:ってどうなの|どうなの|でしょうか|ですか|ますか|だよね|よね|なの|かな|かね|か)+$"
)
_TOPIC_PUNCT_PATTERN = re.compile(r"[\s\?\!？！。、・,.\-]+")


def _normalize_topic_core(topic: str) -> str:
    """表記ゆれ・疑問表現の語尾を正規化してコアとなる文字列を抽出する。"""
    s = unicodedata.normalize("NFKC", topic).lower()
    s = _TOPIC_PUNCT_PATTERN.sub("", s)
    s = _TOPIC_TRAILING_PATTERN.sub("", s)
    return s or unicodedata.normalize("NFKC", topic).strip().lower()


def _simhash64(text: str) -> int:
    """文字 2-gram / 1-gram から 64-bit SimHash を算出する（似た文章ほどハミング距離が近い）。"""
    tokens: list[str] = list(text)
    if len(text) >= 2:
        tokens.extend(text[i : i + 2] for i in range(len(text) - 1))
    if len(text) >= 3:
        tokens.extend(text[i : i + 3] for i in range(len(text) - 2))

    weights = [0] * 64
    for tok in tokens:
        h = int.from_bytes(
            hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest(), "big"
        )
        for bit in range(64):
            if (h >> bit) & 1:
                weights[bit] += 1
            else:
                weights[bit] -= 1

    fp = 0
    for bit, w in enumerate(weights):
        if w >= 0:
            fp |= 1 << bit
    return fp


def judge_mur_topic(
    topic: str,
    *,
    guild_id: int = 0,
    day_key: str | None = None,
) -> MurDecision:
    """議題に対して SimHash（類似文の分散抑制）のみで 1〜5 段階評価を下す。"""
    core = _normalize_topic_core(topic)
    today_str = day_key or datetime.now(JST).strftime("%Y-%m-%d")

    salt_digest = hashlib.sha256(
        f"mur:{guild_id}:{today_str}".encode("utf-8")
    ).digest()
    day_salt64 = int.from_bytes(salt_digest[:8], "big")
    sim = _simhash64(core)

    # 似た議題は SimHash のハミング距離が近いため、popcount ベースのスコアも近くなる
    ones = (sim ^ day_salt64).bit_count()
    # Binomial(64, 0.5) の標準偏差=4 を 1.0〜5.0 へバランスよく広げて中立への過度な集中を防ぐ
    base_score = 3.0 + (ones - 32) / 1.8
    level = max(1, min(5, round(base_score)))

    label, comments = MUR_RATINGS[level]
    comment_idx = (ones + salt_digest[8]) % len(comments)
    return MurDecision(level=level, label=label, comment=comments[comment_idx])


_MUR_WEBHOOK_CACHE: dict[int, discord.Webhook] = {}
_MUR_AVATAR_BYTES: bytes | None = None
_MUR_AVATAR_LOADED: bool = False


def _get_mur_avatar_bytes() -> bytes | None:
    global _MUR_AVATAR_BYTES, _MUR_AVATAR_LOADED
    if not _MUR_AVATAR_LOADED:
        _MUR_AVATAR_LOADED = True
        if MUR_AVATAR_PATH.is_file():
            _MUR_AVATAR_BYTES = MUR_AVATAR_PATH.read_bytes()
    return _MUR_AVATAR_BYTES


async def _get_or_create_mur_webhook(
    channel: discord.TextChannel,
    bot: YajuBot,
) -> discord.Webhook:
    assert bot.user is not None
    ch_id = getattr(channel, "id", None)
    if isinstance(ch_id, int):
        cached = _MUR_WEBHOOK_CACHE.get(ch_id)
        if cached is not None and getattr(cached, "token", None) is not None:
            return cached

    webhooks = await channel.webhooks()
    for wh in webhooks:
        if (
            wh.user is not None
            and wh.user.id == bot.user.id
            and wh.name == MUR_WEBHOOK_NAME
        ):
            if isinstance(ch_id, int):
                if len(_MUR_WEBHOOK_CACHE) >= 500:
                    _MUR_WEBHOOK_CACHE.clear()
                _MUR_WEBHOOK_CACHE[ch_id] = wh
            return wh

    avatar_bytes = _get_mur_avatar_bytes()

    wh = await channel.create_webhook(
        name=MUR_WEBHOOK_NAME,
        avatar=avatar_bytes,
        reason="MURはんがきめる Webhook",
    )
    if isinstance(ch_id, int):
        if len(_MUR_WEBHOOK_CACHE) >= 500:
            _MUR_WEBHOOK_CACHE.clear()
        _MUR_WEBHOOK_CACHE[ch_id] = wh
    return wh


def build_mur_message(
    topic: str,
    executor: discord.abc.User,
    decision: MurDecision,
) -> str:
    """1行目に返信ヘッダー（通知なしメンション＋議題）、2行目以降に判定結果を組み立てる。"""
    mention = getattr(
        executor, "mention", f"@{getattr(executor, 'display_name', 'user')}"
    )
    single_line = " ".join(topic.split())
    snippet = single_line[:32] + "..." if len(single_line) > 35 else single_line
    header = f"{REPLY1_EMOJI}{REPLY2_EMOJI}{mention} :{snippet}"
    return f"{header}\n**【{decision.level}. {decision.label}】**\n{decision.comment}"


class MurCog(commands.Cog):
    """「MURはんがきめる」コマンドを提供する Cog。"""

    def __init__(self, bot: YajuBot) -> None:
        self.bot = bot

    @app_commands.command(
        name="mur",
        description="決められないことはMURはんに決めてもらおう（提案）",
    )
    @app_commands.describe(topic="MURはんに決めてほしい議題")
    @app_commands.checks.cooldown(1, 5.0, key=lambda i: (i.guild_id, i.user.id))
    @app_commands.guild_only()
    async def mur_decide(
        self,
        interaction: discord.Interaction,
        topic: str,
    ) -> None:
        cleaned = topic.strip()
        if not cleaned:
            await interaction.response.send_message(
                "おっ、そうか…？はいじゃあ議題出せ！", ephemeral=True
            )
            return
        if len(cleaned) > MAX_TOPIC_LENGTH:
            await interaction.response.send_message(
                f"議題が長すぎるゾ...（{MAX_TOPIC_LENGTH}文字くらいで見とけよ見とけよ～）",
                ephemeral=True,
            )
            return

        guild_id = interaction.guild.id if interaction.guild is not None else 0
        decision = judge_mur_topic(cleaned, guild_id=guild_id)
        output = build_mur_message(cleaned, interaction.user, decision)[:2000]

        await interaction.response.defer()

        channel = interaction.channel
        webhook_channel: discord.TextChannel | None = None
        thread: discord.abc.Snowflake = discord.utils.MISSING
        if isinstance(channel, discord.TextChannel):
            webhook_channel = channel
        elif isinstance(channel, discord.Thread) and isinstance(
            channel.parent, discord.TextChannel
        ):
            webhook_channel = channel.parent
            thread = channel

        if self.bot.user is not None and webhook_channel is not None:
            try:
                webhook = await _get_or_create_mur_webhook(webhook_channel, self.bot)
                await webhook.send(
                    content=output,
                    username=MUR_WEBHOOK_NAME,
                    thread=thread,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                try:
                    await interaction.delete_original_response()
                except discord.HTTPException:
                    pass
                return
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning(
                    "MURはん Webhook 送信に失敗したため通常応答へフォールバックします: %s",
                    exc,
                )

        await interaction.followup.send(
            output,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: YajuBot) -> None:
    await bot.add_cog(MurCog(bot))
