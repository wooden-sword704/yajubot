"""管理者設定（ロール制限）・送信者照会・暗号化削除ログ閲覧を提供する Cog。"""

from __future__ import annotations

import logging
import re
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from yajubot.bot import YajuBot
from yajubot.cogs.proxy import can_manage_bot
from yajubot.storage import GuildSettings, OWNER_USER_ID, ProxyEditRecord, ProxyMessageRecord

logger = logging.getLogger(__name__)

MESSAGE_LINK_OR_ID_PATTERN = re.compile(
    r"^(?:https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/\d+/\d+/)?(?P<id>\d+)/?$"
)
LOGS_PER_PAGE = 5


def parse_message_id(raw: str) -> int | None:
    """メッセージ ID または Discord メッセージリンクからメッセージ ID を抽出する。"""
    if not raw:
        return None
    m = MESSAGE_LINK_OR_ID_PATTERN.match(raw.strip())
    if not m:
        return None
    return int(m.group("id"))


def build_settings_embed(settings: GuildSettings) -> discord.Embed:
    """現在のギルド権限設定をまとめた Embed を生成する。"""
    proxy_roles_text = (
        " ".join(f"<@&{rid}>" for rid in settings.proxy_role_ids)
        if settings.proxy_role_ids
        else "未設定（管理者・管理ロール・例外アカウントのみ利用可）"
    )
    admin_roles_text = (
        " ".join(f"<@&{rid}>" for rid in settings.admin_role_ids)
        if settings.admin_role_ids
        else "未設定（サーバー管理者・例外アカウントのみ利用可）"
    )

    log_channel_text = (
        f"<#{settings.log_channel_id}> (`{settings.log_channel_id}`)"
        if settings.log_channel_id is not None
        else "未設定（編集・削除ログのチャンネル通知は無効）"
    )

    embed = discord.Embed(
        title="yajubot 管理設定パネル",
        description="ロール権限・ログチャンネルの設定、および暗号化ログからの送信者照会・削除ログ閲覧が行えます。",
        color=discord.Color.gold(),
    )
    embed.add_field(name="発信・返信 許可ロール", value=proxy_roles_text, inline=False)
    embed.add_field(name="Bot管理（/admin・/proxy）許可ロール", value=admin_roles_text, inline=False)
    embed.add_field(name="編集・削除ログチャンネル", value=log_channel_text, inline=False)
    embed.add_field(
        name="例外管理者アカウント",
        value=f"<@{OWNER_USER_ID}> (`{OWNER_USER_ID}`)",
        inline=False,
    )
    return embed


def build_lookup_embed(
    record: ProxyMessageRecord,
    edit_history: list[ProxyEditRecord] | None = None,
) -> discord.Embed:
    """メッセージ照会結果の Embed を生成する。"""
    status_text = f"🗑️ 削除済み（{record.deleted_at or '日時不明'}）" if record.is_deleted else "✅ 存在中"
    embed = discord.Embed(
        title="送信者・メッセージログ照会結果",
        color=discord.Color.red() if record.is_deleted else discord.Color.green(),
    )
    embed.add_field(name="送信者", value=f"<@{record.author_id}> (`{record.author_id}`)", inline=True)
    embed.add_field(name="状態", value=status_text, inline=True)
    embed.add_field(name="チャンネル", value=f"<#{record.channel_id}> (`{record.channel_id}`)", inline=True)
    embed.add_field(name="送信日時", value=record.created_at, inline=True)
    if record.edited_at:
        embed.add_field(name="最終編集", value=record.edited_at, inline=True)
    if record.reply_target_id:
        embed.add_field(name="返信先メッセージ ID", value=f"`{record.reply_target_id}`", inline=True)

    snippet = record.content if len(record.content) <= 1000 else record.content[:997] + "..."
    embed.add_field(
        name="記録された内容（最新・復号済み）",
        value=snippet if snippet else "(空)",
        inline=False,
    )

    if edit_history:
        history_lines: list[str] = []
        for idx, hist in enumerate(edit_history[:5], start=1):
            old_s = " ".join(hist.old_content.split())
            new_s = " ".join(hist.new_content.split())
            if len(old_s) > 80:
                old_s = old_s[:77] + "..."
            if len(new_s) > 80:
                new_s = new_s[:77] + "..."
            history_lines.append(
                f"**{idx}. [{hist.edited_at}]**\n前: {old_s or '(空)'}\n後: {new_s or '(空)'}"
            )
        history_value = "\n".join(history_lines)
        if len(history_value) > 1024:
            history_value = history_value[:1021] + "..."
        embed.add_field(
            name=f"✏️ 編集履歴（最新 {min(len(edit_history), 5)} 件）",
            value=history_value,
            inline=False,
        )

    embed.set_footer(text=f"メッセージ ID: {record.proxy_msg_id}")
    return embed


