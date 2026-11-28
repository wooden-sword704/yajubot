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
            confirm_button = next(
                child for child in view.children if child.label == "退出して新しく開始"
            )
            await confirm_button.callback(confirm_interaction)

            callback.assert_awaited_once()
            self.assertEqual(bot.active_games[42], "神経衰弱")
            edited = confirm_interaction.response.edit_message.call_args.kwargs
            self.assertIsInstance(edited["view"], ShinkeiDifficultySelectView)
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
        return interaction