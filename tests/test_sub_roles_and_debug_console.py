"""サブスクロール課金・剥奪および CUI デバッグコンソールの単体テスト。"""

from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from yajubot.bot import YajuBot
from yajubot.cogs.admin import AdminPanelView, SubRoleCostModal, SubRolePlanView
from yajubot.cogs.omikuji import OmikujiCog, draw_omikuji
from yajubot.cogs.shinkei import (
    DIFFICULTIES,
    PERFECT_CLEAR_REWARDS,
    ShinkeiGame,
    apply_forced_shinkei_moves,
    calculate_shinkei_reward,
)
from yajubot.cogs.tadocoin import (
    SubNotifyMuteView,
    SubRoleView,
    TadocoinCog,
    process_sub_role_billing,
)
from yajubot.config import BotConfig
from yajubot.debug_console import DebugConsoleController
from yajubot.storage import DEFAULT_MAX_RECORDS, OWNER_USER_ID, LogCipher, ProxyStorage


def _make_role(
    role_id: int, name: str, permissions: discord.Permissions | None = None
) -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.mention = f"<@&{role_id}>"
    role.permissions = permissions or discord.Permissions.none()
    role.is_default.return_value = False
    role.managed = False
    return role


class SubRolesAndDebugConsoleTest(unittest.IsolatedAsyncioTestCase):
    async def test_sub_role_admin_subscribe_and_billing_revocation(self) -> None:
        bot = YajuBot(BotConfig(token="dummy"), storage=ProxyStorage(":memory:"))
        try:
            guild_id = 100
            vip_role = _make_role(501, "VIP")
            danger_role = _make_role(
                502, "Admin", discord.Permissions(administrator=True)
            )

            member = MagicMock(spec=discord.Member)
            member.id = 42
            member.roles = []
            member.send = AsyncMock()
            member.add_roles = AsyncMock(side_effect=lambda r, **kw: member.roles.append(r))
            member.remove_roles = AsyncMock(
                side_effect=lambda r, **kw: member.roles.remove(r)
                if r in member.roles
                else None
            )

            guild = MagicMock(spec=discord.Guild)
            guild.id = guild_id
            guild.name = "TestGuild"
            guild.get_role.side_effect = lambda rid: {501: vip_role, 502: danger_role}.get(
                rid
            )
            guild.get_member.return_value = member

            owner = SimpleNamespace(
                id=OWNER_USER_ID, roles=[], guild_permissions=discord.Permissions.none()
            )

            # 1. Admin panel からサブスクロール設定画面へ遷移
            panel = AdminPanelView(bot, guild_id)
            inter = MagicMock(spec=discord.Interaction)
            inter.user = owner
            inter.response = MagicMock()
            inter.response.edit_message = AsyncMock()
            sel = MagicMock(spec=discord.ui.Select)
            sel.values = ["sub_roles"]
            await AdminPanelView.menu_select(panel, inter, sel)
            sub_plan_view = inter.response.edit_message.call_args.kwargs["view"]
            self.assertIsInstance(sub_plan_view, SubRolePlanView)

            # 2. 危険ロールは拒否される
            role_sel_bad = MagicMock(spec=discord.ui.RoleSelect)
            role_sel_bad.values = [danger_role]
            inter_bad = MagicMock(spec=discord.Interaction)
            inter_bad.user = owner
            inter_bad.response = MagicMock()
            inter_bad.response.send_message = AsyncMock()
            await SubRolePlanView.role_select(sub_plan_view, inter_bad, role_sel_bad)
            inter_bad.response.send_message.assert_awaited_once()

            # 3. VIP ロールを週額 300 タドコインで設定
            modal = SubRoleCostModal(bot, guild_id, vip_role, sub_plan_view)
            modal.cost_input._value = "300"
            inter_modal = MagicMock(spec=discord.Interaction)
            inter_modal.user = owner
            inter_modal.response = MagicMock()
            inter_modal.response.edit_message = AsyncMock()
            await modal.on_submit(inter_modal)
            self.assertEqual(bot.storage.get_sub_role_plans(guild_id), {501: 300})

            # 4. ユーザーが加入（残高 500 -> 300 消費して 200 残る）
            bot.storage.set_tadocoin(guild_id, member.id, 500)
            plans = bot.storage.get_sub_role_plans(guild_id)
            sub_view = SubRoleView(bot, guild, member.id, plans, {})
            inter_sub = MagicMock(spec=discord.Interaction)
            inter_sub.guild = guild
            inter_sub.user = member
            inter_sub.data = {"values": ["501"]}
            inter_sub.response = MagicMock()
            inter_sub.response.edit_message = AsyncMock()
            inter_sub.response.send_message = AsyncMock()
            await sub_view._on_select(inter_sub)

            self.assertEqual(bot.storage.get_tadocoin_balance(guild_id, member.id), 200)
            self.assertIn(vip_role, member.roles)
            self.assertIn(501, bot.storage.get_user_subscriptions(guild_id, member.id))

            # 5. 期限未到来なので force=False では引き落とされない
            with patch.object(type(bot), "get_guild", return_value=guild):
                renewed, revoked, _ = await process_sub_role_billing(bot, force=False)
                self.assertEqual((renewed, revoked), (0, 0))

                # 6. 強制引き落とし（残高 200 < 300 なのでロール剥奪＆引き落とし中止！）
                renewed, revoked, _ = await process_sub_role_billing(bot, force=True)
                self.assertEqual((renewed, revoked), (0, 1))
                self.assertNotIn(vip_role, member.roles)
                self.assertEqual(
                    bot.storage.get_user_subscriptions(guild_id, member.id), {}
                )

                # 7. 再度十分な残高で加入し、force=True で更新 DM が届くことを確認
                bot.storage.set_tadocoin(guild_id, member.id, 1000)
                bot.storage.subscribe_user_role(
                    guild_id, member.id, 501, int(time.time()) + 3600
                )
                member.roles.append(vip_role)
                member.send.reset_mock()
                renewed, revoked, _ = await process_sub_role_billing(bot, force=True)
                self.assertEqual((renewed, revoked), (1, 0))
                self.assertEqual(
                    bot.storage.get_tadocoin_balance(guild_id, member.id), 700
                )
                member.send.assert_awaited_once()
                mute_view = member.send.call_args.kwargs["view"]
                self.assertIsInstance(mute_view, SubNotifyMuteView)

                # 8. ミュートボタンを押すと以降の更新通知 DM が止まる
                tcog = TadocoinCog(bot)
                inter_mute = MagicMock(spec=discord.Interaction)
                inter_mute.type = discord.InteractionType.component
                inter_mute.data = {"custom_id": f"sub_mute:{guild_id}:{member.id}:501"}
                inter_mute.user = member
                inter_mute.response = MagicMock()
                inter_mute.response.edit_message = AsyncMock()
                inter_mute.followup = MagicMock()
                inter_mute.followup.send = AsyncMock()
                await tcog._handle_sub_mute_button(inter_mute)
                self.assertTrue(
                    bot.storage.is_sub_notify_muted(guild_id, member.id, role_id=501)
                )
                member.send.reset_mock()
                await process_sub_role_billing(bot, force=True)
                member.send.assert_not_awaited()

                # 9. Bot に DM を送るとミュート解除され、再開メッセージが送信される
                dm_msg = MagicMock(spec=discord.Message)
                dm_msg.author = SimpleNamespace(id=member.id, bot=False)
                dm_msg.guild = None
                dm_msg.channel = MagicMock()
                dm_msg.channel.send = AsyncMock()
                await tcog.on_message(dm_msg)
                dm_msg.channel.send.assert_awaited_once()
                self.assertIn("再開", dm_msg.channel.send.call_args.args[0])
                self.assertFalse(
                    bot.storage.is_sub_notify_muted(guild_id, member.id, role_id=501)
                )
                await process_sub_role_billing(bot, force=True)
                member.send.assert_awaited_once()
        finally:
            await bot.close()

    async def test_debug_console_navigation_and_forced_actions(self) -> None:
        bot = YajuBot(BotConfig(token="dummy"), storage=ProxyStorage(":memory:"))
        try:
            # asyncio loop を bot に設定して run_coroutine_threadsafe が動くようにする
            bot._connection.loop = asyncio.get_running_loop()  # type: ignore[attr-defined]
            ctrl = DebugConsoleController(bot)

            self.assertEqual(ctrl.current_menu_id, "root")
            # ↓ で「次回おみくじティア指定」へ移動して Enter
            ctrl.handle_key("DOWN")
            ctrl.handle_key("ENTER")
            self.assertEqual(ctrl.current_menu_id, "omikuji")

            # 先頭（おおあたり）を Enter で指定
            ctrl.handle_key("ENTER")
            self.assertIsNotNone(bot.debug_next_omikuji_tier)
            forced_res = draw_omikuji(forced_tier_name=bot.debug_next_omikuji_tier)
            self.assertIn("お　お　あ　た　り", forced_res.tier.name)

            # OmikujiCog で1回消費されると解除される
            omikuji_cog = OmikujiCog(bot)
            guild = MagicMock(spec=discord.Guild)
            guild.id = 1
            inter = MagicMock(spec=discord.Interaction)
            inter.guild = guild
            inter.user = SimpleNamespace(id=10, display_name="User10")
            inter.response = MagicMock()
            inter.response.send_message = AsyncMock()
            await omikuji_cog.omikuji.callback(omikuji_cog, inter)
            self.assertIsNone(bot.debug_next_omikuji_tier)

            # Esc で root に戻る
            ctrl.handle_key("ESC")
            self.assertEqual(ctrl.current_menu_id, "root")

            # ↓ で「次回スロット出目指定」へ移動して Enter -> jackpot 指定
            ctrl.handle_key("DOWN")
            ctrl.handle_key("ENTER")
            self.assertEqual(ctrl.current_menu_id, "slot")
            ctrl.handle_key("ENTER")
            self.assertEqual(bot.debug_next_slot_outcome, "jackpot")

            # レンダリング出力の確認
            rendered = ctrl.render()
            self.assertIn("yajubot デバッグコンソール", rendered)
            self.assertIn("jackpot", rendered)

            # 次回神経衰弱の結果すり替え指定
            ctrl.handle_key("ESC")
            for _ in range(4):
                ctrl.handle_key("DOWN")
            ctrl.handle_key("ENTER")
            self.assertEqual(ctrl.current_menu_id, "shinkei")
            ctrl.handle_key("ENTER")  # perfect
            self.assertEqual(bot.debug_next_shinkei_result, "perfect")

            g = ShinkeiGame(
                guild_id=1,
                host_id=10,
                difficulty=DIFFICULTIES["normal"],
                players=[10],
                cards=[],
                moves=30,
            )
            apply_forced_shinkei_moves(g, "perfect")
            self.assertEqual(g.moves, 8)
            reward, _ = calculate_shinkei_reward("normal", g.moves)
            self.assertEqual(reward, PERFECT_CLEAR_REWARDS["normal"][1])

            # TadocoinCog の登録確認
            tcog = TadocoinCog(bot)
            self.assertEqual(tcog.subrole.name, "subrole")

            # 再起動アクションの確認
            self.assertFalse(bot.debug_restart_requested)
            msg = ctrl._action_restart()
            self.assertIn("再起動", msg)
            self.assertTrue(bot.debug_restart_requested)
            self.assertTrue(ctrl.should_exit)

            # 全データリセット 3回確認フローの検証
            self.assertEqual(DEFAULT_MAX_RECORDS, 1_000_000)
            cipher = LogCipher(key_path=None)
            enc = cipher.encrypt("日本語テストテキスト" * 20)
            self.assertEqual(cipher.decrypt(enc), "日本語テストテキスト" * 20)
            # 既存の非圧縮 Fernet トークンも復号できること
            legacy_enc = cipher._fernet.encrypt("旧形式メッセージ".encode("utf-8"))
            self.assertEqual(cipher.decrypt(legacy_enc), "旧形式メッセージ")

            bot.storage.set_tadocoin(1, 42, 999)
            ctrl2 = DebugConsoleController(bot)
            # root -> reset_confirm_1
            for _ in range(4):
                ctrl2.handle_key("DOWN")
            ctrl2.handle_key("ENTER")
            self.assertEqual(ctrl2.current_menu_id, "reset_confirm_1")
            # デフォルト位置（キャンセル）で Enter しても消えない
            ctrl2.handle_key("ENTER")
            self.assertEqual(ctrl2.current_menu_id, "root")
            self.assertEqual(bot.storage.get_tadocoin_balance(1, 42), 999)

            # 3回確認を進めてリセット実行
            for _ in range(4):
                ctrl2.handle_key("DOWN")
            ctrl2.handle_key("ENTER")  # -> reset_confirm_1
            ctrl2.handle_key("DOWN")
            ctrl2.handle_key("ENTER")  # -> reset_confirm_2
            self.assertEqual(ctrl2.current_menu_id, "reset_confirm_2")
            ctrl2.handle_key("DOWN")
            ctrl2.handle_key("ENTER")  # -> reset_confirm_3
            self.assertEqual(ctrl2.current_menu_id, "reset_confirm_3")
            ctrl2.handle_key("DOWN")
            ctrl2.handle_key("ENTER")  # -> 実行
            self.assertEqual(ctrl2.current_menu_id, "root")
            self.assertEqual(bot.storage.get_tadocoin_balance(1, 42), 0)
        finally:
            await bot.close()


if __name__ == "__main__":
    unittest.main()
