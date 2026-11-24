"""ユーザーが自身のロールを付け外しできる機能（/role）とロール権限検査を提供する Cog。"""

from __future__ import annotations

from dataclasses import dataclass, field

import discord
from discord import app_commands
from discord.ext import commands

from yajubot.bot import YajuBot
from yajubot.storage import ProxyStorage

HIGH_RISK_PERMISSION_LABELS: dict[str, str] = {
    "administrator": "管理者",
    "ban_members": "メンバーをBAN",
    "kick_members": "メンバーをキック",
    "moderate_members": "メンバーをタイムアウト",
    "manage_channels": "チャンネルの管理",
    "manage_guild": "サーバー管理",
    "manage_roles": "ロールの管理",
    "manage_webhooks": "ウェブフックの管理",
    "manage_messages": "メッセージの管理",
    "manage_threads": "スレッドの管理",
    "manage_nicknames": "ニックネームの管理",
    "manage_expressions": "絵文字・スタンプ等の管理",
    "manage_events": "イベントの管理",
    "mention_everyone": "@everyone・@here・全ロールへのメンション",
    "view_audit_log": "監査ログの表示",
    "view_guild_insights": "サーバーインサイトの表示",
    "mute_members": "メンバーをミュート",
    "deafen_members": "メンバーのスピーカーをミュート",
    "move_members": "メンバーを移動",
}


@dataclass(slots=True)
class RolePermissionInspection:
    """ロール付け外し対象としての安全性検査結果。"""

    blocked_reasons: list[str] = field(default_factory=list)
    warning_permissions: list[str] = field(default_factory=list)

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocked_reasons)

    @property
    def requires_confirmation(self) -> bool:
        return not self.blocked_reasons and bool(self.warning_permissions)


def inspect_self_role(role: discord.Role) -> RolePermissionInspection:
    """ロールが自己付け外し対象として安全か検査する。"""
    result = RolePermissionInspection()
    if role.is_default():
        result.blocked_reasons.append("@everyone ロールは設定できないゾ。")
    if role.managed:
        result.blocked_reasons.append(
            "Bot・連携等で自動管理されているロールは設定できないゾ。"
        )

    high_perms = [
        label
        for flag, label in HIGH_RISK_PERMISSION_LABELS.items()
        if getattr(role.permissions, flag, False)
    ]
    if high_perms:
        result.blocked_reasons.append(
            f"高度な権限（{', '.join(high_perms)}）が付与されているため設定できないゾ。"
        )

    if not result.blocked_reasons and role.permissions.value != 0:
        enabled = [
            perm
            for perm, is_enabled in role.permissions
            if is_enabled and perm != "manage_emojis_and_stickers"
        ]
        if enabled:
            result.warning_permissions.extend(enabled)
        else:
            result.warning_permissions.append(f"permissions={role.permissions.value}")

    return result


def resolve_user_eligible_roles(
    storage: ProxyStorage,
    guild: discord.Guild,
    member: discord.Member,
) -> tuple[list[discord.Role], set[int]]:
    """ユーザーが付け外し可能なロール一覧と現在所持ロール ID 集合を返す。

    対象は「管理者が許可したロール」かつ「ユーザーが現在所持または過去に所持していたロール」のみ。
    """
    settings = storage.get_guild_settings(guild.id)
    valid_roles: dict[int, discord.Role] = {}
    for rid in settings.self_assignable_role_ids:
        role = guild.get_role(rid)
        if role is not None and not inspect_self_role(role).is_blocked:
            valid_roles[rid] = role

    current_member_role_ids = {r.id for r in member.roles}
    owned_now = set(valid_roles.keys()) & current_member_role_ids
    if owned_now:
        storage.record_user_toggleable_roles(guild.id, member.id, owned_now)

    recorded_ids = storage.get_user_toggleable_roles(guild.id, member.id)
    eligible_ids = set(valid_roles.keys()) & (current_member_role_ids | recorded_ids)

    eligible_roles = sorted(
        (valid_roles[rid] for rid in eligible_ids),
        key=lambda r: r.position,
        reverse=True,
    )
    return eligible_roles, current_member_role_ids


def build_user_role_panel_embed(
    eligible_roles: list[discord.Role],
    current_role_ids: set[int],
    *,
    status_note: str | None = None,
) -> discord.Embed:
    """ユーザー向けロール付け外しパネルの Embed を生成する。"""
    embed = discord.Embed(
        title="🏷️ ロール付け外し",
        description=(
            "元々所持している対象ロールのみ、下の選択メニューから付け外しできるゾ。\n"
            "付けたいロールにチェックを入れ、外したいロールはチェックを外して送信してくれよな～"
        ),
        color=discord.Color.blurple(),
    )
    lines = [
        f"{'✅' if role.id in current_role_ids else '⬜'} {role.mention}"
        for role in eligible_roles
    ]
    embed.add_field(
        name="付け外し可能なロール",
        value="\n".join(lines) if lines else "なし",
        inline=False,
    )
    if status_note:
        embed.add_field(name="更新結果", value=status_note, inline=False)
    return embed


