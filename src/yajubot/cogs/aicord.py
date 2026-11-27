"""AICord API を利用したチャンネル別キャラクター対話 Cog。"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from yajubot.bot import YajuBot

logger = logging.getLogger(__name__)

AICORD_CHAT_URL = "https://aicordapp.com/api/v1/chat"
AICORD_ACCOUNT_URL = "https://aicordapp.com/api/v1/account"

MAX_HISTORY_TURNS = 20
DISCORD_MESSAGE_MAX_LENGTH = 2000

AICORD_CHARACTERS: dict[str, tuple[str, str]] = {
    "dokachan": ("df932593-d15d-4d2c-8dc8-9f455fcebb03", "AIどかちゃん"),
    "takuya": ("e8df0cf2-8387-4d22-8f9d-b311afd49156", "AI拓也"),
}


def _split_discord_message(text: str, limit: int = DISCORD_MESSAGE_MAX_LENGTH) -> list[str]:
    if not text:
        return ["（応答が空だったゾ）"]
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    return chunks


class AICordTokenModal(discord.ui.Modal, title="AICord API トークン登録"):
    """ユーザー自身の AICord API トークンを登録するモーダル。"""

    token_input: discord.ui.TextInput = discord.ui.TextInput(
        label="AICord API トークン",
        placeholder="ここに自分の AICord API トークンを貼り付け",
        style=discord.TextStyle.short,
        required=True,
        max_length=512,
    )

    def __init__(self, cog: AICordCog) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw_token = self.token_input.value.strip()
        if raw_token.lower().startswith("bearer "):
            raw_token = raw_token[7:].strip()
        if not raw_token:
            await interaction.response.send_message(
                "トークンが空だゾ。", ephemeral=True
            )
            return

        self.cog.bot.storage.set_aicord_user_token(interaction.user.id, raw_token)
        await interaction.response.send_message(
            "AICord API トークンを暗号化して保存したゾ！これで対話チャンネルで会話できるゾ。",
            ephemeral=True,
        )


class AICordCog(
    commands.GroupCog,
    group_name="aicord",
    group_description="AICord キャラクター対話の設定・トークン登録",
):
    """AICord のユーザー別トークン登録とキャラクター対話を提供する Cog。"""

    def __init__(self, bot: YajuBot) -> None:
        self.bot = bot
        self._http_session: aiohttp.ClientSession | None = None
        # (guild_id, channel_id, user_id) -> deque[{"role": ..., "content": ...}]
        self._histories: dict[tuple[int, int, int], deque[dict[str, str]]] = {}
        # user_id -> {"requests": int, "cores_used": float, "total_tokens": int}
        self._usage_stats: dict[int, dict[str, float | int]] = {}

    async def cog_load(self) -> None:
        self._http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60)
        )

    async def cog_unload(self) -> None:
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None

    @app_commands.command(
        name="login",
        description="自分の AICord API トークンを登録するゾ（クレジットは本人消費）",
    )
    async def login_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(AICordTokenModal(self))

    @app_commands.command(
        name="logout",
        description="保存されている AICord API トークンを削除するゾ",
    )
    async def logout_cmd(self, interaction: discord.Interaction) -> None:
        deleted = self.bot.storage.delete_aicord_user_token(interaction.user.id)
        if deleted:
            await interaction.response.send_message(
                "AICord のトークンを削除したゾ。", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "まだ AICord トークンが登録されていないゾ。", ephemeral=True
            )

    @app_commands.command(
        name="set",
        description="指定チャンネルで対話する AICord キャラクターを設定するゾ",
    )
    @app_commands.describe(
        channel="対話を有効にするテキストチャンネル",
        character="使用するキャラクター",
    )
    @app_commands.choices(
        character=[
            app_commands.Choice(name="AIどかちゃん", value="dokachan"),
            app_commands.Choice(name="AI拓也", value="takuya"),
        ]
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def set_channel_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        character: app_commands.Choice[str],
    ) -> None:
        if interaction.guild is None:
            return
        char_id, char_name = AICORD_CHARACTERS[character.value]
        self.bot.storage.set_aicord_channel(
            interaction.guild.id, channel.id, char_id, char_name
        )
        await interaction.response.send_message(
            f"{channel.mention} の対話キャラクターを **{char_name}** に設定したゾ。",
            ephemeral=True,
        )

    @app_commands.command(
        name="remove",
        description="指定チャンネルの AICord 対話設定を解除するゾ",
    )
    @app_commands.describe(channel="設定を解除するテキストチャンネル")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def remove_channel_cmd(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        if interaction.guild is None:
            return
        removed = self.bot.storage.remove_aicord_channel(
            interaction.guild.id, channel.id
        )
        if removed:
            await interaction.response.send_message(
                f"{channel.mention} の AICord 対話設定を解除したゾ。", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"{channel.mention} には対話設定が登録されていないゾ。", ephemeral=True
            )

    @app_commands.command(
        name="status",
        description="このサーバーの AICord 対話設定一覧を表示するゾ",
    )
    @app_commands.guild_only()
    async def status_cmd(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        rows = self.bot.storage.list_aicord_channels(interaction.guild.id)
        if not rows:
            await interaction.response.send_message(
                "このサーバーには AICord 対話チャンネルが設定されていないゾ。",
                ephemeral=True,
            )
            return
        lines = [
            f"<#{ch_id}> → **{char_name}** (`{char_id}`)"
            for ch_id, char_id, char_name in rows
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(
        name="usage",
        description="自分の AICord クレジット残高・使用状況を確認するゾ",
    )
    async def usage_cmd(self, interaction: discord.Interaction) -> None:
        token = self.bot.storage.get_aicord_user_token(interaction.user.id)
        if not token:
            await interaction.response.send_message(
                "AICord トークンが未登録だゾ！先に `/aicord login` で登録してくれよな～",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        account_lines: list[str] = []
        if self._http_session is not None:
            try:
                async with self._http_session.get(
                    AICORD_ACCOUNT_URL,
                    headers={"Authorization": f"Bearer {token}"},
                ) as resp:
                    if resp.status == 200:
                        acc: dict[str, Any] = await resp.json()
                        username = acc.get("username") or acc.get("name")
                        if username:
                            account_lines.append(f"アカウント: **{username}**")
                        for key in ("cores", "credits", "balance", "remaining_cores"):
                            if key in acc and acc[key] is not None:
                                account_lines.append(f"残高 ({key}): **{acc[key]}**")
                    elif resp.status == 401:
                        await interaction.followup.send(
                            "AICord のトークンが無効または期限切れだゾ。`/aicord login` で再登録してくれよな～",
                            ephemeral=True,
                        )
                        return
            except Exception as exc:
                logger.debug("AICord アカウント情報取得をスキップしました: %s", exc)

        stats = self._usage_stats.get(interaction.user.id)
        if stats:
            account_lines.append(
                f"この Bot 経由の利用: **{int(stats['requests'])} 回** / "
                f"消費 Cores 累計: **{stats['cores_used']:.4g}** / "
                f"トークン累計: **{int(stats['total_tokens']):,}**"
            )
        else:
            account_lines.append("この Bot 経由の利用履歴はまだないゾ。")

        await interaction.followup.send("\n".join(account_lines), ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if not isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            return

        config = self.bot.storage.get_aicord_channel(
            message.guild.id, message.channel.id
        )
        if config is None:
            return
        character_id, _character_name = config

        content = message.content.strip()
        if not content:
            return

        history_key = (message.guild.id, message.channel.id, message.author.id)
        if content.lower() == "new":
            self._histories.pop(history_key, None)
            await message.reply(
                f"{message.author.mention} の会話履歴が消されたゾ",
                mention_author=False,
            )
            return

        token = self.bot.storage.get_aicord_user_token(message.author.id)
        if not token:
            await message.reply(
                "AICord トークンが未登録だゾ！先に `/aicord login` で登録してくれよな～",
                mention_author=False,
            )
            return

        if self._http_session is None:
            return

        history = self._histories.setdefault(
            history_key, deque(maxlen=MAX_HISTORY_TURNS * 2)
        )
        payload_messages: list[dict[str, str]] = list(history)

        # 返信がある場合は返信先の1メッセージのみを一時コンテキストとして読み込む（永続化しない）
        if message.reference and message.reference.message_id:
            ref_msg = message.reference.resolved
            if not isinstance(ref_msg, discord.Message):
                try:
                    ref_msg = await message.channel.fetch_message(
                        message.reference.message_id
                    )
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    ref_msg = None
            if ref_msg is not None and ref_msg.content.strip():
                ref_author = ref_msg.author.display_name
                payload_messages.append(
                    {
                        "role": "user",
                        "content": f"[返信先 {ref_author} の発言]: {ref_msg.content.strip()}",
                    }
                )

        payload_messages.append({"role": "user", "content": content})

        async with message.channel.typing():
            try:
                async with self._http_session.post(
                    AICORD_CHAT_URL,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json={"character": character_id, "messages": payload_messages},
                ) as resp:
                    if resp.status == 401:
                        await message.reply(
                            "AICord のトークンが無効だゾ。`/aicord login` で再登録してくれよな～",
                            mention_author=False,
                        )
                        return
                    if resp.status == 402:
                        await message.reply(
                            "AICord のクレジット（Cores）が不足しているゾ！",
                            mention_author=False,
                        )
                        return
                    if resp.status >= 400:
                        logger.warning("AICord Chat API エラー (%s)", resp.status)
                        await message.reply(
                            f"AICord API エラーが発生したゾ (HTTP {resp.status})。",
                            mention_author=False,
                        )
                        return
                    data: dict[str, Any] = await resp.json()
            except Exception as exc:
                logger.warning("AICord Chat API 通信失敗: %s", exc)
                await message.reply(
                    "AICord との通信に失敗したゾ。少し待ってから試してくれよな～",
                    mention_author=False,
                )
                return

        reply_text = str(data.get("text") or "").strip()
        if not reply_text:
            reply_text = "（応答が空だったゾ）"

        usage = data.get("usage")
        stats = self._usage_stats.setdefault(
            message.author.id, {"requests": 0, "cores_used": 0.0, "total_tokens": 0}
        )
        stats["requests"] = int(stats["requests"]) + 1
        if isinstance(usage, dict):
            cores = usage.get("cores") or usage.get("cost") or 0
            tokens = usage.get("total_tokens") or (
                int(usage.get("prompt_tokens", 0) or 0)
                + int(usage.get("completion_tokens", 0) or 0)
            )
            try:
                stats["cores_used"] = float(stats["cores_used"]) + float(cores)
            except (TypeError, ValueError):
                pass
            try:
                stats["total_tokens"] = int(stats["total_tokens"]) + int(tokens)
            except (TypeError, ValueError):
                pass

        # 通常会話のみ永続履歴に保存（一時的な返信先コンテキストは保存しない）
        history.append({"role": "user", "content": content})
        history.append({"role": "assistant", "content": reply_text})

        for chunk in _split_discord_message(reply_text):
            await message.reply(chunk, mention_author=False)


async def setup(bot: YajuBot) -> None:
    await bot.add_cog(AICordCog(bot))
