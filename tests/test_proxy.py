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
    ProxyError,
    encode_proxy_author_id,
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
            self.assertTrue(send_kwargs["content"].startswith("こんにちは！"))
            self.assertEqual(send_kwargs["username"], "サーバーニックネーム")
            self.assertEqual(send_kwargs["avatar_url"], "https://example.com/server.png")

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

    async def test_context_menu_delete_proxy(self) -> None:
        bot = YajuBot(BotConfig(token="dummy-token"))
        try:
            cog = ProxyCog(bot)

            encoded_id = encode_proxy_author_id(42)
            target_message = MagicMock(spec=discord.Message)
            target_message.id = 1234
            target_message.content = f"テスト{encoded_id}"

            # 他人による右クリック実行
            other_interaction = MagicMock(spec=discord.Interaction)
            other_interaction.guild = MagicMock(spec=discord.Guild)
            other_interaction.channel = MagicMock(spec=discord.TextChannel)
            other_interaction.user = SimpleNamespace(id=999)
            other_interaction.response = MagicMock()
            other_interaction.response.send_message = AsyncMock()

            await cog._context_menu_delete_proxy(other_interaction, target_message)
            other_interaction.response.send_message.assert_awaited_once()
            self.assertTrue(other_interaction.response.send_message.call_args.kwargs["ephemeral"])
            self.assertNotIn("view", other_interaction.response.send_message.call_args.kwargs)

            # 送信者本人による右クリック実行
            author_interaction = MagicMock(spec=discord.Interaction)
            author_interaction.guild = MagicMock(spec=discord.Guild)
            author_interaction.channel = MagicMock(spec=discord.TextChannel)
            author_interaction.user = SimpleNamespace(id=42)
            author_interaction.response = MagicMock()
            author_interaction.response.send_message = AsyncMock()

            await cog._context_menu_delete_proxy(author_interaction, target_message)
            author_interaction.response.send_message.assert_awaited_once()
            kwargs = author_interaction.response.send_message.call_args.kwargs
            self.assertTrue(kwargs["ephemeral"])
            self.assertIsInstance(kwargs["view"], ConfirmDeleteView)
        finally:
            await bot.close()

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

        # タイムアウト時の無効化
        mock_prompt_interaction = MagicMock(spec=discord.Interaction)
        mock_prompt_interaction.edit_original_response = AsyncMock()
        view3 = ConfirmDeleteView(
            target_message=target_message,
            author_id=42,
            prompt_interaction=mock_prompt_interaction,
        )
        self.assertFalse(view3.confirm_button.disabled)
        self.assertFalse(view3.cancel_button.disabled)

        await view3.on_timeout()
        self.assertTrue(view3.confirm_button.disabled)
        self.assertTrue(view3.cancel_button.disabled)
        mock_prompt_interaction.edit_original_response.assert_awaited_once()
        self.assertIn("時間切れだゾ", mock_prompt_interaction.edit_original_response.call_args.kwargs["content"])

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
        webhook_msg.content = "プロキシ送信されたメッセージ"
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
        nested_reply_msg.content = raw_with_reply
        nested_reply_msg.clean_content = raw_with_reply
        nested_reply_msg.jump_url = "https://discord.com/channels/1/2/9"

        nested_header = format_reply_header(nested_reply_msg)
        self.assertIn("2行目の本文です", nested_header)
        self.assertNotIn("引用", nested_header)

        # 通常メッセージ
        msg1 = MagicMock(spec=discord.Message)
        msg1.author = SimpleNamespace(mention="@バチバチマン")
        msg1.attachments = []
        msg1.content = "おおっ…凍結されちゃったんだよなぁ…カワイソに"
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
        msg2.content = "画像あげるよ"
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

            # 1回目: 選択メニュー（ReplyActionChoiceView）が表示される
            await cog._context_menu_toggle_reply(interaction, msg1)
            interaction.response.send_message.assert_awaited_once()
            choice_view = interaction.response.send_message.call_args.kwargs["view"]
            self.assertEqual(type(choice_view).__name__, "ReplyActionChoiceView")

            # 「返信先に設定する」ボタンを押す
            set_interaction = MagicMock(spec=discord.Interaction)
            set_interaction.channel = channel
            set_interaction.user = SimpleNamespace(id=42)
            set_interaction.response = MagicMock()
            set_interaction.response.edit_message = AsyncMock()
            await choice_view.set_reply_button.callback(set_interaction)
            self.assertEqual(cog._pending_replies.get(key), (111, "テストメッセージ1"))
            set_interaction.response.edit_message.assert_awaited_once()
            self.assertIn("返信先をセットしたゾ", set_interaction.response.edit_message.call_args.kwargs["content"])

            # 2回目: 同じメッセージで発火 -> キャンセル
            interaction.response.send_message.reset_mock()
            await cog._context_menu_toggle_reply(interaction, msg1)
            self.assertNotIn(key, cog._pending_replies)
            interaction.response.send_message.assert_awaited_once()
            self.assertIn("返信先の指定を解除したゾ", interaction.response.send_message.call_args.args[0])

            # 3回目: 「直接返信する」ボタンを押すとモーダルへダイレクト遷移（メンション選択あり）
            interaction.response.send_message.reset_mock()
            await cog._context_menu_toggle_reply(interaction, msg1)
            choice_view2 = interaction.response.send_message.call_args.kwargs["view"]

            direct_interaction = MagicMock(spec=discord.Interaction)
            direct_interaction.channel = channel
            direct_interaction.user = SimpleNamespace(id=42)
            direct_interaction.response = MagicMock()
            direct_interaction.response.send_modal = AsyncMock()
            await choice_view2.direct_reply_button.callback(direct_interaction)

            self.assertEqual(cog._pending_replies.get(key), (111, "テストメッセージ1"))
            direct_interaction.response.send_modal.assert_awaited_once()
            reply_modal = direct_interaction.response.send_modal.call_args.args[0]
            self.assertTrue(reply_modal.is_reply)
            self.assertIsNotNone(reply_modal.mention_select)
            self.assertEqual(len(reply_modal.children), 2)

            # 非返信時の「ほらいくどー」ボタンではメンション選択が見えない
            cog._pending_replies.pop(key, None)
            from yajubot.cogs.proxy import ProxyLauncherView
            launcher_view = ProxyLauncherView(cog)
            open_interaction = MagicMock(spec=discord.Interaction)
            open_interaction.channel = channel
            open_interaction.user = SimpleNamespace(id=42)
            open_interaction.message = MagicMock(spec=discord.Message)
            open_interaction.response = MagicMock()
            open_interaction.response.send_modal = AsyncMock()
            await launcher_view.open_modal.callback(open_interaction)
            normal_modal = open_interaction.response.send_modal.call_args.args[0]
            self.assertFalse(normal_modal.is_reply)
            self.assertIsNone(normal_modal.mention_select)
            self.assertEqual(len(normal_modal.children), 1)
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
            reply_target.content = "返信元の本文です"
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

    async def test_mark_reply_header_as_deleted(self) -> None:
        from yajubot.cogs.proxy import mark_reply_header_as_deleted, extract_editable_content

        text = "<:reply1:1571547845683712101><:reply2:1571547847264829490>@先輩 :[こんにちは](https://discord.com/channels/1/2/3)\n本文だよ"
        # 一致するメッセージID
        res, changed = mark_reply_header_as_deleted(text, 3)
        self.assertTrue(changed)
        self.assertEqual(res, "<:reply1:1571547845683712101><:reply2:1571547847264829490>削除されたメッセージ\n本文だよ")

        # 異なるメッセージID
        res2, changed2 = mark_reply_header_as_deleted(text, 999)
        self.assertFalse(changed2)
        self.assertEqual(res2, text)

        # メンションID形式 (<@123>) のヘッダー
        text_id = "<:reply1:1571547845683712101><:reply2:1571547847264829490><@987654321> :[こんにちは](https://discord.com/channels/1/2/3)\n本文だよ"
        res_id, changed_id = mark_reply_header_as_deleted(text_id, 3)
        self.assertTrue(changed_id)
        self.assertEqual(res_id, "<:reply1:1571547845683712101><:reply2:1571547847264829490>削除されたメッセージ\n本文だよ")

        # 編集用コンテンツ抽出
        editable = extract_editable_content(text)
        self.assertEqual(editable, "本文だよ")

    async def test_on_raw_message_delete_updates_proxy_reply(self) -> None:
        bot = YajuBot(BotConfig(token="dummy-token"))
        try:
            cog = ProxyCog(bot)
            bot_user = SimpleNamespace(id=100)

            channel = MagicMock(spec=discord.TextChannel)
            channel.id = 555

            proxy_msg = MagicMock(spec=discord.Message)
            proxy_msg.id = 7001
            proxy_msg.webhook_id = 999
            proxy_msg.content = (
                "<:reply1:1571547845683712101><:reply2:1571547847264829490>@先輩 :[削除されるやつ](https://discord.com/channels/1/2/888)\n返信本文"
            )

            other_msg = MagicMock(spec=discord.Message)
            other_msg.id = 7002
            other_msg.webhook_id = None
            other_msg.content = "通常のメッセージ"

            async def mock_history(limit=50):
                yield proxy_msg
                yield other_msg

            channel.history = mock_history

            webhook = MagicMock(spec=discord.Webhook)
            webhook.edit_message = AsyncMock()

            with (
                patch.object(type(bot), "user", new=bot_user),
                patch.object(type(bot), "get_channel", return_value=channel),
                patch.dict(
                    ProxyCog.__init__.__globals__,
                    {"get_or_create_proxy_webhook": AsyncMock(return_value=webhook)},
                ),
            ):
                payload = discord.RawMessageDeleteEvent(
                    {"id": 888, "channel_id": 555, "guild_id": 1}
                )
                await cog.on_raw_message_delete(payload)

                webhook.edit_message.assert_awaited_once()
                kwargs = webhook.edit_message.call_args.kwargs
                self.assertEqual(kwargs["message_id"], 7001)
                self.assertIn("削除されたメッセージ", kwargs["content"])
                self.assertNotIn("@先輩", kwargs["content"])
        finally:
            await bot.close()

    async def test_context_menu_edit_proxy_and_modal_submit(self) -> None:
        from yajubot.cogs.proxy import ProxyEditModal

        bot = YajuBot(BotConfig(token="dummy-token"))
        try:
            cog = ProxyCog(bot)
            bot_user = SimpleNamespace(id=100)

            encoded_id = encode_proxy_author_id(42)
            target_message = MagicMock(spec=discord.Message)
            target_message.id = 1234
            target_message.content = f"<:reply1:1571547845683712101><:reply2:1571547847264829490>@先輩 :[引用](url)\n編集前の本文{encoded_id}"

            # 他人による編集試行（拒否される）
            other_interaction = MagicMock(spec=discord.Interaction)
            other_interaction.guild = MagicMock(spec=discord.Guild)
            other_interaction.channel = MagicMock(spec=discord.TextChannel)
            other_interaction.user = SimpleNamespace(id=999)
            other_interaction.response = MagicMock()
            other_interaction.response.send_message = AsyncMock()

            await cog._context_menu_edit_proxy(other_interaction, target_message)
            other_interaction.response.send_message.assert_awaited_once()
            self.assertIn("本人以外は編集できないゾ", other_interaction.response.send_message.call_args.args[0])

            # 本人による編集メニュー呼び出し（モーダルが開く）
            author_interaction = MagicMock(spec=discord.Interaction)
            author_interaction.guild = MagicMock(spec=discord.Guild)
            author_interaction.channel = MagicMock(spec=discord.TextChannel)
            author_interaction.user = SimpleNamespace(id=42)
            author_interaction.response = MagicMock()
            author_interaction.response.send_modal = AsyncMock()

            await cog._context_menu_edit_proxy(author_interaction, target_message)
            author_interaction.response.send_modal.assert_awaited_once()
            modal = author_interaction.response.send_modal.call_args.args[0]
            self.assertEqual(type(modal).__name__, "ProxyEditModal")
            self.assertEqual(modal.message_input.default, "編集前の本文")

            # モーダル送信（Webhook 編集の実行）
            submit_interaction = MagicMock(spec=discord.Interaction)
            submit_interaction.guild = MagicMock(spec=discord.Guild)
            submit_interaction.guild.emojis = []
            submit_interaction.channel = MagicMock(spec=discord.TextChannel)
            submit_interaction.user = SimpleNamespace(id=42)
            submit_interaction.response = MagicMock()
            submit_interaction.response.defer = AsyncMock()
            submit_interaction.followup = MagicMock()
            submit_interaction.followup.send = AsyncMock()

            modal.message_input._value = "編集後の新しい本文"

            webhook = MagicMock(spec=discord.Webhook)
            webhook.edit_message = AsyncMock()

            with (
                patch.object(type(bot), "user", new=bot_user),
                patch.dict(
                    ProxyCog.__init__.__globals__,
                    {"get_or_create_proxy_webhook": AsyncMock(return_value=webhook)},
                ),
            ):
                await modal.on_submit(submit_interaction)
                webhook.edit_message.assert_awaited_once()
                kwargs = webhook.edit_message.call_args.kwargs
                self.assertEqual(kwargs["message_id"], 1234)
                self.assertIn("編集後の新しい本文", kwargs["content"])
                self.assertIn("reply1", kwargs["content"])  # 元の返信ヘッダーが保持されている
                submit_interaction.followup.send.assert_awaited_once()
                self.assertIn("メッセージを編集したゾ", submit_interaction.followup.send.call_args.args[0])
        finally:
            await bot.close()

    async def test_ratelimit_error_handling(self) -> None:
        from discord.errors import RateLimited, HTTPException
        from yajubot.cogs.proxy import format_ratelimit_message, is_ratelimited_error

        rl = RateLimited(4.5)
        self.assertTrue(is_ratelimited_error(rl))
        self.assertIn("あと 4.5 秒待ってからやり直してくれよな～頼むよ～", format_ratelimit_message(rl))

        class MockResp:
            status = 429
            reason = "Too Many Requests"

        http_429 = HTTPException(MockResp(), "You are being rate limited. retry_after: 3.2")
        self.assertTrue(is_ratelimited_error(http_429))
        self.assertIn("あと 3.2 秒待ってからやり直してくれよな～頼むよ～", format_ratelimit_message(http_429))


if __name__ == "__main__":
    unittest.main()
