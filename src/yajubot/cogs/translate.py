"""ガバガバ翻訳（多段階逆翻訳）コマンドおよびコンテキストメニューを提供する Cog。"""

from __future__ import annotations

import logging

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from yajubot.bot import YajuBot
from yajubot.translator import (
    DEFAULT_TRANSLATION_ROUTE,
    FallbackTranslator,
    GoogleUnofficialTranslator,
    MyMemoryTranslator,
    TranslationError,
    TranslatorBackend,
    gabagaba_translate,
)

logger = logging.getLogger(__name__)

MAX_TRANSLATE_LENGTH: int = 1000
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
        self._translator: TranslatorBackend = translator or FallbackTranslator(
            (
                MyMemoryTranslator(self._get_session),
                GoogleUnofficialTranslator(self._get_session),
            )
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

    async def _execute_translate(
        self,
        interaction: discord.Interaction,
        raw_text: str,
    ) -> None:
        cleaned = raw_text.strip()
        if not cleaned:
            await interaction.response.send_message(
                "翻訳するテキストがないやん！",
                ephemeral=True,
            )
            return

        if len(cleaned) > MAX_TRANSLATE_LENGTH:
            await interaction.response.send_message(
                f"長すぎィ！（{MAX_TRANSLATE_LENGTH}文字以内の文章にしてくれよな～）",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        try:
            translated = await gabagaba_translate(
                cleaned,
                self._translator,
                route=DEFAULT_TRANSLATION_ROUTE,
            )
        except TranslationError as exc:
            logger.warning("ガバガバ翻訳に失敗しました: %s", exc)
            await interaction.followup.send(
                "翻訳に失敗しました。少し時間をおいて再度お試しください。",
                ephemeral=True,
            )
            return

        output = translated[:DISCORD_MESSAGE_MAX_LENGTH]
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
        await self._execute_translate(interaction, message.content)

    @app_commands.command(
        name="translate",
        description="入力した日本語をガバガバ逆翻訳する",
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
