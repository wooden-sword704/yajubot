"""ガバガバ翻訳（多段階逆翻訳）コマンドおよびコンテキストメニューを提供する Cog。"""

from __future__ import annotations

import logging

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from yajubot.bot import YajuBot
from yajubot.cogs.binjou import _get_or_create_binjou_webhook
from yajubot.cogs.proxy import (
    REPLY1_EMOJI,
    REPLY2_EMOJI,
    strip_reply_header,
)
from yajubot.translator import (
    TranslationError,
    TranslatorBackend,
    build_default_translator,
    gabagaba_translate,
)

logger = logging.getLogger(__name__)

MAX_TRANSLATE_LENGTH: int = 2000
DISCORD_MESSAGE_MAX_LENGTH: int = 2000


class TranslateCog(commands.Cog):
    """既存メッセージまたは入力テキストをガバガバ翻訳（逆翻訳）する Cog。"""

    def __init__(
        self,
        bot: YajuBot,
        *,
        translator: TranslatorBackend | None = None,
    ) -> None:
        self.bot = bot
        self._session: aiohttp.ClientSession | None = None
        self._translator: TranslatorBackend = translator or build_default_translator(
            self._get_session
        )
        self._context_menu = app_commands.ContextMenu(
            name="ガバガバ翻訳[仮復刻]",
            callback=self.gabagaba_context_menu,
        )
        # ContextMenu デコレータ相当のクールダウンを付与（ユーザーごとに10秒）
        app_commands.checks.cooldown(1, 10.0, key=lambda i: i.user.id)(
            self._context_menu
        )

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self._context_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(
            self._context_menu.name, type=self._context_menu.type
        )
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    @staticmethod
    def _build_reply_header(
        raw_text: str,
        executor: discord.abc.User,
        source_message: discord.Message | None = None,
    ) -> str:
        executor_mention = getattr(
            executor, "mention", f"@{getattr(executor, 'display_name', 'user')}"
        )
        if source_message is not None:
            has_attachments = bool(getattr(source_message, "attachments", None))
            if has_attachments:
                content_text = "添付ファイル"
            else:
                single_line = " ".join(strip_reply_header(raw_text).split())
                if single_line:
                    content_text = (
                        single_line[:32] + "..."
                        if len(single_line) > 35
                        else single_line
                    )
                elif getattr(source_message, "embeds", None):
                    content_text = "埋め込み"
                else:
                    content_text = "メッセージ"
            content_text = content_text.replace("[", r"\[").replace("]", r"\]")
            return f"{REPLY1_EMOJI}{REPLY2_EMOJI}{executor_mention} :[{content_text}]({source_message.jump_url})"

        single_line = " ".join(raw_text.split())
        snippet = (
            single_line[:32] + "..." if len(single_line) > 35 else single_line
        )
        return f"{REPLY1_EMOJI}{REPLY2_EMOJI}{executor_mention} :{snippet}"

    async def _execute_translate(
        self,
        interaction: discord.Interaction,
        raw_text: str,
        *,
        source_message: discord.Message | None = None,
    ) -> None:
        cleaned = strip_reply_header(raw_text).strip()
        if not cleaned:
            await interaction.response.send_message(
                "翻訳するテキストがないやん！",
                ephemeral=True,
            )
            return

        if len(cleaned) > MAX_TRANSLATE_LENGTH:
            await interaction.response.send_message(
                f"長スギィ！（{MAX_TRANSLATE_LENGTH}文字以内の文章にしてくれよな～）",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        try:
            translated = await gabagaba_translate(
                cleaned,
                self._translator,
            )
        except TranslationError as exc:
            logger.warning("ガバガバ翻訳に失敗しました: %s", exc)
            await interaction.followup.send(
                "翻訳に失敗しちゃっ...たぁ！ちょ、ちょ、ちょっと待って下さい！待って！助けて！待って下さい！お願いします！ｱｱｱｱｱｱｱｱ！",
                ephemeral=True,
            )
            return

        header = self._build_reply_header(
            cleaned, interaction.user, source_message=source_message
        )

        output = f"{header}\n{translated}"[:DISCORD_MESSAGE_MAX_LENGTH]
        bot_user = self.bot.user
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

        if bot_user is not None and webhook_channel is not None:
            try:
                webhook = await _get_or_create_binjou_webhook(
                    webhook_channel, bot_user.id
                )
                await webhook.send(
                    content=output,
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
                    "ガバガバ翻訳の Webhook 送信に失敗したため通常応答へフォールバックします: %s",
                    exc,
                )

        await interaction.followup.send(
            output,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def gabagaba_context_menu(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> None:
        """既存メッセージを右クリック→アプリからガバガバ翻訳する。"""
        await self._execute_translate(
            interaction, message.content, source_message=message
        )

    @app_commands.command(
        name="translate",
        description="ガバガバ翻訳をする",
    )
    @app_commands.describe(text="ガバガバ翻訳したい日本語テキスト")
    @app_commands.checks.cooldown(1, 10.0, key=lambda i: i.user.id)
    async def translate_slash(
        self,
        interaction: discord.Interaction,
        text: str,
    ) -> None:
        """スラッシュコマンドからテキスト入力でガバガバ翻訳する。"""
        await self._execute_translate(interaction, text)


async def setup(bot: YajuBot) -> None:
    await bot.add_cog(TranslateCog(bot))
