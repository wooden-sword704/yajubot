"""神経衰弱コマンドからの進行中ゲーム中断テスト。"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

from yajubot.bot import YajuBot
from yajubot.cogs.shinkei import (
    ShinkeiCog,
    ShinkeiDifficultySelectView,
    ShinkeiInterruptConfirmView,
)
from yajubot.config import BotConfig


class ShinkeiInterruptFlowTest(unittest.IsolatedAsyncioTestCase):
    async def test_conflict_can_interrupt_game_and_open_difficulty_select(self) -> None:
        storage = MagicMock()
        storage.has_cleared_shinkei.return_value = False
        bot = YajuBot(BotConfig(token="dummy-token"), storage=storage)
        callback = AsyncMock()
        target_message = MagicMock(spec=discord.Message)
        callback.return_value = target_message
        try:
            bot.try_acquire_game(42, "ポーカー")
            bot.register_game_interrupt(42, callback)

            command_interaction = self._make_interaction(42, 99)
            cog = ShinkeiCog(bot)
            await ShinkeiCog.shinkei_cmd.callback(cog, command_interaction)

            command_interaction.response.send_message.assert_awaited_once()
            rejection = command_interaction.response.send_message.call_args.kwargs
            self.assertIn(
                "返金されず",
                command_interaction.response.send_message.call_args.args[0],
            )
            view = rejection["view"]
            self.assertIsInstance(view, ShinkeiInterruptConfirmView)

            confirm_interaction = self._make_interaction(42, 99)
            channel = MagicMock(spec=discord.abc.Messageable)
            channel.send = AsyncMock()
            confirm_interaction.channel = channel
            confirm_button = next(
                child for child in view.children if child.label == "退出して新しく開始"
            )
            await confirm_button.callback(confirm_interaction)

            callback.assert_awaited_once()
            self.assertEqual(bot.active_games[42], "神経衰弱")
            channel.send.assert_awaited_once()
            posted = channel.send.call_args.kwargs
            self.assertIsInstance(posted["view"], ShinkeiDifficultySelectView)
            self.assertIs(posted["reference"], target_message)
            self.assertFalse(posted["mention_author"])
            confirm_interaction.followup.send.assert_not_awaited()
            confirm_interaction.edit_original_response.assert_awaited_once_with(
                content="新しい神経衰弱の選択画面をチャンネルに投稿しました。",
                view=None,
            )
        finally:
            await bot.close()

    async def test_missing_game_message_uses_public_followup(self) -> None:
        storage = MagicMock()
        storage.has_cleared_shinkei.return_value = False
        bot = YajuBot(BotConfig(token="dummy-token"), storage=storage)
        callback = AsyncMock(return_value=None)
        try:
            bot.try_acquire_game(42, "ポーカー")
            bot.register_game_interrupt(42, callback)
            command_interaction = self._make_interaction(42, 99)
            await ShinkeiCog.shinkei_cmd.callback(ShinkeiCog(bot), command_interaction)
            view = command_interaction.response.send_message.call_args.kwargs["view"]

            confirm_interaction = self._make_interaction(42, 99)
            confirm_interaction.channel = None
            confirm_button = next(
                child for child in view.children if child.label == "退出して新しく開始"
            )
            await confirm_button.callback(confirm_interaction)

            confirm_interaction.followup.send.assert_awaited_once()
            posted = confirm_interaction.followup.send.call_args.kwargs
            self.assertFalse(posted["ephemeral"])
            self.assertIsInstance(posted["view"], ShinkeiDifficultySelectView)
        finally:
            await bot.close()

    @staticmethod
    def _make_interaction(user_id: int, guild_id: int) -> MagicMock:
        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = MagicMock()
        interaction.user.id = user_id
        interaction.guild = MagicMock()
        interaction.guild.id = guild_id
        interaction.response = MagicMock()
        interaction.response.send_message = AsyncMock()
        interaction.response.edit_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.channel = None
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()
        interaction.edit_original_response = AsyncMock()
        return interaction