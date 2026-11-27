"""便乗機能（設定チャンネルで一定確率で便乗メッセージを Webhook 送信する Cog）。"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections import deque
from pathlib import Path

import discord
from discord.ext import commands

from yajubot.bot import YajuBot
from yajubot.cogs.proxy import extract_retry_after, is_ratelimited_error

logger = logging.getLogger(__name__)

BINJOU_WEBHOOK_NAME: str = "そうだよ(便乗)BOT[仮]"
BINJOU_AVATAR_PATH: Path = (
    Path(__file__).resolve().parent.parent / "assets" / "binjou_avatar.jpg"
)
BINJOU_IMAGES_DIR: Path = (
    Path(__file__).resolve().parent.parent / "assets" / "binjou_images"
)
BINJOU_IMAGE_SUFFIXES: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".gif", ".webp")
BINJOU_IMAGE_PROBABILITY: float = 0.001  # 通常便乗時の 0.1%
BINJOU_OTHER_TEXT_PROBABILITY: float = 0.25  # 「そうだよ/そうですよ（便乗）」以外の割合 25%
SPECIAL_BINJOU_BONUS_PERCENT: float = 10.0  # 特別便乗時のベース確率への加算 (+10%)
BINJOU_RATELIMIT_SILENCE_SECONDS: float = 10.0  # レート制限時の最小サイレンス秒数

MITENAI_PATTERN = re.compile(r"[見観み]て(?:い?ない|い?ません)")
MITAI_PATTERN = re.compile(r"[見観み]たい")

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
    "ランドセルすっごい重かったゾ～",
    "さうだよ(びんぜう)",
    "腹減ったなぁ",
    "おっ、そうだな",
)


_BINJOU_WEBHOOK_CACHE: dict[int, discord.Webhook] = {}
_BINJOU_AVATAR_BYTES: bytes | None = None
_BINJOU_AVATAR_LOADED: bool = False


def _get_binjou_avatar_bytes() -> bytes | None:
    global _BINJOU_AVATAR_BYTES, _BINJOU_AVATAR_LOADED
    if not _BINJOU_AVATAR_LOADED:
        _BINJOU_AVATAR_LOADED = True
        if BINJOU_AVATAR_PATH.is_file():
            _BINJOU_AVATAR_BYTES = BINJOU_AVATAR_PATH.read_bytes()
    return _BINJOU_AVATAR_BYTES


async def _get_or_create_binjou_webhook(
    channel: discord.TextChannel,
    bot_user_id: int,
) -> discord.Webhook:
    ch_id = getattr(channel, "id", None)
    if isinstance(ch_id, int):
        cached = _BINJOU_WEBHOOK_CACHE.get(ch_id)
        if cached is not None and getattr(cached, "token", None) is not None:
            return cached

    webhooks = await channel.webhooks()
    for wh in webhooks:
        if wh.user is not None and wh.user.id == bot_user_id and wh.name == BINJOU_WEBHOOK_NAME:
            if isinstance(ch_id, int):
                if len(_BINJOU_WEBHOOK_CACHE) >= 500:
                    _BINJOU_WEBHOOK_CACHE.clear()
                _BINJOU_WEBHOOK_CACHE[ch_id] = wh
            return wh

    avatar_bytes = _get_binjou_avatar_bytes()

    wh = await channel.create_webhook(
        name=BINJOU_WEBHOOK_NAME,
        avatar=avatar_bytes,
        reason="便乗機能用 Webhook",
    )
    if isinstance(ch_id, int):
        if len(_BINJOU_WEBHOOK_CACHE) >= 500:
            _BINJOU_WEBHOOK_CACHE.clear()
        _BINJOU_WEBHOOK_CACHE[ch_id] = wh
    return wh


def _pick_normal_binjou_text() -> str:
    """通常便乗テキストを抽選する（75% が「そうだよ/そうですよ（便乗）」各半々、25% がその他）。"""
    if len(BINJOU_MESSAGES) <= 1 or random.random() >= BINJOU_OTHER_TEXT_PROBABILITY:
        return "そうですよ（便乗）" if random.random() < 0.5 else "そうだよ（便乗）"
    others = [
        m for m in BINJOU_MESSAGES if m not in ("そうだよ（便乗）", "そうですよ（便乗）")
    ]
    return random.choice(others) if others else BINJOU_MESSAGES[0]


def _list_binjou_images() -> list[Path]:
    if not BINJOU_IMAGES_DIR.is_dir():
        return []
    return [
        p
        for p in BINJOU_IMAGES_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in BINJOU_IMAGE_SUFFIXES
    ]


def _detect_special_binjou_text(content: str) -> str | None:
    if not content:
        return None
    if MITENAI_PATTERN.search(content):
        return "嘘つけ絶対見てたぞ（ホモは嘘つき）"
    if MITAI_PATTERN.search(content):
        return "見たけりゃみせてやるよ（震え声）"
    return None


class BinjouCog(commands.Cog):
    """設定されたチャンネルで確率に応じて便乗メッセージを送信する Cog。"""

    def __init__(self, bot: YajuBot) -> None:
        self.bot = bot
        self._silenced_until: dict[int, float] = {}
        self._pending_queues: dict[int, deque[tuple[discord.TextChannel, discord.abc.Snowflake, str | None, Path | None]]] = {}
        self._active_workers: dict[int, asyncio.Task[None]] = {}

    def _is_silenced(self, guild_id: int) -> bool:
        return time.monotonic() < self._silenced_until.get(guild_id, 0.0)

    def _silence_and_clear(self, guild_id: int, retry_after: float | None) -> None:
        duration = max(BINJOU_RATELIMIT_SILENCE_SECONDS, (retry_after or 0.0) + 1.0)
        self._silenced_until[guild_id] = time.monotonic() + duration
        queue = self._pending_queues.get(guild_id)
        if queue is not None:
            queue.clear()
        logger.warning(
            "便乗送信でレート制限を検知したためキューをクリアし %.1f 秒間停止します (guild_id=%s)",
            duration,
            guild_id,
        )

    async def _send_queued_item(
        self,
        guild_id: int,
        bot_user_id: int,
        webhook_channel: discord.TextChannel,
        thread: discord.abc.Snowflake,
        content: str | None,
        image_path: Path | None,
    ) -> None:
        try:
            webhook = await _get_or_create_binjou_webhook(webhook_channel, bot_user_id)
            if image_path is not None:
                await webhook.send(
                    file=discord.File(image_path),
                    username=BINJOU_WEBHOOK_NAME,
                    thread=thread,
                )
            elif content is not None:
                await webhook.send(
                    content=content,
                    username=BINJOU_WEBHOOK_NAME,
                    thread=thread,
                )
        except Exception as exc:
            if is_ratelimited_error(exc):
                self._silence_and_clear(guild_id, extract_retry_after(exc))
            elif isinstance(exc, discord.NotFound):
                ch_id = getattr(webhook_channel, "id", None)
                if isinstance(ch_id, int):
                    _BINJOU_WEBHOOK_CACHE.pop(ch_id, None)
            elif isinstance(exc, (discord.Forbidden, discord.HTTPException)):
                logger.debug(
                    "便乗メッセージの送信に失敗しました (guild_id=%s, channel_id=%s)",
                    guild_id,
                    webhook_channel.id,
                    exc_info=True,
                )
            else:
                raise

    async def cog_unload(self) -> None:
        for task in list(self._active_workers.values()):
            if not task.done():
                task.cancel()
        self._active_workers.clear()

    async def _drain_queue(self, guild_id: int, bot_user_id: int) -> None:
        try:
            queue = self._pending_queues.get(guild_id)
            while queue and not self._is_silenced(guild_id):
                webhook_channel, thread, content, image_path = queue.popleft()
                await self._send_queued_item(
                    guild_id, bot_user_id, webhook_channel, thread, content, image_path
                )
        finally:
            if self._active_workers.get(guild_id) is asyncio.current_task():
                self._active_workers.pop(guild_id, None)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.webhook_id is not None:
            return
        if message.guild is None:
            return
        if self._is_silenced(message.guild.id):
            return

        settings = self.bot.storage.get_guild_settings(message.guild.id)
        in_binjou = message.channel.id in settings.binjou_channel_ids
        is_binjou_enabled = (
            not in_binjou if settings.binjou_channels_inverted else in_binjou
        )
        if not is_binjou_enabled:
            return
        if re.search(r"南佳也にそっくりよね", message.content or ""):
            try:
                await message.reply("⚠️全く似ていません⚠️")
            except (discord.Forbidden, discord.HTTPException):
                pass
            return
        if settings.binjou_probability <= 0.0:
            return

        special_text = _detect_special_binjou_text(message.content or "")
        if special_text is not None:
            effective_prob = min(
                100.0, settings.binjou_probability + SPECIAL_BINJOU_BONUS_PERCENT
            )
            if random.random() * 100.0 >= effective_prob:
                return
        elif random.random() * 100.0 >= settings.binjou_probability:
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

        content: str | None = None
        image_path: Path | None = None
        if special_text is not None:
            content = special_text
        else:
            images = _list_binjou_images()
            if images and random.random() < BINJOU_IMAGE_PROBABILITY:
                image_path = random.choice(images)
            else:
                content = _pick_normal_binjou_text()

        queue = self._pending_queues.setdefault(message.guild.id, deque(maxlen=5))
        queue.append((webhook_channel, thread, content, image_path))
        worker = self._active_workers.get(message.guild.id)
        if worker is None or worker.done():
            self._active_workers[message.guild.id] = asyncio.create_task(
                self._drain_queue(message.guild.id, bot_user.id),
                name=f"binjou-worker-{message.guild.id}",
            )


async def setup(bot: YajuBot) -> None:
    await bot.add_cog(BinjouCog(bot))
