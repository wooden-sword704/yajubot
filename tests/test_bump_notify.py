"""Bump/Up 通知機能（BumpNotifyCog & Storage）の単体テスト。"""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord

from yajubot.bot import YajuBot
from yajubot.cogs.bump_notify import (
    BumpNotifyCog,
    detect_bump_or_up_command,
    is_command_success_text,
)
from yajubot.config import BotConfig
from yajubot.storage import (
    DEFAULT_BUMP_DELAY_MINUTES,
    DEFAULT_DISBOARD_BOT_ID,
    DEFAULT_DISSOKU_BOT_ID,
    DEFAULT_UP_DELAY_MINUTES,
    LogCipher,
    ProxyStorage,
)


def _make_bot_message(
    *,
    message_id: int = 1001,
    guild_id: int = 111,
    channel_id: int = 222,
    author_id: int = DEFAULT_DISBOARD_BOT_ID,
    content: str = "",
    embed_title: str = "",
    embed_description: str = "",
    interaction_name: str | None = "bump",
    user_id: int = 999,
    use_metadata_only: bool = False,
) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.id = message_id
    msg.guild = MagicMock(id=guild_id)
    msg.channel = MagicMock(id=channel_id)
    msg.author = MagicMock(id=author_id)
    msg.content = content

    if embed_title or embed_description:
        embed = discord.Embed(title=embed_title or None, description=embed_description or None)
        msg.embeds = [embed]
    else:
        msg.embeds = []

    user_mock = MagicMock(id=user_id)
    if use_metadata_only:
        msg.interaction = None
        meta = MagicMock()
        meta.type = discord.InteractionType.application_command
        meta.user = user_mock
        meta.name = None
        msg.interaction_metadata = meta
    else:
        inter = MagicMock()
        inter.name = interaction_name
        inter.user = user_mock
        msg.interaction = inter
        meta = MagicMock()
        meta.type = discord.InteractionType.application_command
        meta.user = user_mock
        msg.interaction_metadata = meta
    return msg


class BumpNotifyStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmp.name)
        self.storage = ProxyStorage(
            db_path=tmp_path / "test.db",
            cipher=LogCipher(key_path=tmp_path / ".key"),
        )

    def tearDown(self) -> None:
        self.storage.close()
        self._tmp.cleanup()

    def test_default_settings_and_updates(self) -> None:
        defaults = self.storage.get_bump_notify_settings(100)
        self.assertEqual(defaults.guild_id, 100)
        self.assertIsNone(defaults.channel_id)
        self.assertTrue(defaults.bump_enabled)
        self.assertTrue(defaults.up_enabled)
        self.assertEqual(defaults.bump_delay_minutes, DEFAULT_BUMP_DELAY_MINUTES)
        self.assertEqual(defaults.up_delay_minutes, DEFAULT_UP_DELAY_MINUTES)
        self.assertEqual(defaults.bump_bot_id, DEFAULT_DISBOARD_BOT_ID)
        self.assertEqual(defaults.up_bot_id, DEFAULT_DISSOKU_BOT_ID)

        updated = self.storage.update_bump_notify_settings(
            100,
            channel_id=555,
            bump_enabled=False,
            up_delay_minutes=90,
            bump_bot_id=12345,
        )
        self.assertEqual(updated.channel_id, 555)
        self.assertFalse(updated.bump_enabled)
        self.assertTrue(updated.up_enabled)
        self.assertEqual(updated.bump_delay_minutes, 120)
        self.assertEqual(updated.up_delay_minutes, 90)
        self.assertEqual(updated.bump_bot_id, 12345)

        cleared = self.storage.update_bump_notify_settings(100, channel_id=None)
        self.assertIsNone(cleared.channel_id)

    def test_reminder_upsert_list_and_delete(self) -> None:
        r1 = self.storage.upsert_bump_reminder(100, "bump", 200, 300, 1000.0, created_at=900.0)
        self.assertEqual(r1.notify_at, 1000.0)

        # 上書き更新
        r2 = self.storage.upsert_bump_reminder(100, "bump", 201, 301, 2000.0, created_at=1900.0)
        self.assertEqual(r2.channel_id, 201)
        self.assertEqual(r2.user_id, 301)

        self.storage.upsert_bump_reminder(100, "up", 202, 302, 1500.0, created_at=1400.0)

        all_items = self.storage.list_bump_reminders(100)
        self.assertEqual([r.command_type for r in all_items], ["up", "bump"])

        self.assertTrue(self.storage.delete_bump_reminder(100, "bump"))
        self.assertFalse(self.storage.delete_bump_reminder(100, "bump"))
        self.assertIsNone(self.storage.get_bump_reminder(100, "bump"))


class BumpNotifyDetectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmp.name)
        self.storage = ProxyStorage(
            db_path=tmp_path / "test.db",
            cipher=LogCipher(key_path=tmp_path / ".key"),
        )
        self.settings = self.storage.get_bump_notify_settings(111)

    def tearDown(self) -> None:
        self.storage.close()
        self._tmp.cleanup()

    def test_detects_disboard_bump_japanese_and_english(self) -> None:
        msg_ja = _make_bot_message(
            author_id=DEFAULT_DISBOARD_BOT_ID,
            interaction_name="bump",
            embed_title="DISBOARD: Discordサーバー掲示板",
            embed_description="表示順をアップしたよ 👍\nサーバー掲示板を見てみよう！",
            user_id=777,
        )
        self.assertEqual(detect_bump_or_up_command(msg_ja, self.settings), ("bump", 777))

        msg_en = _make_bot_message(
            author_id=DEFAULT_DISBOARD_BOT_ID,
            interaction_name="bump",
            embed_description="Bump done! Check it on DISBOARD.",
            user_id=778,
            use_metadata_only=True,
        )
        self.assertEqual(detect_bump_or_up_command(msg_en, self.settings), ("bump", 778))

    def test_ignores_disboard_cooldown_message(self) -> None:
        msg_wait = _make_bot_message(
            author_id=DEFAULT_DISBOARD_BOT_ID,
            interaction_name="bump",
            embed_description="このサーバーをまだアップできません。あと45分待ってね！",
        )
        self.assertIsNone(detect_bump_or_up_command(msg_wait, self.settings))

        msg_wait_en = _make_bot_message(
            author_id=DEFAULT_DISBOARD_BOT_ID,
            interaction_name="bump",
            embed_description="Please wait another 30 minutes before you can bump.",
        )
        self.assertIsNone(detect_bump_or_up_command(msg_wait_en, self.settings))

    def test_detects_dissoku_up_and_ignores_failure(self) -> None:
        msg_up = _make_bot_message(
            author_id=DEFAULT_DISSOKU_BOT_ID,
            interaction_name="dissoku up",
            embed_description="サーバーをアップしました！",
            user_id=888,
        )
        self.assertEqual(detect_bump_or_up_command(msg_up, self.settings), ("up", 888))

        msg_up_fail = _make_bot_message(
            author_id=DEFAULT_DISSOKU_BOT_ID,
            interaction_name="up",
            embed_description="アップに失敗しました。まだアップできません。",
        )
        self.assertIsNone(detect_bump_or_up_command(msg_up_fail, self.settings))

    def test_ignores_wrong_command_or_disabled_or_no_interaction(self) -> None:
        msg_help = _make_bot_message(
            author_id=DEFAULT_DISBOARD_BOT_ID,
            interaction_name="help",
            embed_description="表示順をアップしたよ",
        )
        self.assertIsNone(detect_bump_or_up_command(msg_help, self.settings))

        msg_no_inter = _make_bot_message(
            author_id=DEFAULT_DISBOARD_BOT_ID,
            embed_description="表示順をアップしたよ",
        )
        msg_no_inter.interaction = None
        msg_no_inter.interaction_metadata = None
        self.assertIsNone(detect_bump_or_up_command(msg_no_inter, self.settings))

        disabled = self.storage.update_bump_notify_settings(111, bump_enabled=False)
        msg_ok = _make_bot_message(
            author_id=DEFAULT_DISBOARD_BOT_ID,
            interaction_name="bump",
            embed_description="表示順をアップしたよ",
        )
        self.assertIsNone(detect_bump_or_up_command(msg_ok, disabled))
        self.assertFalse(is_command_success_text("up", ""))


class BumpNotifyCogAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmp.name)
        self.storage = ProxyStorage(
            db_path=tmp_path / "test.db",
            cipher=LogCipher(key_path=tmp_path / ".key"),
        )
        self.bot = YajuBot(BotConfig(token="dummy-token"), storage=self.storage)
        self.cog = BumpNotifyCog(self.bot)

    async def asyncTearDown(self) -> None:
        await self.cog.cog_unload()
        await self.bot.close()
        self._tmp.cleanup()

    async def test_schedules_and_deduplicates_with_configured_channel_and_delay(self) -> None:
        self.storage.update_bump_notify_settings(
            111,
            channel_id=999,
            bump_delay_minutes=15,
            up_delay_minutes=30,
        )
        msg_bump = _make_bot_message(
            message_id=5001,
            guild_id=111,
            channel_id=222,
            author_id=DEFAULT_DISBOARD_BOT_ID,
            interaction_name="bump",
            embed_description="表示順をアップしたよ",
            user_id=777,
        )

        before = time.time()
        await self.cog.on_message(msg_bump)
        # 同じメッセージの edit は重複登録されない
        await self.cog.on_message_edit(msg_bump, msg_bump)

        reminder = self.storage.get_bump_reminder(111, "bump")
        self.assertIsNotNone(reminder)
        assert reminder is not None
        self.assertEqual(reminder.channel_id, 999)
        self.assertEqual(reminder.user_id, 777)
        self.assertGreaterEqual(reminder.notify_at, before + 15 * 60 - 2)
        self.assertLessEqual(reminder.notify_at, before + 15 * 60 + 5)

        # /up は設定チャンネル未指定のギルドでは実行チャンネルへ通知
        msg_up = _make_bot_message(
            message_id=5002,
            guild_id=333,
            channel_id=444,
            author_id=DEFAULT_DISSOKU_BOT_ID,
            interaction_name="up",
            embed_description="サーバーをアップしました！",
            user_id=888,
        )
        await self.cog.on_message(msg_up)
        up_reminder = self.storage.get_bump_reminder(333, "up")
        self.assertIsNotNone(up_reminder)
        assert up_reminder is not None
        self.assertEqual(up_reminder.channel_id, 444)

    async def test_restore_reminders_fires_overdue_and_cleans_up(self) -> None:
        channel = MagicMock(spec=discord.TextChannel)
        channel.send = AsyncMock()
        self.bot.get_channel = MagicMock(return_value=channel)

        self.storage.upsert_bump_reminder(
            guild_id=111,
            command_type="up",
            channel_id=222,
            user_id=777,
            notify_at=time.time() - 1.0,
        )

        count = self.cog.restore_reminders()
        self.assertEqual(count, 1)
        await asyncio.sleep(0.05)

        channel.send.assert_awaited_once()
        sent_text = channel.send.call_args.args[0]
        self.assertIn("<@777>", sent_text)
        self.assertIn("/up", sent_text)
        self.assertIsNone(self.storage.get_bump_reminder(111, "up"))

    async def test_slash_commands_require_admin_and_update_settings(self) -> None:
        non_admin = MagicMock(spec=discord.Member)
        non_admin.id = 12345
        non_admin.guild_permissions = discord.Permissions(administrator=False)
        non_admin.roles = []

        inter_denied = MagicMock(spec=discord.Interaction)
        inter_denied.guild_id = 111
        inter_denied.user = non_admin
        inter_denied.response = MagicMock()
        inter_denied.response.send_message = AsyncMock()

        await self.cog.set_timer.callback(
            self.cog,
            inter_denied,
            discord.app_commands.Choice(name="bump (DISBOARD)", value="bump"),
            90,
        )
        inter_denied.response.send_message.assert_awaited_once()
        self.assertEqual(self.storage.get_bump_notify_settings(111).bump_delay_minutes, 120)

        admin_user = MagicMock(spec=discord.Member)
        admin_user.id = 54321
        admin_user.guild_permissions = discord.Permissions(administrator=True)

        inter_ok = MagicMock(spec=discord.Interaction)
        inter_ok.guild_id = 111
        inter_ok.user = admin_user
        inter_ok.response = MagicMock()
        inter_ok.response.send_message = AsyncMock()

        await self.cog.set_timer.callback(
            self.cog,
            inter_ok,
            discord.app_commands.Choice(name="up (ディス速)", value="up"),
            45,
        )
        self.assertEqual(self.storage.get_bump_notify_settings(111).up_delay_minutes, 45)

        await self.cog.status.callback(self.cog, inter_ok)
        self.assertEqual(inter_ok.response.send_message.await_count, 2)


if __name__ == "__main__":
    unittest.main()
