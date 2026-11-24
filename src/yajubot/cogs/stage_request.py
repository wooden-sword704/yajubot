"""ステージチャンネル内のスピーカーリクエスト機能を提供する Cog。"""

from __future__ import annotations

import time

import discord
from discord import app_commands
from discord.ext import commands

from yajubot.bot import YajuBot
from yajubot.cogs.proxy import can_manage_bot

HAND_RAISE_COOLDOWN_SECONDS = 30.0

REJECT_TIMEOUT_OPTIONS: tuple[tuple[str, int], ...] = (
    ("5分", 5 * 60),
    ("15分", 15 * 60),
    ("30分", 30 * 60),
    ("1時間", 60 * 60),
    ("6時間", 6 * 60 * 60),
    ("1日", 24 * 60 * 60),
    ("3日", 3 * 24 * 60 * 60),
    ("1週間", 7 * 24 * 60 * 60),
)


def can_moderate_stage(
    member: discord.abc.User, bot: YajuBot, guild_id: int
) -> bool:
    """ステージモデレーターロールまたは Bot 管理権限を持つか判定する。"""
    if can_manage_bot(member, bot.storage, guild_id):
        return True
    if not isinstance(member, discord.Member):
        return False
    settings = bot.storage.get_guild_settings(guild_id)
    if not settings.stage_mod_role_ids:
        return False
    member_role_ids = {r.id for r in member.roles}
    return any(rid in member_role_ids for rid in settings.stage_mod_role_ids)


