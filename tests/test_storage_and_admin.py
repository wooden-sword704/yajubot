"""暗号化ストレージ・権限モデル・偽装防止・/admin コマンドの単体テスト。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from yajubot.bot import YajuBot
from yajubot.cogs.admin import AdminCog, AdminPanelView, DeletedLogsView, parse_message_id
from yajubot.cogs.proxy import (
    ProxyCog,
    ProxyError,
    can_manage_bot,
    can_use_proxy,
    encode_proxy_author_id,
    extract_proxy_author_id,
    strip_invisible_meta,
)
from yajubot.config import BotConfig
from yajubot.storage import LogCipher, OWNER_USER_ID, ProxyStorage


class StorageAndSecurityTest(unittest.IsolatedAsyncioTestCase):
    def test_cipher_encrypts_and_db_stores_no_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            key_path = Path(tmpdir) / ".key"
            cipher = LogCipher(key_path=key_path)
            storage = ProxyStorage(db_path, cipher=cipher)
            try:
                secret_text = "秘密のメッセージ本文だゾ"
                storage.record_proxy_message(
                    proxy_msg_id=10001,
                    guild_id=1,
                    channel_id=2,
                    author_id=42,
                    content=secret_text,
                    reply_target_id=999,
                )

                # 生の SQLite テーブルには平文が存在しないこと
                conn = sqlite3.connect(db_path)
                raw_blob = conn.execute(
                    "SELECT content_enc FROM proxy_messages WHERE proxy_msg_id = 10001"
                ).fetchone()[0]
                conn.close()
                self.assertNotIn(secret_text.encode("utf-8"), raw_blob)

                # ストレージ経由で復号できること
                rec = storage.get_proxy_message(10001)
                self.assertIsNotNone(rec)
                assert rec is not None
                self.assertEqual(rec.content, secret_text)
                self.assertEqual(rec.author_id, 42)
                self.assertFalse(rec.is_deleted)

                # 削除マークの記録と削除ログ抽出
                marked = storage.mark_proxy_message_deleted(10001)
                self.assertTrue(marked)
                deleted_logs = storage.get_deleted_logs(1)
                self.assertEqual(len(deleted_logs), 1)
                self.assertTrue(deleted_logs[0].is_deleted)
                self.assertEqual(deleted_logs[0].content, secret_text)
            finally:
                storage.close()

    def test_max_records_limit_evicts_oldest(self) -> None:
        storage = ProxyStorage(":memory:", max_records=3)
        try:
            for i in range(1, 6):
                storage.record_proxy_message(
                    proxy_msg_id=i,
                    guild_id=1,
                    channel_id=2,
                    author_id=10,
                    content=f"msg-{i}",
                )
            self.assertIsNone(storage.get_proxy_message(1))
            self.assertIsNone(storage.get_proxy_message(2))
            self.assertIsNotNone(storage.get_proxy_message(3))
            self.assertIsNotNone(storage.get_proxy_message(5))
        finally:
            storage.close()

    def test_role_permissions_and_owner_exception(self) -> None:
        storage = ProxyStorage(":memory:")
        try:
            guild_id = 100
            owner = SimpleNamespace(id=OWNER_USER_ID, roles=[], guild_permissions=discord.Permissions.none())
            admin_perm = SimpleNamespace(id=2, roles=[], guild_permissions=discord.Permissions(administrator=True))
            normal_user = SimpleNamespace(id=3, roles=[SimpleNamespace(id=50)], guild_permissions=discord.Permissions.none())
            role_user = SimpleNamespace(id=4, roles=[SimpleNamespace(id=60)], guild_permissions=discord.Permissions.none())
            admin_role_user = SimpleNamespace(id=5, roles=[SimpleNamespace(id=70)], guild_permissions=discord.Permissions.none())

            # 未設定時: 例外アカウント・サーバー管理者のみ許可、一般ユーザーは拒否
            self.assertTrue(can_manage_bot(owner, storage, guild_id))
            self.assertTrue(can_manage_bot(admin_perm, storage, guild_id))
            self.assertFalse(can_manage_bot(normal_user, storage, guild_id))
            self.assertFalse(can_use_proxy(normal_user, storage, guild_id))
            self.assertTrue(can_use_proxy(owner, storage, guild_id))

            # ロール設定後
            storage.set_proxy_roles(guild_id, [60])
            storage.set_admin_roles(guild_id, [70])

            self.assertFalse(can_use_proxy(normal_user, storage, guild_id))
            self.assertTrue(can_use_proxy(role_user, storage, guild_id))
            self.assertFalse(can_manage_bot(role_user, storage, guild_id))
            self.assertTrue(can_manage_bot(admin_role_user, storage, guild_id))
            self.assertTrue(can_use_proxy(admin_role_user, storage, guild_id))
            self.assertTrue(can_manage_bot(owner, storage, guild_id))
        finally:
            storage.close()

    def test_spoofing_prevention_db_source_of_truth_and_sanitization(self) -> None:
        storage = ProxyStorage(":memory:")
        try:
            storage.record_proxy_message(
                proxy_msg_id=5000,
                guild_id=1,
                channel_id=2,
                author_id=42,
                content="正規メッセージ",
            )
            # 本文中に他人の偽装ゼロ幅IDが含まれていても DB の ID (42) が優先される
            fake_meta = encode_proxy_author_id(999999)
            msg = MagicMock(spec=discord.Message)
            msg.id = 5000
            msg.webhook_id = 123
            msg.content = f"{fake_meta}本文"
            self.assertEqual(extract_proxy_author_id(msg, storage=storage), 42)

            # 通常メッセージ（webhook_id=None）にゼロ幅文字が仕込まれてもプロキシ扱いされない
            normal_msg = MagicMock(spec=discord.Message)
            normal_msg.id = 5001
            normal_msg.webhook_id = None
            normal_msg.content = f"悪意ある入力{fake_meta}"
            normal_msg.components = []
            self.assertIsNone(extract_proxy_author_id(normal_msg, storage=storage))

            # 入力サニタイズでゼロ幅ブロックが除去される
            self.assertEqual(strip_invisible_meta(f"テスト{fake_meta}本文"), "テスト本文")
        finally:
            storage.close()

    async def test_cooldown_and_length_limit(self) -> None:
        bot = YajuBot(BotConfig(token="dummy-token"), storage=ProxyStorage(":memory:"))
        try:
            cog = ProxyCog(bot)
            bot_user = SimpleNamespace(id=100)
            bot.storage.set_proxy_roles(1, [10])

            channel = MagicMock(spec=discord.TextChannel)
            channel.id = 555
            guild = MagicMock(spec=discord.Guild)
            guild.id = 1
            guild.emojis = []

            user = SimpleNamespace(
                id=42,
                roles=[SimpleNamespace(id=10)],
                display_name="テスト",
                display_avatar=SimpleNamespace(url="https://example.com/a.png"),
            )
            interaction = MagicMock(spec=discord.Interaction)
            interaction.channel = channel
            interaction.guild = guild
            interaction.user = user

            webhook = MagicMock(spec=discord.Webhook)
            webhook.send = AsyncMock(return_value=SimpleNamespace(id=8001))

            with (
                patch.object(type(bot), "user", new=bot_user),
                patch.dict(
                    ProxyCog.__init__.__globals__,
                    {"get_or_create_proxy_webhook": AsyncMock(return_value=webhook)},
                ),
            ):
                # 1回目成功
                await cog.send_proxy_message(
                    interaction=interaction, content="1回目", clicked_launcher=None, repost_launcher=False
                )
                # 2秒以内の2回目はクールダウンで拒否
                with self.assertRaises(ProxyError) as ctx:
                    await cog.send_proxy_message(
                        interaction=interaction, content="2回目", clicked_launcher=None, repost_launcher=False
                    )
                self.assertIn("連打しすぎだゾ", str(ctx.exception))

                # 2000文字超過の事前ブロック
                cog._user_cooldowns.clear()
                with self.assertRaises(ProxyError) as ctx_len:
                    await cog.send_proxy_message(
                        interaction=interaction, content="あ" * 1995, clicked_launcher=None, repost_launcher=False
                    )
                self.assertIn("文字数制限", str(ctx_len.exception))
        finally:
            await bot.close()

    async def test_admin_lookup_and_deleted_logs(self) -> None:
        self.assertEqual(parse_message_id("123456789"), 123456789)
        self.assertEqual(
            parse_message_id("https://discord.com/channels/1/2/987654321"), 987654321
        )
        self.assertIsNone(parse_message_id("not-an-id"))

        bot = YajuBot(BotConfig(token="dummy-token"), storage=ProxyStorage(":memory:"))
        try:
            admin_cog = AdminCog(bot)
            bot.storage.record_proxy_message(
                proxy_msg_id=3001,
                guild_id=1,
                channel_id=2,
                author_id=42,
                content="秘密の発言",
            )
            # 編集履歴を記録
            old_text, edited_at = bot.storage.update_proxy_message_content(3001, "編集後の秘密の発言")
            self.assertEqual(old_text, "秘密の発言")
            self.assertTrue(edited_at.endswith("JST"))
            bot.storage.mark_proxy_message_deleted(3001)

            guild = MagicMock(spec=discord.Guild)
            guild.id = 1

            # 非権限ユーザーは拒否
            non_admin = SimpleNamespace(id=999, roles=[], guild_permissions=discord.Permissions.none())
            bad_interaction = MagicMock(spec=discord.Interaction)
            bad_interaction.guild = guild
            bad_interaction.user = non_admin
            bad_interaction.response = MagicMock()
            bad_interaction.response.send_message = AsyncMock()

            await admin_cog.admin_lookup.callback(admin_cog, bad_interaction, "3001")
            bad_interaction.response.send_message.assert_awaited_once()
            self.assertIn("管理者または許可された管理ロールのみ", bad_interaction.response.send_message.call_args.args[0])

            # 例外アカウントは許可され、復号結果と編集履歴を確認できる
            owner_interaction = MagicMock(spec=discord.Interaction)
            owner_interaction.guild = guild
            owner_interaction.user = SimpleNamespace(id=OWNER_USER_ID, roles=[], guild_permissions=discord.Permissions.none())
            owner_interaction.response = MagicMock()
            owner_interaction.response.send_message = AsyncMock()

            await admin_cog.admin_lookup.callback(admin_cog, owner_interaction, "3001")
            owner_interaction.response.send_message.assert_awaited_once()
            embed = owner_interaction.response.send_message.call_args.kwargs["embed"]
            self.assertTrue(any("編集後の秘密の発言" in f.value for f in embed.fields))
            self.assertTrue(any("編集履歴" in f.name and "秘密の発言" in f.value for f in embed.fields))
            self.assertTrue(any("JST" in f.value for f in embed.fields))

            # 削除ログ閲覧
            owner_interaction.response.send_message.reset_mock()
            await admin_cog.admin_deleted_logs.callback(admin_cog, owner_interaction)
            owner_interaction.response.send_message.assert_awaited_once()
            logs_embed = owner_interaction.response.send_message.call_args.kwargs["embed"]
            self.assertIsInstance(owner_interaction.response.send_message.call_args.kwargs["view"], DeletedLogsView)
            self.assertTrue(any("編集後の秘密の発言" in f.value for f in logs_embed.fields))

            # ロール・ログチャンネル設定パネル（3つの Select + 2つの Button = 5要素）
            panel_view = AdminPanelView(bot, 1)
            self.assertEqual(len(panel_view.children), 5)

            # /admin log_channel でログチャンネルを設定し、削除イベントで自動通知されることを検証
            log_channel = MagicMock(spec=discord.TextChannel)
            log_channel.id = 7777
            log_channel.send = AsyncMock()

            owner_interaction.response.send_message.reset_mock()
            await admin_cog.admin_log_channel.callback(admin_cog, owner_interaction, log_channel)
            self.assertEqual(bot.storage.get_guild_settings(1).log_channel_id, 7777)

            bot.storage.record_proxy_message(
                proxy_msg_id=4001,
                guild_id=1,
                channel_id=2,
                author_id=42,
                content="消されるメッセージ",
            )
            proxy_cog = ProxyCog(bot)
            with patch.object(type(bot), "get_channel", return_value=log_channel):
                payload = discord.RawMessageDeleteEvent(
                    {"id": 4001, "channel_id": 2, "guild_id": 1}
                )
                await proxy_cog.on_raw_message_delete(payload)
                log_channel.send.assert_awaited_once()
                sent_embed = log_channel.send.call_args.kwargs["embed"]
                self.assertIn("削除ログ", sent_embed.title)
                self.assertTrue(any("消されるメッセージ" in f.value for f in sent_embed.fields))

                # 同じメッセージの2回目削除イベントでは二重送信されない
                log_channel.send.reset_mock()
                await proxy_cog.on_raw_message_delete(payload)
                log_channel.send.assert_not_awaited()
        finally:
            await bot.close()


if __name__ == "__main__":
    unittest.main()
