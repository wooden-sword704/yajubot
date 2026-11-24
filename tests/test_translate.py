"""ガバガバ翻訳（MyMemory + Google 非公式フォールバック）と TranslateCog の単体テスト。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import discord

from yajubot.bot import YajuBot
from yajubot.cogs.translate import MAX_TRANSLATE_LENGTH, TranslateCog
from yajubot.config import BotConfig
from yajubot.translator import (
    MINOR_LANGUAGE_POOL,
    MYMEMORY_MAX_QUERY_CHARS,
    FallbackTranslator,
    GoogleUnofficialTranslator,
    MyMemoryTranslator,
    TranslationError,
    build_random_route,
    gabagaba_translate,
    preprocess_for_gabagaba,
)


def _make_mock_session(status: int, json_data: object) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock(spec=aiohttp.ClientSession)
    session.get = MagicMock(return_value=cm)
    return session


class TranslatorBackendTest(unittest.IsolatedAsyncioTestCase):
    async def test_mymemory_success(self) -> None:
        session = _make_mock_session(
            200,
            {
                "responseStatus": 200,
                "responseData": {"translatedText": "Hello world"},
                "quotaFinished": False,
            },
        )
        backend = MyMemoryTranslator(lambda: session)
        result = await backend.translate("こんにちは世界", "ja", "en")
        self.assertEqual(result, "Hello world")
        session.get.assert_called_once()

    async def test_mymemory_rejects_over_limit_without_http(self) -> None:
        session = _make_mock_session(200, {})
        backend = MyMemoryTranslator(lambda: session)
        long_text = "あ" * (MYMEMORY_MAX_QUERY_CHARS + 1)
        with self.assertRaises(TranslationError):
            await backend.translate(long_text, "ja", "en")
        session.get.assert_not_called()

    async def test_mymemory_quota_and_warning_errors(self) -> None:
        quota_session = _make_mock_session(
            200,
            {
                "responseStatus": 200,
                "responseData": {"translatedText": "x"},
                "quotaFinished": True,
            },
        )
        with self.assertRaises(TranslationError):
            await MyMemoryTranslator(lambda: quota_session).translate("test", "ja", "en")

        warn_session = _make_mock_session(
            200,
            {
                "responseStatus": 200,
                "responseData": {
                    "translatedText": "MYMEMORY WARNING: YOU USED ALL AVAILABLE FREE TRANSLATIONS"
                },
            },
        )
        with self.assertRaises(TranslationError):
            await MyMemoryTranslator(lambda: warn_session).translate("test", "ja", "en")

    async def test_google_unofficial_success_joins_segments(self) -> None:
        session = _make_mock_session(
            200,
            [
                [
                    ["こんにちは、", "Hello, ", None, None, 1],
                    ["世界。", "world.", None, None, 1],
                ],
                None,
                "en",
            ],
        )
        backend = GoogleUnofficialTranslator(lambda: session)
        result = await backend.translate("Hello, world.", "en", "ja")
        self.assertEqual(result, "こんにちは、世界。")

    async def test_google_unofficial_http_error(self) -> None:
        session = _make_mock_session(429, {})
        backend = GoogleUnofficialTranslator(lambda: session)
        with self.assertRaises(TranslationError):
            await backend.translate("test", "ja", "en")

    async def test_fallback_translator(self) -> None:
        primary = MagicMock()
        primary.translate = AsyncMock(side_effect=TranslationError("limit"))
        secondary = MagicMock()
        secondary.translate = AsyncMock(return_value="fallback-ok")

        translator = FallbackTranslator((primary, secondary))
        result = await translator.translate("原文", "ja", "en")
        self.assertEqual(result, "fallback-ok")
        primary.translate.assert_awaited_once()
        secondary.translate.assert_awaited_once()

    async def test_fallback_all_fail_raises(self) -> None:
        b1 = MagicMock()
        b1.translate = AsyncMock(side_effect=TranslationError("err1"))
        b2 = MagicMock()
        b2.translate = AsyncMock(side_effect=TranslationError("err2"))

        translator = FallbackTranslator((b1, b2))
        with self.assertRaises(TranslationError):
            await translator.translate("原文", "ja", "en")

    async def test_gabagaba_translate_follows_custom_route(self) -> None:
        calls: list[tuple[str, str, str]] = []

        class StubBackend:
            async def translate(self, text: str, source: str, target: str) -> str:
                calls.append((text, source, target))
                return f"{text}->{target}"

        result = await gabagaba_translate(
            "元文",
            StubBackend(),
            route=("ja", "th", "ru", "ja"),
            preprocess=False,
        )
        self.assertEqual(result, "元文->th->ru->ja")
        self.assertEqual(
            calls,
            [
                ("元文", "ja", "th"),
                ("元文->th", "th", "ru"),
                ("元文->th->ru", "ru", "ja"),
            ],
        )

    def test_preprocess_for_gabagaba_hiragana_and_no_punctuation(self) -> None:
        result = preprocess_for_gabagaba("今日は、いい天気ですね！テスト。")
        self.assertEqual(result, "こんにちはいいてんきですねてすと")

    def test_build_random_route(self) -> None:
        route = build_random_route(count=5)
        self.assertEqual(len(route), 7)
        self.assertEqual(route[0], "ja")
        self.assertEqual(route[-1], "ja")
        middle = route[1:-1]
        self.assertEqual(len(set(middle)), 5)
        for lang in middle:
            self.assertIn(lang, MINOR_LANGUAGE_POOL)

    async def test_gabagaba_translate_validation(self) -> None:
        backend = MagicMock()
        with self.assertRaises(TranslationError):
            await gabagaba_translate("   ", backend)
        with self.assertRaises(ValueError):
            await gabagaba_translate("テスト", backend, route=("ja",))


class TranslateCogTest(unittest.IsolatedAsyncioTestCase):
    def _make_interaction(self) -> MagicMock:
        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = SimpleNamespace(id=999)
        interaction.response = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.response.send_message = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()
        return interaction

    async def test_cog_registers_and_unregisters_context_menu(self) -> None:
        bot = YajuBot(BotConfig(token="dummy-token"))
        try:
            cog = TranslateCog(bot)
            await bot.add_cog(cog)

            ctx_cmds = bot.tree.get_commands(type=discord.AppCommandType.message)
            self.assertEqual([c.name for c in ctx_cmds], ["ガバガバ翻訳[仮復刻]"])

            slash_cmds = bot.tree.get_commands(type=discord.AppCommandType.chat_input)
            self.assertIn("translate", [c.name for c in slash_cmds])

            await bot.remove_cog("TranslateCog")
            ctx_cmds_after = bot.tree.get_commands(type=discord.AppCommandType.message)
            self.assertEqual(ctx_cmds_after, [])
        finally:
            await bot.close()

    async def test_context_menu_outputs_plain_translated_text(self) -> None:
        bot = YajuBot(BotConfig(token="dummy-token"))
        try:
            backend = MagicMock()
            backend.translate = AsyncMock(return_value="ガバガバ結果")
            cog = TranslateCog(bot, translator=backend)

            interaction = self._make_interaction()
            message = SimpleNamespace(content="こんにちは")
            await cog.gabagaba_context_menu(interaction, message)

            interaction.response.defer.assert_awaited_once()
            interaction.followup.send.assert_awaited_once()
            args = interaction.followup.send.call_args.args
            kwargs = interaction.followup.send.call_args.kwargs
            self.assertEqual(args[0], "ガバガバ結果")
            self.assertNotIn("embed", kwargs)
            self.assertIsInstance(kwargs["allowed_mentions"], discord.AllowedMentions)
            self.assertFalse(kwargs["allowed_mentions"].users)
        finally:
            await bot.close()

    async def test_slash_translate_and_error_cases(self) -> None:
        bot = YajuBot(BotConfig(token="dummy-token"))
        try:
            backend = MagicMock()
            backend.translate = AsyncMock(return_value="翻訳済み")
            cog = TranslateCog(bot, translator=backend)

            # 正常系
            ok_interaction = self._make_interaction()
            await cog.translate_slash.callback(cog, ok_interaction, "テスト")
            ok_interaction.followup.send.assert_awaited_once()
            self.assertEqual(ok_interaction.followup.send.call_args.args[0], "翻訳済み")

            # 空文字
            empty_interaction = self._make_interaction()
            await cog.translate_slash.callback(cog, empty_interaction, "   ")
            empty_interaction.response.send_message.assert_awaited_once()
            self.assertTrue(
                empty_interaction.response.send_message.call_args.kwargs.get("ephemeral")
            )

            # 文字数超過
            long_interaction = self._make_interaction()
            await cog.translate_slash.callback(
                cog, long_interaction, "あ" * (MAX_TRANSLATE_LENGTH + 1)
            )
            long_interaction.response.send_message.assert_awaited_once()
            self.assertTrue(
                long_interaction.response.send_message.call_args.kwargs.get("ephemeral")
            )

            # バックエンド全滅時
            fail_backend = MagicMock()
            fail_backend.translate = AsyncMock(side_effect=TranslationError("boom"))
            fail_cog = TranslateCog(bot, translator=fail_backend)
            fail_interaction = self._make_interaction()
            await fail_cog.translate_slash.callback(fail_cog, fail_interaction, "テスト")
            fail_interaction.followup.send.assert_awaited_once()
            self.assertTrue(
                fail_interaction.followup.send.call_args.kwargs.get("ephemeral")
            )
        finally:
            await bot.close()
