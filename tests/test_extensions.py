"""拡張機能の自動検出・ロードとコマンド登録の単体テスト。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import discord
from discord import app_commands

from yajubot.bot import YajuBot, YajuCommandTree, discover_extensions
from yajubot.cogs.general import _format_uptime, _iter_slash_entries
from yajubot.config import BotConfig


class DiscoverExtensionsTest(unittest.TestCase):
    def test_discovers_builtin_cogs_and_events(self) -> None:
        pkg_root = Path(__file__).resolve().parents[1] / "src" / "yajubot"
        cogs = discover_extensions("yajubot.cogs", pkg_root / "cogs")
        events = discover_extensions("yajubot.events", pkg_root / "events")

        self.assertIn("yajubot.cogs.general", cogs)
        self.assertIn("yajubot.events.lifecycle", events)

    def test_ignores_underscore_files_and_missing_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "__init__.py").write_text("", encoding="utf-8")
            (tmp_path / "_private.py").write_text("", encoding="utf-8")
            (tmp_path / "alpha.py").write_text("", encoding="utf-8")
            (tmp_path / "beta.py").write_text("", encoding="utf-8")

            found = discover_extensions("sample.pkg", tmp_path)
            self.assertEqual(found, ["sample.pkg.alpha", "sample.pkg.beta"])
            self.assertEqual(
                discover_extensions("sample.pkg", tmp_path / "nonexistent"),
                [],
            )


class BotSetupAndGeneralHelpersTest(unittest.IsolatedAsyncioTestCase):
    async def test_interrupt_game_runs_registered_callback_once(self) -> None:
        bot = YajuBot(BotConfig(token="dummy-token"))
        callback = AsyncMock()
        callback.return_value = None
        try:
            self.assertIsNone(bot.try_acquire_game(123, "神経衰弱"))
            bot.register_game_interrupt(123, callback)

            interrupted, message = await bot.interrupt_game(123)
            self.assertTrue(interrupted)
            self.assertIsNone(message)
            interrupted, message = await bot.interrupt_game(123)
            self.assertFalse(interrupted)
            self.assertIsNone(message)
            callback.assert_awaited_once()
            self.assertNotIn(123, bot.active_games)
        finally:
            await bot.close()

    async def test_setup_hook_loads_extensions_and_syncs_commands(self) -> None:
        config = BotConfig(token="dummy-token", guild_id=123456, log_level="INFO")
        bot = YajuBot(config)
        try:
            with patch.object(bot, "sync_commands", new=AsyncMock(return_value=[])) as mock_sync:
                await bot.setup_hook()

            mock_sync.assert_awaited_once()
            self.assertIn("GeneralCog", bot.cogs)
            self.assertIn("ProxyCog", bot.cogs)
            self.assertIn("AdminCog", bot.cogs)
            self.assertIn("OmikujiCog", bot.cogs)
            self.assertIn("LifecycleEvents", bot.cogs)

            cmd_names = {
                cmd.name
                for cmd in bot.tree.get_commands(type=discord.AppCommandType.chat_input)
            }
            self.assertEqual(
                cmd_names,
                {"ping", "info", "help", "proxy", "say", "admin", "omikuji", "omikuji_reset"},
            )
        finally:
            await bot.close()

    async def test_tree_error_handler_sends_ephemeral_message(self) -> None:
        config = BotConfig(token="dummy-token")
        bot = YajuBot(config)
        try:
            tree = bot.tree
            self.assertIsInstance(tree, YajuCommandTree)

            interaction = MagicMock(spec=discord.Interaction)
            interaction.command = None
            interaction.response = MagicMock()
            interaction.response.is_done.return_value = False
            interaction.response.send_message = AsyncMock()

            await tree.on_error(interaction, app_commands.MissingPermissions(["manage_guild"]))
            interaction.response.send_message.assert_awaited_once()
            kwargs = interaction.response.send_message.call_args.kwargs
            self.assertTrue(kwargs.get("ephemeral"))
        finally:
            await bot.close()

    def test_format_uptime(self) -> None:
        start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        now = start + timedelta(days=1, hours=2, minutes=3, seconds=4)
        self.assertEqual(_format_uptime(start, now), "1日 2時間 3分 4秒")
        self.assertEqual(_format_uptime(start, start), "0秒")

    def test_iter_slash_entries_flattens_nested_groups(self) -> None:
        group = app_commands.Group(name="admin", description="管理コマンド")
        sub_group = app_commands.Group(name="role", description="ロール管理", parent=group)

        @app_commands.command(name="add", description="ロールを付与")
        async def role_add(interaction: discord.Interaction) -> None:
            pass

        sub_group.add_command(role_add)

        @app_commands.command(name="ping", description="疎通確認")
        async def ping(interaction: discord.Interaction) -> None:
            pass

        entries = _iter_slash_entries([group, ping])
        self.assertEqual(
            entries,
            [
                ("/admin role add", "ロールを付与"),
                ("/ping", "疎通確認"),
            ],
        )

    async def test_general_commands_respond_with_embeds(self) -> None:
        config = BotConfig(token="dummy-token", guild_id=None, log_level="INFO")
        bot = YajuBot(config)
        try:
            await bot.load_extension("yajubot.cogs.general")
            cog = bot.get_cog("GeneralCog")
            self.assertIsNotNone(cog)
            assert cog is not None

            for method, expect_ephemeral in [
                (cog.ping.callback, False),  # type: ignore[attr-defined]
                (cog.info.callback, False),  # type: ignore[attr-defined]
                (cog.help_command.callback, True),  # type: ignore[attr-defined]
            ]:
                interaction = self._make_interaction()
                with patch.object(
                    type(bot), "latency", new_callable=PropertyMock, return_value=0.042
                ):
                    await method(cog, interaction)
                interaction.response.send_message.assert_awaited_once()
                kwargs = interaction.response.send_message.call_args.kwargs
                self.assertIsInstance(kwargs.get("embed"), discord.Embed)
                self.assertEqual(kwargs.get("ephemeral", False), expect_ephemeral)
        finally:
            await bot.close()

    async def test_sync_commands_selects_guild_or_global(self) -> None:
        guild_bot = YajuBot(BotConfig(token="dummy", guild_id=987654321))
        global_bot = YajuBot(BotConfig(token="dummy", guild_id=None))
        try:
            with (
                patch.object(guild_bot.tree, "copy_global_to") as mock_copy,
                patch.object(
                    guild_bot.tree, "sync", new=AsyncMock(return_value=["c1"])
                ) as mock_sync,
            ):
                synced = await guild_bot.sync_commands()
                self.assertEqual(synced, ["c1"])
                mock_copy.assert_called_once()
                mock_sync.assert_awaited_once()
                self.assertEqual(mock_sync.call_args.kwargs["guild"].id, 987654321)

            with (
                patch.object(global_bot.tree, "copy_global_to") as mock_copy,
                patch.object(
                    global_bot.tree, "sync", new=AsyncMock(return_value=["c1", "c2"])
                ) as mock_sync,
            ):
                synced = await global_bot.sync_commands()
                self.assertEqual(synced, ["c1", "c2"])
                mock_copy.assert_not_called()
                mock_sync.assert_awaited_once_with()
        finally:
            await guild_bot.close()
            await global_bot.close()

    @staticmethod
    def _make_interaction() -> MagicMock:
        interaction = MagicMock(spec=discord.Interaction)
        interaction.guild = None
        interaction.response = MagicMock()
        interaction.response.is_done.return_value = False
        interaction.response.send_message = AsyncMock()
        return interaction


if __name__ == "__main__":
    unittest.main()
