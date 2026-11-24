"""ロール付け外し機能（/admin panel 設定・権限チェック・/role）の単体テスト。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from yajubot.bot import YajuBot
from yajubot.cogs.admin import (
    AdminPanelView,
    SelfRoleConfirmView,
    SelfRoleSettingView,
)
from yajubot.cogs.roles import (
    RolesCog,
    SelfRoleToggleView,
    inspect_self_role,
    resolve_user_eligible_roles,
)
from yajubot.config import BotConfig
from yajubot.storage import OWNER_USER_ID, ProxyStorage


def _make_role(
    role_id: int,
    name: str,
    permissions: discord.Permissions,
    *,
    is_default: bool = False,
    managed: bool = False,
    position: int = 1,
) -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    role.mention = f"<@&{role_id}>"
    role.permissions = permissions
    role.is_default.return_value = is_default
    role.managed = managed
    role.position = position
    return role


class SelfRolesTest(unittest.IsolatedAsyncioTestCase):
    def test_inspect_self_role_classification(self) -> None:
        cosmetic = _make_role(1, "コスメ", discord.Permissions.none())
        res_cosmetic = inspect_self_role(cosmetic)
        self.assertFalse(res_cosmetic.is_blocked)
        self.assertFalse(res_cosmetic.requires_confirmation)

        mild = _make_role(2, "発言許可", discord.Permissions(send_messages=True))
        res_mild = inspect_self_role(mild)
        self.assertFalse(res_mild.is_blocked)
        self.assertTrue(res_mild.requires_confirmation)
        self.assertIn("send_messages", res_mild.warning_permissions)

        for flag in (
            "administrator",
            "ban_members",
            "kick_members",
            "moderate_members",
            "manage_channels",
            "manage_guild",
        ):
            dangerous = _make_role(10, flag, discord.Permissions(**{flag: True}))
            res_danger = inspect_self_role(dangerous)
            self.assertTrue(res_danger.is_blocked, f"{flag} should be blocked")

        everyone = _make_role(
            11, "@everyone", discord.Permissions.none(), is_default=True
        )
        self.assertTrue(inspect_self_role(everyone).is_blocked)

        managed = _make_role(12, "BotRole", discord.Permissions.none(), managed=True)
        self.assertTrue(inspect_self_role(managed).is_blocked)

    async def test_admin_panel_self_role_setting_flows(self) -> None:
        bot = YajuBot(BotConfig(token="dummy"), storage=ProxyStorage(":memory:"))
        try:
            guild_id = 100
            cosmetic = _make_role(10, "色ロール", discord.Permissions.none())
            mild = _make_role(20, "発言ロール", discord.Permissions(send_messages=True))
            high = _make_role(30, "BANロール", discord.Permissions(ban_members=True))

            guild = MagicMock(spec=discord.Guild)
            guild.id = guild_id
            guild.get_role.side_effect = lambda rid: {
                10: cosmetic,
                20: mild,
                30: high,
            }.get(rid)

            owner = SimpleNamespace(
                id=OWNER_USER_ID, roles=[], guild_permissions=discord.Permissions.none()
            )

            # 1. AdminPanelView から self_roles へ遷移
            panel = AdminPanelView(bot, guild_id)
            inter = MagicMock(spec=discord.Interaction)
            inter.user = owner
            inter.guild = guild
            inter.response = MagicMock()
            inter.response.edit_message = AsyncMock()
            sel = MagicMock(spec=discord.ui.Select)
            sel.values = ["self_roles"]
            await AdminPanelView.menu_select(panel, inter, sel)
            self.assertIsInstance(
                inter.response.edit_message.call_args.kwargs["view"],
                SelfRoleSettingView,
            )

            # 2. コスメロールのみなら即座に保存
            setting_view = SelfRoleSettingView(bot, guild_id)
            inter2 = MagicMock(spec=discord.Interaction)
            inter2.user = owner
            inter2.guild = guild
            inter2.data = {"values": ["10"]}
            inter2.response = MagicMock()
            inter2.response.edit_message = AsyncMock()
            inter2.response.send_message = AsyncMock()
            role_sel = MagicMock(spec=discord.ui.RoleSelect)
            role_sel.values = [cosmetic]
            await SelfRoleSettingView.self_role_select(setting_view, inter2, role_sel)
            self.assertEqual(
                bot.storage.get_guild_settings(guild_id).self_assignable_role_ids,
                (10,),
            )

            # 3. 権限付きロール（非高度）は警告画面に遷移し、まだ保存されない
            inter3 = MagicMock(spec=discord.Interaction)
            inter3.user = owner
            inter3.guild = guild
            inter3.data = {"values": ["10", "20"]}
            inter3.response = MagicMock()
            inter3.response.edit_message = AsyncMock()
            inter3.response.send_message = AsyncMock()
            role_sel3 = MagicMock(spec=discord.ui.RoleSelect)
            role_sel3.values = [cosmetic, mild]
            await SelfRoleSettingView.self_role_select(setting_view, inter3, role_sel3)
            confirm_view = inter3.response.edit_message.call_args.kwargs["view"]
            self.assertIsInstance(confirm_view, SelfRoleConfirmView)
            # まだ反映されていないこと
            self.assertEqual(
                bot.storage.get_guild_settings(guild_id).self_assignable_role_ids,
                (10,),
            )

            # キャンセル押下時は保存されない
            inter_cancel = MagicMock(spec=discord.Interaction)
            inter_cancel.user = owner
            inter_cancel.guild = guild
            inter_cancel.response = MagicMock()
            inter_cancel.response.edit_message = AsyncMock()
            await SelfRoleConfirmView.cancel_button(
                confirm_view, inter_cancel, MagicMock(spec=discord.ui.Button)
            )
            self.assertEqual(
                bot.storage.get_guild_settings(guild_id).self_assignable_role_ids,
                (10,),
            )

            # 続行押下時は保存される
            inter_confirm = MagicMock(spec=discord.Interaction)
            inter_confirm.user = owner
            inter_confirm.guild = guild
            inter_confirm.response = MagicMock()
            inter_confirm.response.edit_message = AsyncMock()
            await SelfRoleConfirmView.confirm_button(
                confirm_view, inter_confirm, MagicMock(spec=discord.ui.Button)
            )
            self.assertEqual(
                bot.storage.get_guild_settings(guild_id).self_assignable_role_ids,
                (10, 20),
            )

            # 4. 高度な権限を持つロールは続行不可で拒否される
            inter4 = MagicMock(spec=discord.Interaction)
            inter4.user = owner
            inter4.guild = guild
            inter4.data = {"values": ["30"]}
            inter4.response = MagicMock()
            inter4.response.edit_message = AsyncMock()
            inter4.response.send_message = AsyncMock()
            role_sel4 = MagicMock(spec=discord.ui.RoleSelect)
            role_sel4.values = [high]
            await SelfRoleSettingView.self_role_select(setting_view, inter4, role_sel4)
            inter4.response.send_message.assert_awaited_once()
            inter4.response.edit_message.assert_not_called()
            self.assertEqual(
                bot.storage.get_guild_settings(guild_id).self_assignable_role_ids,
                (10, 20),
            )
        finally:
            await bot.close()

    async def test_user_can_toggle_only_originally_held_roles(self) -> None:
        bot = YajuBot(BotConfig(token="dummy"), storage=ProxyStorage(":memory:"))
        try:
            guild_id = 200
            role_a = _make_role(101, "RoleA", discord.Permissions.none(), position=2)
            role_b = _make_role(102, "RoleB", discord.Permissions.none(), position=1)
            bot.storage.set_self_assignable_roles(guild_id, [101, 102])

            guild = MagicMock(spec=discord.Guild)
            guild.id = guild_id
            guild.get_role.side_effect = lambda rid: {101: role_a, 102: role_b}.get(rid)

            member = MagicMock(spec=discord.Member)
            member.id = 555
            member.roles = [role_a]  # 元々 RoleA のみ所持、RoleB は未所持
            member.add_roles = AsyncMock()
            member.remove_roles = AsyncMock()

            eligible, _ = resolve_user_eligible_roles(bot.storage, guild, member)
            self.assertEqual([r.id for r in eligible], [101])

            # /role で RoleA を外し、不正に RoleB を選択しても RoleB は付かない
            toggle_view = SelfRoleToggleView(
                bot, guild_id, member.id, eligible, {101}
            )
            inter = MagicMock(spec=discord.Interaction)
            inter.guild = guild
            inter.user = member
            inter.data = {"values": ["102"]}  # RoleA を外し、RoleB を付けようとする
            inter.response = MagicMock()
            inter.response.edit_message = AsyncMock()
            inter.response.send_message = AsyncMock()

            await toggle_view._on_select(inter)
            member.remove_roles.assert_awaited_once_with(
                role_a, reason="/role によるロール解除"
            )
            member.add_roles.assert_not_called()

            # RoleA を外した後も再度付け直せること
            member.roles = []
            eligible_after, _ = resolve_user_eligible_roles(bot.storage, guild, member)
            self.assertEqual([r.id for r in eligible_after], [101])

            member.remove_roles.reset_mock()
            member.add_roles.reset_mock()
            toggle_view2 = SelfRoleToggleView(
                bot, guild_id, member.id, eligible_after, set()
            )
            inter2 = MagicMock(spec=discord.Interaction)
            inter2.guild = guild
            inter2.user = member
            inter2.data = {"values": ["101"]}
            inter2.response = MagicMock()
            inter2.response.edit_message = AsyncMock()
            await toggle_view2._on_select(inter2)
            member.add_roles.assert_awaited_once_with(
                role_a, reason="/role によるロール付与"
            )

            # RolesCog の登録確認 & 未登録時はエフェメラルで簡潔に通知
            cog = RolesCog(bot)
            self.assertEqual(cog.role_panel.name, "role")

            unconfigured_guild = MagicMock(spec=discord.Guild)
            unconfigured_guild.id = 999
            inter_unconf = MagicMock(spec=discord.Interaction)
            inter_unconf.guild = unconfigured_guild
            inter_unconf.user = member
            inter_unconf.response = MagicMock()
            inter_unconf.response.send_message = AsyncMock()
            await cog.role_panel.callback(cog, inter_unconf)
            inter_unconf.response.send_message.assert_awaited_once_with(
                "変更できるロールが設定されていないゾ。",
                ephemeral=True,
            )
        finally:
            await bot.close()

    async def test_unencrypted_role_backup_and_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "yajubot.db"
            backup_path = Path(tmpdir) / "role_toggle_backup.json"
            storage = ProxyStorage(db_path, role_backup_path=backup_path)
            try:
                storage.set_self_assignable_roles(300, [101, 102])
                storage.record_user_toggleable_roles(300, 777, [101])
                self.assertTrue(backup_path.exists())

                raw = json.loads(backup_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    raw["guilds"]["300"]["self_assignable_role_ids"], [101, 102]
                )
                self.assertEqual(raw["guilds"]["300"]["users"]["777"], [101])
            finally:
                storage.close()

            # DB 破損・削除をシミュレートして新規 DB を作成すると自動復元される
            db_path.unlink()
            restored_storage = ProxyStorage(db_path, role_backup_path=backup_path)
            try:
                self.assertEqual(
                    restored_storage.get_guild_settings(300).self_assignable_role_ids,
                    (101, 102),
                )
                self.assertEqual(
                    restored_storage.get_user_toggleable_roles(300, 777),
                    {101},
                )
            finally:
                restored_storage.close()


if __name__ == "__main__":
    unittest.main()
