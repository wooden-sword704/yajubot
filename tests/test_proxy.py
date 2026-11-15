"""代替送信システム（ProxyCog / Webhook / 削除確認ダイアログ）の単体テスト。"""

from __future__ import annotations

import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from yajubot.bot import YajuBot
from yajubot.cogs.proxy import (
    ConfirmDeleteView,
    ProxyCog,
    ProxyDeleteButton,
    ProxyError,
    ProxyMessageView,
    get_or_create_proxy_webhook,
)
from yajubot.config import BotConfig


class ProxyWebhookTest(unittest.IsolatedAsyncioTestCase):
    async def test_reuses_bot_owned_webhook_with_token(self) -> None:
        foreign_wh = SimpleNamespace(token=None, user=SimpleNamespace(id=999))
        bot_wh = SimpleNamespace(token="wh-token", user=SimpleNamespace(id=100))

        channel = MagicMock()
        channel.webhooks = AsyncMock(return_value=[foreign_wh, bot_wh])
        channel.create_webhook = AsyncMock()

        result = await get_or_create_proxy_webhook(channel, bot_user_id=100)
        self.assertIs(result, bot_wh)
        channel.create_webhook.assert_not_awaited()

    async def test_creates_webhook_when_none_owned(self) -> None:
        created_wh = SimpleNamespace(token="new-token", user=SimpleNamespace(id=100))
        channel = MagicMock()
        channel.webhooks = AsyncMock(return_value=[])
        channel.create_webhook = AsyncMock(return_value=created_wh)

        result = await get_or_create_proxy_webhook(channel, bot_user_id=100)
        self.assertIs(result, created_wh)
        channel.create_webhook.assert_awaited_once()

    async def test_raises_proxy_error_when_missing_permission(self) -> None:
        channel = MagicMock()
        channel.webhooks = AsyncMock(
            side_effect=discord.Forbidden(MagicMock(status=403), "Missing Permissions")
        )
        with self.assertRaises(ProxyError):
            await get_or_create_proxy_webhook(channel, bot_user_id=100)


