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
from yajubot.cogs.roles import inspect_self_role
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


ROLE_SETTING_CONFIG: dict[str, dict[str, str]] = {
    "proxy": {
        "attr": "proxy_role_ids",
        "field_name": "発信・返信 許可ロール",
        "empty_text": "未設定（管理者・管理ロールのみ利用可）",
        "title": "🔐 発信・返信 許可ロール設定",
        "description": (
            "身代わり送信・返信（`/proxy`・`/say`・返信ボタン）を利用できるロールを設定するゾ。\n"
            "下の選択メニューから許可したいロールを選んでくれよな～"
        ),
        "placeholder": "発信・返信を許可するロールを選択",
    },
    "admin": {
        "attr": "admin_role_ids",
        "field_name": "Bot管理（/admin・/proxy）許可ロール",
        "empty_text": "未設定（サーバー管理者のみ利用可）",
        "title": "🛡️ Bot管理（/admin・/proxy）許可ロール設定",
        "description": (
            "管理パネル（`/admin panel`）や代理送信管理などの Bot 管理機能を利用できるロールを設定するゾ。\n"
            "下の選択メニューから許可したいロールを選んでくれよな～"
        ),
        "placeholder": "Bot管理(/admin・/proxy)を許可するロールを選択",
    },
    "stage_mod": {
        "attr": "stage_mod_role_ids",
        "field_name": "ステージモデレーターロール",
        "empty_text": "未設定（Bot管理者のみ承認・拒否可能）",
        "title": "🎙️ ステージモデレーターロール設定",
        "description": (
            "ステージチャンネルのスピーカーリクエストを承認・拒否できるモデレーターロールを設定するゾ。\n"
            "下の選択メニューから許可したいロールを選んでくれよな～"
        ),
        "placeholder": "ステージモデレーターロールを選択",
    },
}


def _format_roles(role_ids: list[int], empty_text: str) -> str:
    return " ".join(f"<@&{rid}>" for rid in role_ids) if role_ids else empty_text


def _format_log_channel(channel_id: int | None) -> str:
    return (
        f"<#{channel_id}> (`{channel_id}`)"
        if channel_id is not None
        else "未設定（編集・削除ログのチャンネル通知は無効）"
    )


def build_settings_embed(settings: GuildSettings) -> discord.Embed:
    """現在のギルド権限設定をまとめた Embed を生成する。"""
    embed = discord.Embed(
        title="yajubot 管理設定パネル",
        description="ロール権限・ログチャンネルの設定、および暗号化ログからの送信者照会・削除ログ閲覧が行えるゾ",
        color=discord.Color.gold(),
    )
    for cfg in ROLE_SETTING_CONFIG.values():
        role_ids = getattr(settings, cfg["attr"])
        embed.add_field(
            name=cfg["field_name"],
            value=_format_roles(role_ids, cfg["empty_text"]),
            inline=False,
        )
    embed.add_field(
        name="編集・削除ログチャンネル",
        value=_format_log_channel(settings.log_channel_id),
        inline=False,
    )
    embed.add_field(
        name="付け外し可能ロール（/role）",
        value=_format_roles(
            list(settings.self_assignable_role_ids),
            "未設定（ユーザーによるロール付け外しは無効）",
        ),
        inline=False,
    )
    excluded_text = (
        ", ".join(f"<#{cid}>" for cid in settings.tadocoin_excluded_channel_ids)
        if settings.tadocoin_excluded_channel_ids
        else "未設定（全チャンネルでチャット獲得有効）"
    )
    embed.add_field(
        name="タドコイン獲得例外チャンネル",
        value=excluded_text,
        inline=False,
    )
    return embed


def build_role_setting_embed(settings: GuildSettings, mode: str) -> discord.Embed:
    """各ロール設定画面用の説明付き Embed を生成する。"""
    cfg = ROLE_SETTING_CONFIG[mode]
    role_ids = getattr(settings, cfg["attr"])
    embed = discord.Embed(
        title=cfg["title"],
        description=cfg["description"],
        color=discord.Color.gold(),
    )
    embed.add_field(
        name=f"現在の設定（{cfg['field_name']}）",
        value=_format_roles(role_ids, cfg["empty_text"]),
        inline=False,
    )
    return embed