def build_deleted_logs_embed(
    records: list[ProxyMessageRecord],
    *,
    page: int,
    total_count: int,
) -> discord.Embed:
    """削除済みプロキシメッセージの一覧 Embed を生成する。"""
    total_pages = max(1, (total_count + LOGS_PER_PAGE - 1) // LOGS_PER_PAGE)
    embed = discord.Embed(
        title="🗑️ 削除済みプロキシメッセージログ",
        description=f"全 {total_count} 件中（ページ {page + 1} / {total_pages}）",
        color=discord.Color.dark_red(),
    )
    if not records:
        embed.description = "削除されたプロキシメッセージの記録はまだないゾ。"
        return embed

    for rec in records:
        snippet = " ".join(rec.content.split())
        if len(snippet) > 120:
            snippet = snippet[:117] + "..."
        embed.add_field(
            name=f"ID: {rec.proxy_msg_id} | 削除: {rec.deleted_at or '不明'}",
            value=(
                f"**送信者**: <@{rec.author_id}> (`{rec.author_id}`) | <#{rec.channel_id}>\n"
                f"**内容**: {snippet or '(空)'}"
            ),
            inline=False,
        )
    return embed


class LookupMessageModal(discord.ui.Modal, title="メッセージIDから送信者を確認"):
    """メッセージ ID（またはリンク）を入力して送信者・内容を照会するモーダル。"""

    msg_id_input = discord.ui.TextInput(
        label="メッセージ ID またはリンク",
        placeholder="例: 1234567890123456789",
        required=True,
        max_length=120,
    )

    def __init__(self, bot: YajuBot) -> None:
        super().__init__(timeout=180)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or not can_manage_bot(interaction.user, self.bot.storage, guild.id):
            await interaction.response.send_message(
                "この操作を実行する権限がないゾ。", ephemeral=True
            )
            return

        msg_id = parse_message_id(self.msg_id_input.value or "")
        if msg_id is None:
            await interaction.response.send_message(
                "有効なメッセージ ID またはメッセージリンクを入力してくれよな～",
                ephemeral=True,
            )
            return

        record = self.bot.storage.get_proxy_message(msg_id)
        if record is None or record.guild_id != guild.id:
            await interaction.response.send_message(
                f"メッセージ ID `{msg_id}` のプロキシ送信記録が見つからなかったゾ。",
                ephemeral=True,
            )
            return

        edit_history = self.bot.storage.get_edit_history(msg_id, limit=5)
        await interaction.response.send_message(
            embed=build_lookup_embed(record, edit_history=edit_history),
            ephemeral=True,
        )


class DeletedLogsView(discord.ui.View):
    """削除済みログをページ切り替えしながら閲覧する View。"""

    def __init__(self, bot: YajuBot, guild_id: int, *, page: int = 0) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.guild_id = guild_id
        self.page = page
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        total = self.bot.storage.count_deleted_logs(self.guild_id)
        total_pages = max(1, (total + LOGS_PER_PAGE - 1) // LOGS_PER_PAGE)
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= total_pages - 1

    def build_current_embed(self) -> discord.Embed:
        total = self.bot.storage.count_deleted_logs(self.guild_id)
        records = self.bot.storage.get_deleted_logs(
            self.guild_id, limit=LOGS_PER_PAGE, offset=self.page * LOGS_PER_PAGE
        )
        return build_deleted_logs_embed(records, page=self.page, total_count=total)

    @discord.ui.button(label="◀ 前へ", style=discord.ButtonStyle.secondary)
    async def prev_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        self.page = max(0, self.page - 1)
        self._sync_buttons()
        await interaction.response.edit_message(
            embed=self.build_current_embed(), view=self
        )

    @discord.ui.button(label="次へ ▶", style=discord.ButtonStyle.secondary)
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        total = self.bot.storage.count_deleted_logs(self.guild_id)
        max_page = max(0, (total - 1) // LOGS_PER_PAGE)
        self.page = min(max_page, self.page + 1)
        self._sync_buttons()
        await interaction.response.edit_message(
            embed=self.build_current_embed(), view=self
        )


class AdminPanelView(discord.ui.View):
    """管理設定パネル（ロール設定・ログ確認）の View。"""

    def __init__(self, bot: YajuBot, guild_id: int) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="発信・返信を許可するロールを選択（未選択でリセット）",
        min_values=0,
        max_values=10,
        row=0,
    )
    async def proxy_role_select(
        self, interaction: discord.Interaction, select: discord.ui.RoleSelect
    ) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        role_ids = [r.id for r in select.values]
        settings = self.bot.storage.set_proxy_roles(self.guild_id, role_ids)
        await interaction.response.edit_message(
            embed=build_settings_embed(settings), view=self
        )

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="Bot管理(/admin・/proxy)を許可するロールを選択",
        min_values=0,
        max_values=10,
        row=1,
    )
    async def admin_role_select(
        self, interaction: discord.Interaction, select: discord.ui.RoleSelect
    ) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        role_ids = [r.id for r in select.values]
        settings = self.bot.storage.set_admin_roles(self.guild_id, role_ids)
        await interaction.response.edit_message(
            embed=build_settings_embed(settings), view=self
        )

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[
            discord.ChannelType.text,
            discord.ChannelType.public_thread,
            discord.ChannelType.private_thread,
        ],
        placeholder="編集・削除ログを送信するチャンネルを選択（未選択で解除）",
        min_values=0,
        max_values=1,
        row=2,
    )
    async def log_channel_select(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        channel_id = select.values[0].id if select.values else None
        settings = self.bot.storage.set_log_channel(self.guild_id, channel_id)
        await interaction.response.edit_message(
            embed=build_settings_embed(settings), view=self
        )

    @discord.ui.button(
        label="IDから送信者を確認", emoji="🔍", style=discord.ButtonStyle.primary, row=3
    )
    async def lookup_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        await interaction.response.send_modal(LookupMessageModal(self.bot))

    @discord.ui.button(
        label="削除ログを掘る", emoji="🗑️", style=discord.ButtonStyle.danger, row=3
    )
    async def deleted_logs_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        view = DeletedLogsView(self.bot, self.guild_id)
        await interaction.response.send_message(
            embed=view.build_current_embed(), view=view, ephemeral=True
        )


@app_commands.guild_only()
class AdminCog(commands.GroupCog, group_name="admin"):
    """管理者向けのロール設定・暗号化ログ確認コマンド群。"""

    def __init__(self, bot: YajuBot) -> None:
        self.bot = bot

    async def _ensure_admin(self, interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "サーバー内のチャンネルでのみ利用できるゾ。", ephemeral=True
            )
            return False
        if not can_manage_bot(interaction.user, self.bot.storage, guild.id):
            await interaction.response.send_message(
                "このコマンドは管理者または許可された管理ロールのみ実行できるゾ。",
                ephemeral=True,
            )
            return False
        return True

    @app_commands.command(
        name="panel",
        description="設定用パネル。多分管理者専用だと思うんですけど（名推理）",
    )
    async def admin_panel(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_admin(interaction):
            return
        assert interaction.guild is not None
        settings = self.bot.storage.get_guild_settings(interaction.guild.id)
        view = AdminPanelView(self.bot, interaction.guild.id)
        await interaction.response.send_message(
            embed=build_settings_embed(settings), view=view, ephemeral=True
        )

    @app_commands.command(
        name="lookup",
        description="メッセージ照会。多分管理者専用だと思うんですけど（名推理）",
    )
    @app_commands.describe(message_id="確認したいメッセージの ID またはメッセージリンク")
    async def admin_lookup(
        self, interaction: discord.Interaction, message_id: str
    ) -> None:
        if not await self._ensure_admin(interaction):
            return
        assert interaction.guild is not None
        parsed_id = parse_message_id(message_id)
        if parsed_id is None:
            await interaction.response.send_message(
                "有効なメッセージ ID またはメッセージリンクを指定してくれよな～",
                ephemeral=True,
            )
            return

        record = self.bot.storage.get_proxy_message(parsed_id)
        if record is None or record.guild_id != interaction.guild.id:
            await interaction.response.send_message(
                f"メッセージ ID `{parsed_id}` のプロキシ送信記録が見つからなかったゾ。",
                ephemeral=True,
            )
            return

        edit_history = self.bot.storage.get_edit_history(parsed_id, limit=5)
        await interaction.response.send_message(
            embed=build_lookup_embed(record, edit_history=edit_history),
            ephemeral=True,
        )

    @app_commands.command(
        name="log_channel",
        description="ログチャンネル設定。多分管理者専用だと思うんですけど（名推理）",
    )
    @app_commands.describe(channel="ログを送信するチャンネル（省略時は解除）")
    async def admin_log_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | discord.Thread | None = None,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return
        assert interaction.guild is not None
        channel_id = channel.id if channel is not None else None
        self.bot.storage.set_log_channel(interaction.guild.id, channel_id)
        if channel is not None:
            await interaction.response.send_message(
                f"編集・削除ログの送信先を <#{channel.id}> に設定したゾ。",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "編集・削除ログのチャンネル送信を解除したゾ。",
                ephemeral=True,
            )

    @app_commands.command(
        name="deleted_logs",
        description="削除されたプロキシメッセージの暗号化ログを閲覧します",
    )
    async def admin_deleted_logs(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_admin(interaction):
            return
        assert interaction.guild is not None
        view = DeletedLogsView(self.bot, interaction.guild.id)
        await interaction.response.send_message(
            embed=view.build_current_embed(), view=view, ephemeral=True
        )


async def setup(bot: YajuBot) -> None:
    await bot.add_cog(AdminCog(bot))