class ProxyFlowAndDeleteTest(unittest.IsolatedAsyncioTestCase):
    async def test_send_proxy_message_posts_webhook_and_reposts_launcher(self) -> None:
        bot = YajuBot(BotConfig(token="dummy-token"))
        try:
            cog = ProxyCog(bot)
            bot_user = SimpleNamespace(id=100)

            old_partial = MagicMock()
            old_partial.delete = AsyncMock()

            new_launcher = SimpleNamespace(id=777)
            channel = MagicMock(spec=discord.TextChannel)
            channel.id = 555
            channel.get_partial_message.return_value = old_partial
            channel.send = AsyncMock(return_value=new_launcher)

            cog._latest_launcher_ids[555] = 666

            webhook = MagicMock(spec=discord.Webhook)
            webhook.send = AsyncMock()

            member = MagicMock(spec=discord.Member)
            member.id = 42
            member.display_name = "サーバーニックネーム"
            member.display_avatar = SimpleNamespace(url="https://example.com/server.png")

            interaction = MagicMock(spec=discord.Interaction)
            interaction.channel = channel
            interaction.guild = MagicMock(spec=discord.Guild)
            interaction.user = member

            with (
                patch.object(
                    type(bot), "user", new=bot_user
                ),
                patch.dict(
                    ProxyCog.__init__.__globals__,
                    {"get_or_create_proxy_webhook": AsyncMock(return_value=webhook)},
                ),
            ):
                await cog.send_proxy_message(
                    interaction=interaction,
                    content="こんにちは！",
                    clicked_launcher=None,
                )

            webhook.send.assert_awaited_once()
            send_kwargs = webhook.send.call_args.kwargs
            self.assertEqual(send_kwargs["content"], "こんにちは！")
            self.assertEqual(send_kwargs["username"], "サーバーニックネーム")
            self.assertEqual(send_kwargs["avatar_url"], "https://example.com/server.png")
            self.assertIsInstance(send_kwargs["view"], ProxyMessageView)

            # スロットリングタスクの完了を待機
            task = cog._pending_throttle_tasks.get(555)
            if task is not None:
                await task

            old_partial.delete.assert_awaited_once()
            channel.send.assert_awaited_once()
            self.assertEqual(cog._latest_launcher_ids[555], 777)

            # 直接送信（repost_launcher=False）時は待機ボタンの削除・再送信を行わない
            old_partial.delete.reset_mock()
            channel.send.reset_mock()
            webhook.send.reset_mock()
            with (
                patch.object(type(bot), "user", new=bot_user),
                patch.dict(
                    ProxyCog.__init__.__globals__,
                    {"get_or_create_proxy_webhook": AsyncMock(return_value=webhook)},
                ),
            ):
                await cog.send_proxy_message(
                    interaction=interaction,
                    content="直接送信テスト",
                    clicked_launcher=None,
                    repost_launcher=False,
                )
            webhook.send.assert_awaited_once()
            old_partial.delete.assert_not_awaited()
            channel.send.assert_not_awaited()
        finally:
            await bot.close()

    async def test_delete_button_rejects_other_user_and_confirms_for_author(self) -> None:
        match = re.match(r"proxy:delete:(?P<author_id>\d+)", "proxy:delete:42")
        assert match is not None

        button = await ProxyDeleteButton.from_custom_id(
            MagicMock(spec=discord.Interaction),
            MagicMock(spec=discord.ui.Button),
            match,
        )
        self.assertEqual(button.author_id, 42)

        # 他人が押した場合は拒否
        other_interaction = MagicMock(spec=discord.Interaction)
        other_interaction.user = SimpleNamespace(id=999)
        other_interaction.response = MagicMock()
        other_interaction.response.send_message = AsyncMock()

        await button.callback(other_interaction)
        other_interaction.response.send_message.assert_awaited_once()
        self.assertTrue(other_interaction.response.send_message.call_args.kwargs["ephemeral"])
        self.assertNotIn("view", other_interaction.response.send_message.call_args.kwargs)

        # 本人が押した場合はエフェメラルの確認ダイアログを表示
        target_message = MagicMock(spec=discord.Message)
        author_interaction = MagicMock(spec=discord.Interaction)
        author_interaction.user = SimpleNamespace(id=42)
        author_interaction.message = target_message
        author_interaction.response = MagicMock()
        author_interaction.response.send_message = AsyncMock()

        await button.callback(author_interaction)
        author_interaction.response.send_message.assert_awaited_once()
        kwargs = author_interaction.response.send_message.call_args.kwargs
        self.assertTrue(kwargs["ephemeral"])
        self.assertIsInstance(kwargs["view"], ConfirmDeleteView)

    async def test_confirm_delete_view_deletes_and_cancels(self) -> None:
        target_message = MagicMock(spec=discord.Message)
        target_message.id = 1234
        target_message.channel = MagicMock(spec=discord.TextChannel)

        view = ConfirmDeleteView(target_message=target_message, author_id=42)

        confirm_interaction = MagicMock(spec=discord.Interaction)
        confirm_interaction.user = SimpleNamespace(id=42)
        confirm_interaction.client = SimpleNamespace(user=SimpleNamespace(id=100))
        confirm_interaction.response = MagicMock()
        confirm_interaction.response.defer = AsyncMock()
        confirm_interaction.edit_original_response = AsyncMock()

        mock_del = AsyncMock()
        with patch.dict(
            ProxyCog.__init__.__globals__,
            {"delete_webhook_message": mock_del},
        ):
            await view.confirm_button.callback(confirm_interaction)
            mock_del.assert_awaited_once_with(target_message, 100)

        confirm_interaction.edit_original_response.assert_awaited_once()
        self.assertIsNone(
            confirm_interaction.edit_original_response.call_args.kwargs["view"]
        )

        # キャンセルボタン
        view2 = ConfirmDeleteView(target_message=target_message, author_id=42)
        cancel_interaction = MagicMock(spec=discord.Interaction)
        cancel_interaction.response = MagicMock()
        cancel_interaction.response.edit_message = AsyncMock()

        await view2.cancel_button.callback(cancel_interaction)
        cancel_interaction.response.edit_message.assert_awaited_once()
        self.assertIsNone(
            cancel_interaction.response.edit_message.call_args.kwargs["view"]
        )

    async def test_format_reply_header(self) -> None:
        from yajubot.cogs.proxy import (
            expand_custom_emojis,
            extract_proxy_author_id,
            format_reply_header,
            strip_reply_header,
        )

        # 絵文字自動展開テスト
        emoji_map = {
            "gabigabipinky": "<a:gabigabipinky:1571542577700671582>",
            "ikisugi": "<:ikisugi:1447802942739447829>",
        }
        text_before = "これは :gabigabipinky: テスト <:already:123> :ikisugi: :not_found:"
        text_after = expand_custom_emojis(text_before, emoji_map)
        self.assertEqual(
            text_after,
            "これは <a:gabigabipinky:1571542577700671582> テスト <:already:123> <:ikisugi:1447802942739447829> :not_found:",
        )

        # Webhookメッセージへの返信テスト（送信者ユーザーIDがメンションに使われる）
        webhook_msg = MagicMock(spec=discord.Message)
        webhook_msg.author = SimpleNamespace(mention="@WebhookUser", display_name="WebhookUser")
        child_btn = SimpleNamespace(custom_id="proxy:delete:987654321")
        row = SimpleNamespace(children=[child_btn])
        webhook_msg.components = [row]
        webhook_msg.attachments = []
        webhook_msg.clean_content = "プロキシ送信されたメッセージ"
        webhook_msg.jump_url = "https://discord.com/channels/1/2/10"

        self.assertEqual(extract_proxy_author_id(webhook_msg), 987654321)
        wh_header = format_reply_header(webhook_msg)
        self.assertIn("<@987654321>", wh_header)
        self.assertNotIn("@WebhookUser", wh_header)

        # 返信ヘッダーのストリップテスト
        raw_with_reply = "<:reply1:1571547845683712101><:reply2:1571547847264829490>@誰か :[引用]\n2行目の本文です"
        self.assertEqual(strip_reply_header(raw_with_reply), "2行目の本文です")

        # 返信への返信テスト（1行目のヘッダーが除去されて2行目から引用される）
        nested_reply_msg = MagicMock(spec=discord.Message)
        nested_reply_msg.author = SimpleNamespace(mention="@後輩")
        nested_reply_msg.attachments = []
        nested_reply_msg.clean_content = raw_with_reply
        nested_reply_msg.jump_url = "https://discord.com/channels/1/2/9"

        nested_header = format_reply_header(nested_reply_msg)
        self.assertIn("2行目の本文です", nested_header)
        self.assertNotIn("引用", nested_header)

        # 通常メッセージ
        msg1 = MagicMock(spec=discord.Message)
        msg1.author = SimpleNamespace(mention="@バチバチマン")
        msg1.attachments = []
        msg1.clean_content = "おおっ…凍結されちゃったんだよなぁ…カワイソに"
        msg1.jump_url = "https://discord.com/channels/1/2/3"

        header1 = format_reply_header(msg1)
        self.assertTrue(header1.startswith("<:reply1:1571547845683712101><:reply2:1571547847264829490>@バチバチマン :["))
        self.assertIn("](https://discord.com/channels/1/2/3)", header1)
        self.assertIn("おおっ…", header1)

        # 添付ファイル混在メッセージ
        msg2 = MagicMock(spec=discord.Message)
        msg2.author = SimpleNamespace(mention="@先輩")
        msg2.attachments = [MagicMock()]
        msg2.clean_content = "画像あげるよ"
        msg2.jump_url = "https://discord.com/channels/1/2/4"

        header2 = format_reply_header(msg2)
        self.assertEqual(
            header2,
            "<:reply1:1571547845683712101><:reply2:1571547847264829490>@先輩 :[添付ファイル](https://discord.com/channels/1/2/4)",
        )

        # 添付ファイルなし・テキストなし（フォールバック）
        msg3 = MagicMock(spec=discord.Message)
        msg3.author = SimpleNamespace(mention="@先輩")
        msg3.attachments = []
        msg3.clean_content = ""
        msg3.content = ""
        msg3.embeds = []
        msg3.jump_url = "https://discord.com/channels/1/2/5"

        header3 = format_reply_header(msg3)
        self.assertEqual(
            header3,
            "<:reply1:1571547845683712101><:reply2:1571547847264829490>@先輩 :[メッセージ](https://discord.com/channels/1/2/5)",
        )

    async def test_on_message_reposts_launcher(self) -> None:
        bot = YajuBot(BotConfig(token="dummy-token"))
        try:
            cog = ProxyCog(bot)
            channel = MagicMock(spec=discord.TextChannel)
            channel.id = 777

            old_launcher = MagicMock()
            old_launcher.delete = AsyncMock()
            channel.get_partial_message.return_value = old_launcher
            channel.send = AsyncMock(return_value=SimpleNamespace(id=999))

            cog._latest_launcher_ids[777] = 888

            # 一般ユーザーからの通常メッセージ受信
            user_msg = MagicMock(spec=discord.Message)
            user_msg.guild = MagicMock()
            user_msg.author = SimpleNamespace(bot=False)
            user_msg.webhook_id = None
            user_msg.channel = channel

            await cog.on_message(user_msg)

            # スロットリングタスクの完了を待機
            task = cog._pending_throttle_tasks.get(777)
            if task is not None:
                await task

            old_launcher.delete.assert_awaited_once()
            channel.send.assert_awaited_once()
            self.assertEqual(cog._latest_launcher_ids[777], 999)
        finally:
            await bot.close()

    async def test_launcher_repost_throttling_burst(self) -> None:
        """短時間に連続メッセージが送信された場合、スロットリングにより再送信がまとめられることをテスト。"""
        bot = YajuBot(BotConfig(token="dummy-token"))
        try:
            cog = ProxyCog(bot)
            channel = MagicMock(spec=discord.TextChannel)
            channel.id = 888

            channel.get_partial_message.return_value = MagicMock(delete=AsyncMock())
            channel.send = AsyncMock(return_value=SimpleNamespace(id=1001))
            cog._latest_launcher_ids[888] = 1000

            user_msg = MagicMock(spec=discord.Message)
            user_msg.guild = MagicMock()
            user_msg.author = SimpleNamespace(bot=False)
            user_msg.webhook_id = None
            user_msg.channel = channel

            # 短時間に3回連続でメッセージを受信
            await cog.on_message(user_msg)
            await cog.on_message(user_msg)
            await cog.on_message(user_msg)

            # タスクは1つにまとめられる
            task = cog._pending_throttle_tasks.get(888)
            self.assertIsNotNone(task)
            if task is not None:
                await task

            self.assertLessEqual(channel.send.await_count, 2)
        finally:
            await bot.close()
        bot = YajuBot(BotConfig(token="dummy-token"))
        try:
            cog = ProxyCog(bot)
            msg1 = MagicMock(spec=discord.Message)
            msg1.id = 111
            msg1.clean_content = "テストメッセージ1"
            msg1.author = SimpleNamespace(display_name="ユーザーA")

            interaction = MagicMock(spec=discord.Interaction)
            interaction.guild = MagicMock(spec=discord.Guild)
            channel = MagicMock(spec=discord.TextChannel)
            channel.id = 999
            interaction.channel = channel
            interaction.user = SimpleNamespace(id=42)
            interaction.response = MagicMock()
            interaction.response.send_message = AsyncMock()

            key = (42, 999)

            # 1回目: 返信先セット
            await cog._context_menu_toggle_reply(interaction, msg1)
            self.assertEqual(cog._pending_replies.get(key), (111, "テストメッセージ1"))
            interaction.response.send_message.assert_awaited_once()
            self.assertIn("返信先をセットしました", interaction.response.send_message.call_args.args[0])

            # 2回目: 同じメッセージで発火 -> キャンセル
            interaction.response.send_message.reset_mock()
            await cog._context_menu_toggle_reply(interaction, msg1)
            self.assertNotIn(key, cog._pending_replies)
            interaction.response.send_message.assert_awaited_once()
            self.assertIn("返信先の指定を解除しました", interaction.response.send_message.call_args.args[0])
        finally:
            await bot.close()

    async def test_say_proxy_with_reply_embed(self) -> None:
        bot = YajuBot(BotConfig(token="dummy-token"))
        try:
            cog = ProxyCog(bot)
            bot_user = SimpleNamespace(id=100)

            channel = MagicMock(spec=discord.TextChannel)
            channel.id = 555
            reply_target = MagicMock(spec=discord.Message)
            reply_target.id = 888
            reply_target.author = SimpleNamespace(display_name="返信相手", display_avatar=SimpleNamespace(url="https://example.com/avatar.png"))
            reply_target.attachments = []
            reply_target.clean_content = "返信元の本文です"
            reply_target.jump_url = "https://discord.com/channels/1/2/888"

            channel.fetch_message = AsyncMock(return_value=reply_target)

            interaction = MagicMock(spec=discord.Interaction)
            interaction.channel = channel
            interaction.guild = MagicMock(spec=discord.Guild)
            interaction.user = SimpleNamespace(id=42, display_name="送信者", display_avatar=SimpleNamespace(url="https://example.com/me.png"))
            interaction.response = MagicMock()
            interaction.response.defer = AsyncMock()
            interaction.delete_original_response = AsyncMock()

            webhook = MagicMock(spec=discord.Webhook)
            webhook.send = AsyncMock()

            with (
                patch.object(type(bot), "user", new=bot_user),
                patch.dict(
                    ProxyCog.__init__.__globals__,
                    {"get_or_create_proxy_webhook": AsyncMock(return_value=webhook)},
                ),
            ):
                # メンションON
                await cog.say_proxy.callback(
                    cog, interaction, "返信付きテスト", reply_to="888", mention=True
                )
                webhook.send.assert_awaited_once()
                kwargs = webhook.send.call_args.kwargs
                self.assertTrue(kwargs["allowed_mentions"].users)

                # メンションOFF（ユーザー通知なし・ハイライトなし）
                webhook.send.reset_mock()
                await cog.say_proxy.callback(
                    cog, interaction, "返信付きテスト2", reply_to="888", mention=False
                )
                webhook.send.assert_awaited_once()
                kwargs_off = webhook.send.call_args.kwargs
                self.assertFalse(kwargs_off["allowed_mentions"].users)
                self.assertIn("@返信相手", kwargs_off["content"])
        finally:
            await bot.close()


if __name__ == "__main__":
    unittest.main()