class SelfRoleToggleView(discord.ui.View):
    """ユーザーが自身の許可済みロールを付け外しする View。"""

    def __init__(
        self,
        bot: YajuBot,
        guild_id: int,
        user_id: int,
        eligible_roles: list[discord.Role],
        current_role_ids: set[int],
    ) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.guild_id = guild_id
        self.user_id = user_id

        options = [
            discord.SelectOption(
                label=role.name[:100],
                value=str(role.id),
                default=role.id in current_role_ids,
            )
            for role in eligible_roles[:25]
        ]
        select = discord.ui.Select(
            placeholder="付け外しするロールを選択してください",
            min_values=0,
            max_values=len(options),
            options=options,
            row=0,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "サーバー内のチャンネルでのみ利用できるゾ。", ephemeral=True
            )
            return
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "他のユーザーのパネルは操作できないゾ。", ephemeral=True
            )
            return

        member = interaction.user
        eligible_roles, current_role_ids = resolve_user_eligible_roles(
            self.bot.storage, guild, member
        )
        if not eligible_roles:
            await interaction.response.edit_message(
                content="付け外し可能なロールが現在ないゾ。",
                embed=None,
                view=None,
            )
            return

        eligible_map = {r.id: r for r in eligible_roles}
        selected_ids = {
            int(v) for v in interaction.data.get("values", [])  # type: ignore[union-attr]
        } & set(eligible_map.keys())

        currently_has = current_role_ids & set(eligible_map.keys())
        to_add = [eligible_map[rid] for rid in (selected_ids - currently_has)]
        to_remove = [eligible_map[rid] for rid in (currently_has - selected_ids)]

        notes: list[str] = []
        try:
            if to_add:
                await member.add_roles(*to_add, reason="/role によるロール付与")
                notes.append("付与: " + " ".join(r.mention for r in to_add))
            if to_remove:
                await member.remove_roles(*to_remove, reason="/role によるロール解除")
                notes.append("解除: " + " ".join(r.mention for r in to_remove))
            if not to_add and not to_remove:
                notes.append("変更はなかったゾ。")
        except discord.Forbidden:
            await interaction.response.send_message(
                "Bot の権限不足、またはロール順位が Bot より高いため変更できなかったゾ。",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            await interaction.response.send_message(
                "ロールの更新中にエラーが発生したゾ。時間をおいて再度試してくれよな～",
                ephemeral=True,
            )
            return

        updated_role_ids = (current_role_ids - {r.id for r in to_remove}) | {
            r.id for r in to_add
        }
        new_view = SelfRoleToggleView(
            self.bot,
            guild.id,
            member.id,
            eligible_roles,
            updated_role_ids,
        )
        await interaction.response.edit_message(
            embed=build_user_role_panel_embed(
                eligible_roles,
                updated_role_ids,
                status_note="\n".join(notes),
            ),
            view=new_view,
        )


@app_commands.guild_only()
class RolesCog(commands.Cog):
    """ユーザー自身のロール付け外しコマンドを提供する Cog。"""

    def __init__(self, bot: YajuBot) -> None:
        self.bot = bot

    @app_commands.command(
        name="role",
        description="元々持っている対象ロールを自分で付け外しするゾ",
    )
    async def role_panel(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "サーバー内のチャンネルでのみ利用できるゾ。", ephemeral=True
            )
            return

        settings = self.bot.storage.get_guild_settings(guild.id)
        if not settings.self_assignable_role_ids:
            await interaction.response.send_message(
                "変更できるロールが設定されていないゾ。",
                ephemeral=True,
            )
            return

        eligible_roles, current_role_ids = resolve_user_eligible_roles(
            self.bot.storage, guild, interaction.user
        )
        if not eligible_roles:
            await interaction.response.send_message(
                "変更できるロールが設定されていないゾ。",
                ephemeral=True,
            )
            return

        view = SelfRoleToggleView(
            self.bot,
            guild.id,
            interaction.user.id,
            eligible_roles,
            current_role_ids,
        )
        await interaction.response.send_message(
            embed=build_user_role_panel_embed(eligible_roles, current_role_ids),
            view=view,
            ephemeral=True,
        )


async def setup(bot: YajuBot) -> None:
    await bot.add_cog(RolesCog(bot))