class StageRejectTimeoutView(discord.ui.View):
    """拒否時に申請タイムアウト（任意）を選択して拒否を確定する View。"""

    def __init__(
        self,
        request_view: StageRequestView,
        request_message: discord.Message | None,
    ) -> None:
        super().__init__(timeout=120)
        self.request_view = request_view
        self.request_message = request_message
        self.selected_seconds: int = 0
        self.selected_label: str | None = None

        options = [
            discord.SelectOption(label=f"{label} タイムアウト", value=str(sec))
            for label, sec in REJECT_TIMEOUT_OPTIONS
        ]
        select = discord.ui.Select(
            placeholder="申請タイムアウトを選択（未選択ならタイムアウトなし）",
            min_values=0,
            max_values=1,
            options=options,
            row=0,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        values = interaction.data.get("values", []) if interaction.data else []
        if values:
            sec = int(values[0])
            self.selected_seconds = sec
            self.selected_label = next(
                (lbl for lbl, s in REJECT_TIMEOUT_OPTIONS if s == sec), f"{sec}秒"
            )
        else:
            self.selected_seconds = 0
            self.selected_label = None
        await interaction.response.defer()

    @discord.ui.button(
        label="拒否を確定する", emoji="❌", style=discord.ButtonStyle.danger, row=1
    )
    async def confirm_reject(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        rv = self.request_view
        if rv._handled:
            await interaction.response.edit_message(
                content="このリクエストはすでに対応済みだゾ。", view=None
            )
            return

        rv._handled = True
        rv.stop()
        rv.cog.clear_pending(rv.guild_id, rv.requester_id)

        timeout_note = ""
        if self.selected_seconds > 0 and self.selected_label is not None:
            rv.cog.set_timeout(rv.guild_id, rv.requester_id, self.selected_seconds)
            timeout_note = f"（再申請制限: **{self.selected_label}**）"

        embed = rv._build_embed("拒否", discord.Color.red(), interaction.user.id)
        if timeout_note:
            embed.add_field(name="申請制限", value=self.selected_label, inline=False)

        if self.request_message is not None:
            try:
                await self.request_message.edit(embed=embed, view=None)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        await interaction.response.edit_message(
            content=f"リクエストを拒否したゾ。{timeout_note}", view=None
        )
        if self.request_message is not None:
            await self.request_message.channel.send(
                f"❌ <@{rv.requester_id}> のスピーカーリクエストは拒否されたゾ。（対応: <@{interaction.user.id}>）{timeout_note}"
            )


class StageRequestView(discord.ui.View):
    """スピーカーリクエストに対する承認・拒否ボタン View。"""

    def __init__(
        self,
        cog: StageRequestCog,
        guild_id: int,
        stage_channel_id: int,
        requester_id: int,
        reason: str | None,
    ) -> None:
        super().__init__(timeout=1800)
        self.cog = cog
        self.bot = cog.bot
        self.guild_id = guild_id
        self.stage_channel_id = stage_channel_id
        self.requester_id = requester_id
        self.reason = reason
        self._handled = False

    async def on_timeout(self) -> None:
        self.cog.clear_pending(self.guild_id, self.requester_id)

    def _build_embed(
        self, status: str, color: discord.Color, moderator_id: int | None = None
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"🎙️ スピーカーリクエスト（{status}）",
            description=f"要請者: <@{self.requester_id}>",
            color=color,
        )
        if self.reason:
            embed.add_field(name="理由・ひとこと", value=self.reason, inline=False)
        if moderator_id is not None:
            embed.add_field(
                name="対応モデレーター", value=f"<@{moderator_id}>", inline=False
            )
        return embed

    @discord.ui.button(label="承認", emoji="✅", style=discord.ButtonStyle.success)
    async def approve_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not can_moderate_stage(interaction.user, self.bot, self.guild_id):
            await interaction.response.send_message(
                "承認・拒否はステージモデレーターまたは管理者のみ実行できるゾ。",
                ephemeral=True,
            )
            return
        if self._handled:
            await interaction.response.defer()
            return

        guild = interaction.guild
        if guild is None:
            return
        member = guild.get_member(self.requester_id)
        if member is None:
            await interaction.response.send_message(
                "要請者がサーバーから見つからないゾ。", ephemeral=True
            )
            return
        if (
            member.voice is None
            or member.voice.channel is None
            or member.voice.channel.id != self.stage_channel_id
        ):
            await interaction.response.send_message(
                "要請者が現在このステージチャンネルに接続していないため、スピーカー招待を送れないゾ。",
                ephemeral=True,
            )
            return

        try:
            await member.edit(
                suppress=False,
                reason=f"ステージスピーカーリクエスト承認 (by {interaction.user})",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "Bot にステージでスピーカーを招待する権限（メンバーをミュート/移動）がないゾ。",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await interaction.response.send_message(
                f"スピーカー招待の送信に失敗したゾ: {exc}",
                ephemeral=True,
            )
            return

        self._handled = True
        self.stop()
        self.cog.clear_pending(self.guild_id, self.requester_id)
        await interaction.response.edit_message(
            embed=self._build_embed("承認済み", discord.Color.green(), interaction.user.id),
            view=None,
        )
        await interaction.followup.send(
            f"✅ <@{self.requester_id}> にスピーカー招待を送信したゾ！（承認: <@{interaction.user.id}>）"
        )

    @discord.ui.button(label="拒否", emoji="❌", style=discord.ButtonStyle.danger)
    async def reject_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not can_moderate_stage(interaction.user, self.bot, self.guild_id):
            await interaction.response.send_message(
                "承認・拒否はステージモデレーターまたは管理者のみ実行できるゾ。",
                ephemeral=True,
            )
            return
        if self._handled:
            await interaction.response.defer()
            return

        view = StageRejectTimeoutView(self, interaction.message)
        await interaction.response.send_message(
            "必要に応じて申請タイムアウトを選択し、「拒否を確定する」を押してください（無選択ならタイムアウトなしで拒否）。",
            view=view,
            ephemeral=True,
        )


class StageHandRaisePromptView(discord.ui.View):
    """手を挙げたユーザーにボット経由申請を提案するプロンプト View。"""

    def __init__(
        self,
        cog: StageRequestCog,
        guild_id: int,
        stage_channel_id: int,
        requester_id: int,
    ) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.guild_id = guild_id
        self.stage_channel_id = stage_channel_id
        self.requester_id = requester_id

    @discord.ui.button(label="許可（申請する）", emoji="✅", style=discord.ButtonStyle.success)
    async def allow_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "本人のみ操作できるゾ。", ephemeral=True
            )
            return

        if self.cog.is_pending(self.guild_id, self.requester_id):
            await interaction.response.send_message(
                "すでに承認待ちのリクエストがあるゾ。", ephemeral=True
            )
            return

        timeout_until = self.cog.get_timeout_until(self.guild_id, self.requester_id)
        if timeout_until is not None:
            ts = int(timeout_until)
            await interaction.response.send_message(
                f"兄貴のスピーカーリクエストは現在タイムアウト期間中ゾ。ボット経由での申請はできないゾ（解除: <t:{ts}:R>）",
                ephemeral=True,
            )
            return

        self.stop()
        if interaction.message is not None:
            try:
                await interaction.message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        channel = self.cog.bot.get_channel(self.stage_channel_id)
        if not isinstance(channel, discord.StageChannel):
            return

        await self.cog.send_speaker_request(
            channel=channel,
            guild_id=self.guild_id,
            requester_id=self.requester_id,
            mention=True,
            reason=None,
        )
        await interaction.response.send_message(
            "ん、おかのした", ephemeral=True
        )

    @discord.ui.button(label="無視", emoji="🗑️", style=discord.ButtonStyle.secondary)
    async def ignore_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "本人のみ操作できるゾ。", ephemeral=True
            )
            return
        self.stop()
        await interaction.response.defer()
        if interaction.message is not None:
            try:
                await interaction.message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


class StageRequestCog(commands.Cog):
    """ステージ内チャットでスピーカーリクエストを行う Cog。"""

    def __init__(self, bot: YajuBot) -> None:
        self.bot = bot
        self._pending_requests: set[tuple[int, int]] = set()
        self._request_timeouts: dict[tuple[int, int], float] = {}
        self._hand_raise_cooldowns: dict[tuple[int, int], float] = {}

    def is_pending(self, guild_id: int, user_id: int) -> bool:
        return (guild_id, user_id) in self._pending_requests

    def add_pending(self, guild_id: int, user_id: int) -> None:
        self._pending_requests.add((guild_id, user_id))

    def clear_pending(self, guild_id: int, user_id: int) -> None:
        self._pending_requests.discard((guild_id, user_id))

    def get_timeout_until(self, guild_id: int, user_id: int) -> float | None:
        until = self._request_timeouts.get((guild_id, user_id))
        if until is None:
            return None
        if until <= time.time():
            self._request_timeouts.pop((guild_id, user_id), None)
            return None
        return until

    def set_timeout(self, guild_id: int, user_id: int, seconds: int) -> None:
        self._request_timeouts[(guild_id, user_id)] = time.time() + seconds

    async def send_speaker_request(
        self,
        *,
        channel: discord.StageChannel,
        guild_id: int,
        requester_id: int,
        mention: bool,
        reason: str | None,
    ) -> None:
        mention_text: str | None = None
        allowed_mentions = discord.AllowedMentions.none()
        if mention:
            settings = self.bot.storage.get_guild_settings(guild_id)
            if settings.stage_mod_role_ids:
                mention_text = " ".join(
                    f"<@&{rid}>" for rid in settings.stage_mod_role_ids
                )
                allowed_mentions = discord.AllowedMentions(roles=True)

        self.add_pending(guild_id, requester_id)
        view = StageRequestView(
            self,
            guild_id=guild_id,
            stage_channel_id=channel.id,
            requester_id=requester_id,
            reason=reason,
        )
        embed = view._build_embed("承認待ち", discord.Color.blurple())
        await channel.send(
            content=mention_text,
            embed=embed,
            view=view,
            allowed_mentions=allowed_mentions,
        )

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return
        if not isinstance(after.channel, discord.StageChannel):
            return
        # 「手を挙げる」を新規に押した瞬間のみ反応
        if not (
            before.requested_to_speak_at is None
            and after.requested_to_speak_at is not None
        ):
            return

        guild_id = member.guild.id
        key = (guild_id, member.id)

        # すでに申請中なら何も言わない
        if self.is_pending(guild_id, member.id):
            return

        # 上げ下げ連打対策クールダウン
        now = time.monotonic()
        last_at = self._hand_raise_cooldowns.get(key, 0.0)
        if now - last_at < HAND_RAISE_COOLDOWN_SECONDS:
            return
        self._hand_raise_cooldowns[key] = now

        # タイムアウト期間中かチェック
        timeout_until = self.get_timeout_until(guild_id, member.id)
        if timeout_until is not None:
            ts = int(timeout_until)
            try:
                await after.channel.send(
                    content=f"<@{member.id}> 兄貴のスピーカーリクエストは現在タイムアウト期間中ゾ。ボット経由での申請はできないゾ（解除: <t:{ts}:R>）",
                    allowed_mentions=discord.AllowedMentions(users=True),
                    delete_after=15,
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
            return

        # 申請確認プロンプトを投稿（60秒で自動消去）
        view = StageHandRaisePromptView(
            self,
            guild_id=guild_id,
            stage_channel_id=after.channel.id,
            requester_id=member.id,
        )
        try:
            await after.channel.send(
                content=f"<@{member.id}> モデレーターへスピーカーリクエストを送れるゾ！ドウスッペ...",
                view=view,
                allowed_mentions=discord.AllowedMentions(users=True),
                delete_after=60,
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    @app_commands.command(
        name="speaker_request",
        description="ステージチャンネル内でスピーカーへの招待をリクエストするゾ",
    )
    @app_commands.describe(
        mention="ステージモデレーターへメンションを飛ばすか（デフォルト: ON）",
        reason="リクエストの理由や話したい内容（任意）",
    )
    @app_commands.guild_only()
    async def speaker_request(
        self,
        interaction: discord.Interaction,
        mention: bool = True,
        reason: app_commands.Range[str, 1, 300] | None = None,
    ) -> None:
        guild = interaction.guild
        channel = interaction.channel
        if guild is None or not isinstance(channel, discord.StageChannel):
            await interaction.response.send_message(
                "このコマンドはステージチャンネル内のテキストチャットでのみ使用できるゾ。",
                ephemeral=True,
            )
            return

        if self.is_pending(guild.id, interaction.user.id):
            await interaction.response.send_message(
                "すでに承認待ちのスピーカーリクエストがあるゾ。対応されるまで待ってくれよな～",
                ephemeral=True,
            )
            return

        timeout_until = self.get_timeout_until(guild.id, interaction.user.id)
        if timeout_until is not None:
            ts = int(timeout_until)
            await interaction.response.send_message(
                f"現在スピーカーリクエストが制限されているゾ（解除: <t:{ts}:F> / <t:{ts}:R>）。",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "スピーカーリクエストを送信したゾ！", ephemeral=True
        )
        await self.send_speaker_request(
            channel=channel,
            guild_id=guild.id,
            requester_id=interaction.user.id,
            mention=mention,
            reason=reason,
        )


async def setup(bot: YajuBot) -> None:
    await bot.add_cog(StageRequestCog(bot))