def build_log_channel_setting_embed(settings: GuildSettings) -> discord.Embed:
    """編集・削除ログチャンネル設定画面用の説明付き Embed を生成する。"""
    embed = discord.Embed(
        title="📝 編集・削除ログチャンネル設定",
        description=(
            "プロキシメッセージが編集・削除された際に通知を送るチャンネルを設定するゾ。\n"
            "下の選択メニューから送信先のテキストチャンネルまたはスレッドを選んでくれよな～"
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="現在の設定（編集・削除ログチャンネル）",
        value=_format_log_channel(settings.log_channel_id),
        inline=False,
    )
    return embed


def build_self_role_setting_embed(settings: GuildSettings) -> discord.Embed:
    """ロール付け外し設定画面用の説明付き Embed を生成する。"""
    embed = discord.Embed(
        title="🏷️ ロール付け外し設定（/role）",
        description=(
            "ユーザーが `/role` で付け外しできるロールを設定するゾ（元々ユーザーに付いているロールのみ付け外し可能）。\n"
            "下の選択メニューから許可したいロールを選んでくれよな～（未選択で送信するとリセットされるゾ）\n"
            "※ 権限付きロールは警告が表示され、高度な権限（管理者・BAN・キック・TO・チャンネル/サーバー管理等）を持つロールは設定できないゾ。"
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="現在の設定（付け外し可能ロール）",
        value=_format_roles(
            list(settings.self_assignable_role_ids),
            "未設定（ユーザーによるロール付け外しは無効）",
        ),
        inline=False,
    )
    return embed


def build_self_role_warning_embed(
    warning_roles: list[tuple[discord.Role, list[str]]],
) -> discord.Embed:
    """権限付きロールを設定しようとした際の確認警告 Embed を生成する。"""
    embed = discord.Embed(
        title="⚠️ 権限付きロールが含まれています",
        description=(
            "選択されたロールにコスメロール以外の権限が付与されているゾ。\n"
            "誤って重要なロールを付け外し対象にしないよう確認してくれよな～\n"
            "このまま設定を反映する場合は「続行して設定を反映」を押してくれゾ。"
        ),
        color=discord.Color.orange(),
    )
    for role, perms in warning_roles[:10]:
        perm_text = ", ".join(perms[:10])
        if len(perms) > 10:
            perm_text += f" ほか {len(perms) - 10} 件"
        embed.add_field(
            name=f"ロール: {role.name}",
            value=f"{role.mention}\n付与権限: `{perm_text}`",
            inline=False,
        )
    return embed


def build_tadocoin_excluded_embed(settings: GuildSettings) -> discord.Embed:
    """タドコイン獲得例外チャンネル設定画面用の Embed を生成する。"""
    excluded_text = (
        ", ".join(f"<#{cid}>" for cid in settings.tadocoin_excluded_channel_ids)
        if settings.tadocoin_excluded_channel_ids
        else "未設定（全チャンネルでチャット獲得有効）"
    )
    embed = discord.Embed(
        title="<:tadocoin:1575197619569958942> タドコイン獲得例外チャンネル設定",
        description=(
            "発言してもタドコインが増えない例外チャンネルを設定するゾ。\n"
            "下の選択メニューから例外にしたいチャンネルを選んでくれよな～（未選択で解除）"
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(name="現在の例外チャンネル", value=excluded_text, inline=False)
    return embed


def build_tadocoin_override_embed(
    *,
    last_updated: tuple[int, int] | None = None,
) -> discord.Embed:
    """ユーザータドコイン上書き設定画面用の Embed を生成する。"""
    embed = discord.Embed(
        title="💰 ユーザータドコイン上書き設定",
        description=(
            "指定ユーザーのタドコイン所持数を任意の数に上書きするゾ。\n"
            "下の選択メニューから対象ユーザーを選ぶと、所持数入力モーダルが開くゾ。"
        ),
        color=discord.Color.gold(),
    )
    if last_updated is not None:
        uid, amount = last_updated
        embed.add_field(
            name="直近の上書き結果",
            value=f"<@{uid}> のタドコインを **{amount:,}** に設定したゾ。",
            inline=False,
        )
    return embed


def build_sub_role_plan_embed(bot: YajuBot, guild_id: int) -> discord.Embed:
    """サブスクロール設定画面用の Embed を生成する。"""
    plans = bot.storage.get_sub_role_plans(guild_id)
    embed = discord.Embed(
        title="👑 サブスクロール設定（/subrole）",
        description=(
            "ユーザーが週額タドコインで維持できるサブスクロールを設定するゾ。\n"
            "下の選択メニューからロールを選ぶと、毎週の引き落とし額入力モーダルが開くゾ（0 を入力するとプラン解除）。"
        ),
        color=discord.Color.gold(),
    )
    if plans:
        lines = [
            f"• <@&{rid}> — **{cost:,}** <:tadocoin:1575197619569958942>/週"
            for rid, cost in plans.items()
        ]
        plan_text = "\n".join(lines)
        if len(plan_text) > 1024:
            plan_text = plan_text[:1021] + "..."
        embed.add_field(name="現在のプラン一覧", value=plan_text, inline=False)
    else:
        embed.add_field(name="現在のプラン一覧", value="未設定", inline=False)
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

    @discord.ui.button(label="◀ パネルに戻る", style=discord.ButtonStyle.primary)
    async def back_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        settings = self.bot.storage.get_guild_settings(self.guild_id)
        await interaction.response.edit_message(
            embed=build_settings_embed(settings),
            view=AdminPanelView(self.bot, self.guild_id),
        )


class RoleSettingView(discord.ui.View):
    """ロール設定用の個別サブ画面 View。"""

    def __init__(self, bot: YajuBot, guild_id: int, *, mode: str) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id
        self.mode = mode
        select = discord.ui.RoleSelect(
            placeholder=ROLE_SETTING_CONFIG[mode]["placeholder"],
            min_values=0,
            max_values=10,
            row=0,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        role_ids = [int(v) for v in interaction.data["values"]]  # type: ignore[index]
        if self.mode == "proxy":
            settings = self.bot.storage.set_proxy_roles(self.guild_id, role_ids)
        elif self.mode == "admin":
            settings = self.bot.storage.set_admin_roles(self.guild_id, role_ids)
        else:
            settings = self.bot.storage.set_stage_mod_roles(self.guild_id, role_ids)
        await interaction.response.edit_message(
            embed=build_role_setting_embed(settings, self.mode), view=self
        )

    @discord.ui.button(label="◀ パネルに戻る", style=discord.ButtonStyle.primary, row=1)
    async def back_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        settings = self.bot.storage.get_guild_settings(self.guild_id)
        await interaction.response.edit_message(
            embed=build_settings_embed(settings),
            view=AdminPanelView(self.bot, self.guild_id),
        )


class LogChannelSettingView(discord.ui.View):
    """編集・削除ログチャンネル設定用の個別サブ画面 View。"""

    def __init__(self, bot: YajuBot, guild_id: int) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[
            discord.ChannelType.text,
            discord.ChannelType.public_thread,
            discord.ChannelType.private_thread,
        ],
        placeholder="編集・削除ログを送信するチャンネルを選択",
        min_values=0,
        max_values=1,
        row=0,
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
            embed=build_log_channel_setting_embed(settings), view=self
        )

    @discord.ui.button(label="◀ パネルに戻る", style=discord.ButtonStyle.primary, row=1)
    async def back_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        settings = self.bot.storage.get_guild_settings(self.guild_id)
        await interaction.response.edit_message(
            embed=build_settings_embed(settings),
            view=AdminPanelView(self.bot, self.guild_id),
        )


class SelfRoleConfirmView(discord.ui.View):
    """権限付きロールを付け外し対象に設定する際の警告確認 View。"""

    def __init__(
        self, bot: YajuBot, guild_id: int, pending_role_ids: list[int]
    ) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.guild_id = guild_id
        self.pending_role_ids = pending_role_ids

    @discord.ui.button(
        label="続行して設定を反映", style=discord.ButtonStyle.danger, row=0
    )
    async def confirm_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        guild = interaction.guild
        if guild is not None:
            for rid in self.pending_role_ids:
                role = guild.get_role(rid)
                if role is not None and inspect_self_role(role).is_blocked:
                    await interaction.response.send_message(
                        f"{role.mention} は高度な権限または管理対象外のため設定できないゾ。",
                        ephemeral=True,
                    )
                    return

        settings = self.bot.storage.set_self_assignable_roles(
            self.guild_id, self.pending_role_ids
        )
        await interaction.response.edit_message(
            embed=build_self_role_setting_embed(settings),
            view=SelfRoleSettingView(self.bot, self.guild_id),
        )

    @discord.ui.button(
        label="キャンセル", style=discord.ButtonStyle.secondary, row=0
    )
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        settings = self.bot.storage.get_guild_settings(self.guild_id)
        await interaction.response.edit_message(
            embed=build_self_role_setting_embed(settings),
            view=SelfRoleSettingView(self.bot, self.guild_id),
        )


class SelfRoleSettingView(discord.ui.View):
    """ロール付け外し許可設定用の個別サブ画面 View。"""

    def __init__(self, bot: YajuBot, guild_id: int) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="付け外しを許可するロールを選択（未選択でリセット）",
        min_values=0,
        max_values=25,
        row=0,
    )
    async def self_role_select(
        self, interaction: discord.Interaction, select: discord.ui.RoleSelect
    ) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return

        guild = interaction.guild
        selected_roles: list[discord.Role] = list(select.values)
        if not selected_roles and guild is not None and interaction.data:
            for raw_id in interaction.data.get("values", []):
                role = guild.get_role(int(raw_id))
                if role is not None:
                    selected_roles.append(role)

        blocked_messages: list[str] = []
        warning_roles: list[tuple[discord.Role, list[str]]] = []
        for role in selected_roles:
            inspection = inspect_self_role(role)
            if inspection.is_blocked:
                blocked_messages.append(
                    f"• {role.mention}: {' / '.join(inspection.blocked_reasons)}"
                )
            elif inspection.requires_confirmation:
                warning_roles.append((role, inspection.warning_permissions))

        if blocked_messages:
            await interaction.response.send_message(
                "以下のロールは高度な権限または管理対象外のため、設定できないゾ：\n"
                + "\n".join(blocked_messages),
                ephemeral=True,
            )
            return

        role_ids = [r.id for r in selected_roles]
        if warning_roles:
            await interaction.response.edit_message(
                embed=build_self_role_warning_embed(warning_roles),
                view=SelfRoleConfirmView(self.bot, self.guild_id, role_ids),
            )
            return

        settings = self.bot.storage.set_self_assignable_roles(self.guild_id, role_ids)
        await interaction.response.edit_message(
            embed=build_self_role_setting_embed(settings),
            view=self,
        )

    @discord.ui.button(
        label="🔄 バックアップから復元",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def restore_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        restored_guilds, restored_entries = (
            self.bot.storage.restore_role_toggle_backup(self.guild_id)
        )
        settings = self.bot.storage.get_guild_settings(self.guild_id)
        await interaction.response.edit_message(
            embed=build_self_role_setting_embed(settings), view=self
        )
        await interaction.followup.send(
            f"バックアップから復元したゾ（設定復元: {restored_guilds}件 / ユーザー履歴復元: {restored_entries}件）。",
            ephemeral=True,
        )

    @discord.ui.button(label="◀ パネルに戻る", style=discord.ButtonStyle.primary, row=1)
    async def back_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        settings = self.bot.storage.get_guild_settings(self.guild_id)
        await interaction.response.edit_message(
            embed=build_settings_embed(settings),
            view=AdminPanelView(self.bot, self.guild_id),
        )


class TadocoinExcludedChannelView(discord.ui.View):
    """タドコイン獲得例外チャンネル設定 View。"""

    def __init__(self, bot: YajuBot, guild_id: int) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[
            discord.ChannelType.text,
            discord.ChannelType.public_thread,
            discord.ChannelType.private_thread,
        ],
        placeholder="タドコイン獲得を除外するチャンネルを選択（未選択で解除）",
        min_values=0,
        max_values=25,
        row=0,
    )
    async def excluded_channel_select(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        channel_ids = [c.id for c in select.values]
        settings = self.bot.storage.set_tadocoin_excluded_channels(
            self.guild_id, channel_ids
        )
        await interaction.response.edit_message(
            embed=build_tadocoin_excluded_embed(settings), view=self
        )

    @discord.ui.button(label="◀ パネルに戻る", style=discord.ButtonStyle.primary, row=1)
    async def back_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        settings = self.bot.storage.get_guild_settings(self.guild_id)
        await interaction.response.edit_message(
            embed=build_settings_embed(settings),
            view=AdminPanelView(self.bot, self.guild_id),
        )


class TadocoinOverrideModal(discord.ui.Modal, title="タドコイン所持数の上書き"):
    """対象ユーザーのタドコイン所持数を入力するモーダル。"""

    amount_input: discord.ui.TextInput = discord.ui.TextInput(
        label="新しいタドコイン所持数（0以上の整数）",
        placeholder="例: 1000",
        required=True,
        max_length=12,
    )

    def __init__(
        self,
        bot: YajuBot,
        guild_id: int,
        target_user: discord.abc.User,
        parent_view: TadocoinOverrideView,
    ) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.guild_id = guild_id
        self.target_user = target_user
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        raw = (self.amount_input.value or "").strip()
        try:
            amount = int(raw)
        except ValueError:
            await interaction.response.send_message(
                "0以上の整数を入力してくれよな～", ephemeral=True
            )
            return
        if amount < 0:
            await interaction.response.send_message(
                "0以上の整数を指定してくれよな～", ephemeral=True
            )
            return

        new_balance = self.bot.storage.set_tadocoin(
            self.guild_id, self.target_user.id, amount
        )
        await interaction.response.edit_message(
            embed=build_tadocoin_override_embed(
                last_updated=(self.target_user.id, new_balance)
            ),
            view=self.parent_view,
        )


class TadocoinOverrideView(discord.ui.View):
    """対象ユーザーを選んでタドコイン所持数を上書きする View。"""

    def __init__(self, bot: YajuBot, guild_id: int) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="タドコインを上書きするユーザーを選択",
        min_values=1,
        max_values=1,
        row=0,
    )
    async def user_select(
        self, interaction: discord.Interaction, select: discord.ui.UserSelect
    ) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        target = select.values[0]
        await interaction.response.send_modal(
            TadocoinOverrideModal(self.bot, self.guild_id, target, self)
        )

    @discord.ui.button(label="◀ パネルに戻る", style=discord.ButtonStyle.primary, row=1)
    async def back_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        settings = self.bot.storage.get_guild_settings(self.guild_id)
        await interaction.response.edit_message(
            embed=build_settings_embed(settings),
            view=AdminPanelView(self.bot, self.guild_id),
        )


class SubRoleCostModal(discord.ui.Modal, title="サブスクロール週額の設定"):
    """サブスクロールの毎週引き落とし額を入力するモーダル。"""

    cost_input: discord.ui.TextInput = discord.ui.TextInput(
        label="毎週の引き落としタドコイン数（0で解除）",
        placeholder="例: 500（0 を入力するとこのロールのプランを削除）",
        required=True,
        max_length=10,
    )

    def __init__(
        self,
        bot: YajuBot,
        guild_id: int,
        role: discord.Role,
        parent_view: SubRolePlanView,
    ) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.guild_id = guild_id
        self.role = role
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        raw = (self.cost_input.value or "").strip()
        try:
            cost = int(raw)
        except ValueError:
            await interaction.response.send_message(
                "0以上の整数を入力してくれよな～", ephemeral=True
            )
            return
        if cost < 0:
            await interaction.response.send_message(
                "0以上の整数を指定してくれよな～", ephemeral=True
            )
            return

        self.bot.storage.set_sub_role_plan(self.guild_id, self.role.id, cost)
        await interaction.response.edit_message(
            embed=build_sub_role_plan_embed(self.bot, self.guild_id),
            view=self.parent_view,
        )


class SubRolePlanView(discord.ui.View):
    """サブスクロールのロール・週額設定 View。"""

    def __init__(self, bot: YajuBot, guild_id: int) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="サブスク対象にするロールを選択（週額設定・解除）",
        min_values=1,
        max_values=1,
        row=0,
    )
    async def role_select(
        self, interaction: discord.Interaction, select: discord.ui.RoleSelect
    ) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        role = select.values[0]
        inspection = inspect_self_role(role)
        if inspection.is_blocked:
            await interaction.response.send_message(
                f"{role.mention} は設定できないゾ: {' / '.join(inspection.blocked_reasons)}",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            SubRoleCostModal(self.bot, self.guild_id, role, self)
        )

    @discord.ui.button(label="◀ パネルに戻る", style=discord.ButtonStyle.primary, row=1)
    async def back_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        settings = self.bot.storage.get_guild_settings(self.guild_id)
        await interaction.response.edit_message(
            embed=build_settings_embed(settings),
            view=AdminPanelView(self.bot, self.guild_id),
        )


class AdminPanelView(discord.ui.View):
    """管理設定パネル（プルダウンから各設定・管理画面へ遷移）の View。"""

    def __init__(self, bot: YajuBot, guild_id: int) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id

    @discord.ui.select(
        placeholder="設定・管理項目を選択してください",
        options=[
            discord.SelectOption(
                label="発信・返信 許可ロール設定",
                emoji="🔐",
                value="proxy_roles",
            ),
            discord.SelectOption(
                label="Bot管理（/admin・/proxy）許可ロール設定",
                emoji="🛡️",
                value="admin_roles",
            ),
            discord.SelectOption(
                label="ステージモデレーターロール設定",
                emoji="🎙️",
                value="stage_mod_roles",
            ),
            discord.SelectOption(
                label="編集・削除ログチャンネル設定",
                emoji="📝",
                value="log_channel",
            ),
            discord.SelectOption(
                label="便乗機能設定",
                emoji="🗣️",
                value="binjou",
            ),
            discord.SelectOption(
                label="ロール付け外し設定",
                emoji="🏷️",
                value="self_roles",
            ),
            discord.SelectOption(
                label="タドコイン獲得例外チャンネル設定",
                emoji="<:tadocoin:1575197619569958942>",
                value="tadocoin_excluded",
            ),
            discord.SelectOption(
                label="ユーザータドコイン上書き設定",
                emoji="💰",
                value="tadocoin_override",
            ),
            discord.SelectOption(
                label="サブスクロール設定",
                emoji="👑",
                value="sub_roles",
            ),
            discord.SelectOption(
                label="IDから送信者を確認（メッセージ照会）",
                emoji="🔍",
                value="lookup",
            ),
            discord.SelectOption(
                label="削除ログを掘る",
                emoji="🗑️",
                value="deleted_logs",
            ),
        ],
        row=0,
    )
    async def menu_select(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return

        choice = select.values[0]
        settings = self.bot.storage.get_guild_settings(self.guild_id)

        if choice == "proxy_roles":
            await interaction.response.edit_message(
                embed=build_role_setting_embed(settings, "proxy"),
                view=RoleSettingView(self.bot, self.guild_id, mode="proxy"),
            )
        elif choice == "admin_roles":
            await interaction.response.edit_message(
                embed=build_role_setting_embed(settings, "admin"),
                view=RoleSettingView(self.bot, self.guild_id, mode="admin"),
            )
        elif choice == "stage_mod_roles":
            await interaction.response.edit_message(
                embed=build_role_setting_embed(settings, "stage_mod"),
                view=RoleSettingView(self.bot, self.guild_id, mode="stage_mod"),
            )
        elif choice == "log_channel":
            await interaction.response.edit_message(
                embed=build_log_channel_setting_embed(settings),
                view=LogChannelSettingView(self.bot, self.guild_id),
            )
        elif choice == "binjou":
            await interaction.response.edit_message(
                embed=build_binjou_settings_embed(settings),
                view=BinjouSettingsView(self.bot, self.guild_id),
            )
        elif choice == "self_roles":
            await interaction.response.edit_message(
                embed=build_self_role_setting_embed(settings),
                view=SelfRoleSettingView(self.bot, self.guild_id),
            )
        elif choice == "tadocoin_excluded":
            await interaction.response.edit_message(
                embed=build_tadocoin_excluded_embed(settings),
                view=TadocoinExcludedChannelView(self.bot, self.guild_id),
            )
        elif choice == "tadocoin_override":
            await interaction.response.edit_message(
                embed=build_tadocoin_override_embed(),
                view=TadocoinOverrideView(self.bot, self.guild_id),
            )
        elif choice == "sub_roles":
            await interaction.response.edit_message(
                embed=build_sub_role_plan_embed(self.bot, self.guild_id),
                view=SubRolePlanView(self.bot, self.guild_id),
            )
        elif choice == "lookup":
            await interaction.response.send_modal(LookupMessageModal(self.bot))
        elif choice == "deleted_logs":
            view = DeletedLogsView(self.bot, self.guild_id)
            await interaction.response.edit_message(
                embed=view.build_current_embed(), view=view
            )


def build_binjou_settings_embed(settings: GuildSettings) -> discord.Embed:
    """便乗機能設定の現在値を表示する Embed を生成する。"""
    channels_text = (
        ", ".join(f"<#{cid}>" for cid in settings.binjou_channel_ids)
        if settings.binjou_channel_ids
        else "未設定（どのチャンネルでも発動しません）"
    )
    embed = discord.Embed(
        title="🗣️ 便乗機能設定",
        description=(
            "便乗ボットの仮再現機能の設定をするゾ。腹減ったなぁ～\n"
            "下の選択メニューから有効にするチャンネルを選んでくれよな～"
        ),
        color=discord.Color.blue(),
    )
    embed.add_field(name="有効チャンネル", value=channels_text, inline=False)
    embed.add_field(
        name="発動確率",
        value=f"**{settings.binjou_probability:.1f}%**",
        inline=False,
    )
    return embed


class BinjouProbabilityModal(discord.ui.Modal, title="便乗発動確率の設定"):
    """便乗機能の発動確率（0〜100%）を入力するモーダル。"""

    probability_input: discord.ui.TextInput = discord.ui.TextInput(
        label="発動確率（0〜100 の数値 / %）",
        placeholder="例: 5 または 12.5",
        required=True,
        max_length=10,
    )

    def __init__(self, bot: YajuBot, guild_id: int, parent_view: BinjouSettingsView) -> None:
        super().__init__()
        self.bot = bot
        self.guild_id = guild_id
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        raw = self.probability_input.value.strip().rstrip("%")
        try:
            prob = float(raw)
        except ValueError:
            await interaction.response.send_message(
                "0〜100 の数値を入力してくれよな～頼むよ～", ephemeral=True
            )
            return
        if not (0.0 <= prob <= 100.0):
            await interaction.response.send_message(
                "確率は 0 〜 100 の範囲で指定してくれよな～", ephemeral=True
            )
            return
        settings = self.bot.storage.set_binjou_probability(self.guild_id, prob)
        await interaction.response.edit_message(
            embed=build_binjou_settings_embed(settings), view=self.parent_view
        )


class BinjouSettingsView(discord.ui.View):
    """便乗機能のチャンネル・確率設定 View。"""

    def __init__(self, bot: YajuBot, guild_id: int) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[
            discord.ChannelType.text,
            discord.ChannelType.public_thread,
            discord.ChannelType.private_thread,
        ],
        placeholder="便乗を有効にするチャンネルを選択",
        min_values=0,
        max_values=25,
        row=0,
    )
    async def binjou_channel_select(
        self, interaction: discord.Interaction, select: discord.ui.ChannelSelect
    ) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        channel_ids = [c.id for c in select.values]
        settings = self.bot.storage.set_binjou_channels(self.guild_id, channel_ids)
        await interaction.response.edit_message(
            embed=build_binjou_settings_embed(settings), view=self
    )

    @discord.ui.button(
        label="確率を設定", emoji="🎲", style=discord.ButtonStyle.primary, row=1
    )
    async def set_probability_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        await interaction.response.send_modal(
            BinjouProbabilityModal(self.bot, self.guild_id, self)
        )

    @discord.ui.button(
        label="◀ パネルに戻る", style=discord.ButtonStyle.secondary, row=1
    )
    async def back_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not can_manage_bot(interaction.user, self.bot.storage, self.guild_id):
            await interaction.response.send_message("権限がないゾ。", ephemeral=True)
            return
        settings = self.bot.storage.get_guild_settings(self.guild_id)
        await interaction.response.edit_message(
            embed=build_settings_embed(settings),
            view=AdminPanelView(self.bot, self.guild_id),
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


async def setup(bot: YajuBot) -> None:
    await bot.add_cog(AdminCog(bot))
