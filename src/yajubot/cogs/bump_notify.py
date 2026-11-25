"""DISBOARD (/bump) および Dissoku (/up) の実行を検知してリマインダー通知を送る Cog。"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from yajubot.bot import YajuBot
from yajubot.cogs.proxy import can_manage_bot
from yajubot.storage import BumpNotifySettings, BumpReminderRecord

logger = logging.getLogger(__name__)

CommandType = Literal["bump", "up"]

_FAILURE_PATTERN = re.compile(
    r"まだアップできません|アップに失敗|失敗しました|please\s+wait|wait\s+another|before\s+you\s+can\s+bump|cooldown",
    re.IGNORECASE,
)

_BUMP_SUCCESS_PATTERN = re.compile(
    r"表示順をアップしたよ"
    r"|bump.*(?:success|successful|done|完了|成功)"
    r"|(?:success|successful|done|完了|成功).*bump"
    r"|bump.*ありがとう"
    r"|サーバーを.*bump",
    re.IGNORECASE | re.DOTALL,
)

_UP_SUCCESS_PATTERN = re.compile(
    r"(?:サーバーを)?(?:アップ|up)\s*(?:しました|完了|成功)"
    r"|表示順位を上げました"
    r"|up.*(?:success|successful|done|完了|成功)"
    r"|(?:success|successful|done|完了|成功).*up",
    re.IGNORECASE | re.DOTALL,
)

_COMMAND_LABELS: dict[str, dict[str, str]] = {
    "bump": {
        "title": "DISBOARD (/bump)",
        "command": "/bump",
        "message": "🔔 **DISBOARDのBUMPができます！**\n`/bump` を実行してサーバーをBUMPしましょう！",
    },
    "up": {
        "title": "ディス速 (/up)",
        "command": "/up",
        "message": "🔔 **ディス速（Dissoku）のUPができます！**\n`/up` を実行してサーバーをUPしましょう！",
    },
}


def extract_message_response_text(message: discord.Message) -> str:
    """メッセージ本文および Embed 内のテキストを結合して小文字化する。"""
    parts: list[str] = []
    if message.content:
        parts.append(message.content)
    for embed in message.embeds:
        if embed.title:
            parts.append(embed.title)
        if embed.description:
            parts.append(embed.description)
        for field in embed.fields:
            if field.name:
                parts.append(field.name)
            if field.value:
                parts.append(field.value)
        footer = getattr(embed, "footer", None)
        footer_text = getattr(footer, "text", None) if footer is not None else None
        if footer_text:
            parts.append(footer_text)
    return " ".join(parts).lower()


def is_command_success_text(command_type: CommandType, response_text: str) -> bool:
    """Bot の返信テキストが Bump/Up 成功メッセージかどうかを判定する。"""
    if not response_text:
        return False
    if _FAILURE_PATTERN.search(response_text):
        return False
    if command_type == "bump":
        return bool(_BUMP_SUCCESS_PATTERN.search(response_text))
    return bool(_UP_SUCCESS_PATTERN.search(response_text))


def _matches_command_name(command_type: CommandType, raw_name: str | None) -> bool:
    """アプリコマンド名が対象コマンド（bump / up）に一致するか判定する。"""
    if not raw_name:
        # discord.py の interaction_metadata ではコマンド名が省略される場合があるため、
        # interaction_metadata が存在し Bot ID と成功メッセージが一致すれば許容する。
        return True
    name = raw_name.strip().lower()
    if command_type == "bump":
        return name == "bump" or name.endswith(" bump")
    return name in ("up", "dissoku up", "dissoku") or name.endswith(" up")


def detect_bump_or_up_command(
    message: discord.Message,
    settings: BumpNotifySettings,
) -> tuple[CommandType, int] | None:
    """メッセージが対象の Bump/Up 成功返信であれば `(command_type, user_id)` を返す。"""
    if message.guild is None:
        return None

    author_id = getattr(message.author, "id", None)
    if author_id == settings.bump_bot_id:
        command_type: CommandType = "bump"
        if not settings.bump_enabled:
            return None
    elif author_id == settings.up_bot_id:
        command_type = "up"
        if not settings.up_enabled:
            return None
    else:
        return None

    interaction = getattr(message, "interaction", None)
    metadata = getattr(message, "interaction_metadata", None)
    if interaction is None and metadata is None:
        return None

    if metadata is not None:
        meta_type = getattr(metadata, "type", None)
        if meta_type is not None and meta_type != discord.InteractionType.application_command:
            return None

    raw_name = getattr(interaction, "name", None) or getattr(metadata, "name", None)
    if not _matches_command_name(command_type, raw_name):
        return None

    response_text = extract_message_response_text(message)
    if not is_command_success_text(command_type, response_text):
        logger.debug(
            "%s 成功メッセージを確認できませんでした (guild=%s): %s",
            command_type.upper(),
            message.guild.id,
            response_text,
        )
        return None

    user = getattr(metadata, "user", None) or getattr(interaction, "user", None)
    user_id = int(getattr(user, "id", 0) or 0)
    return command_type, user_id


class BumpNotifyCog(commands.GroupCog, group_name="bump_notify"):
    """DISBOARD (/bump) と Dissoku (/up) の通知リマインダーを提供する Cog。"""

    def __init__(self, bot: YajuBot) -> None:
        self.bot = bot
        self._tasks: dict[tuple[int, str], asyncio.Task[None]] = {}
        self._processed_message_ids: deque[int] = deque(maxlen=500)
        self._restored: bool = False

    async def cog_load(self) -> None:
        if self.bot.is_ready():
            self.restore_reminders()

    async def cog_unload(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()

    def restore_reminders(self) -> int:
        """永続化されているリマインダーを復元してタスクを再スケジュールする。"""
        reminders = self.bot.storage.list_bump_reminders()
        for reminder in reminders:
            self._schedule_task(reminder)
        self._restored = True
        if reminders:
            logger.info("Bump/Up リマインダーを %d 件復元しました。", len(reminders))
        return len(reminders)

    def _schedule_task(self, reminder: BumpReminderRecord) -> None:
        key = (reminder.guild_id, reminder.command_type)
        existing = self._tasks.pop(key, None)
        if existing is not None and not existing.done():
            existing.cancel()

        task = asyncio.create_task(
            self._run_reminder(reminder),
            name=f"bump-notify-{reminder.guild_id}-{reminder.command_type}",
        )
        self._tasks[key] = task

    async def _run_reminder(self, reminder: BumpReminderRecord) -> None:
        key = (reminder.guild_id, reminder.command_type)
        try:
            delay = max(0.0, reminder.notify_at - time.time())
            if delay > 0:
                await asyncio.sleep(delay)

            latest = self.bot.storage.get_bump_reminder(
                reminder.guild_id, reminder.command_type
            )
            if latest is None or abs(latest.notify_at - reminder.notify_at) > 1e-6:
                return

            await self._send_notification(latest)
            self.bot.storage.delete_bump_reminder(
                reminder.guild_id, reminder.command_type
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Bump/Up リマインダー送信中にエラーが発生しました (guild=%s, type=%s)",
                reminder.guild_id,
                reminder.command_type,
            )
        finally:
            if self._tasks.get(key) is asyncio.current_task():
                self._tasks.pop(key, None)

    async def _send_notification(self, reminder: BumpReminderRecord) -> None:
        channel = self.bot.get_channel(reminder.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(reminder.channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                logger.warning(
                    "通知先チャンネル (%s) の取得に失敗しました: %s",
                    reminder.channel_id,
                    exc,
                )
                return

        if not hasattr(channel, "send"):
            return

        label = _COMMAND_LABELS.get(
            reminder.command_type,
            {
                "message": f"🔔 **{reminder.command_type.upper()} ができます！**",
            },
        )
        mention = f"<@{reminder.user_id}> " if reminder.user_id else ""
        content = f"{mention}{label['message']}"
        try:
            await channel.send(content)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning(
                "Bump/Up 通知の送信に失敗しました (channel=%s): %s",
                reminder.channel_id,
                exc,
            )

    async def _handle_bot_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        # 一般ユーザーのメッセージは即除外し、高トラフィック時の負荷を抑える
        if not getattr(message.author, "bot", False):
            return
        if message.id in self._processed_message_ids:
            return

        settings = self.bot.storage.get_bump_notify_settings(message.guild.id)
        detected = detect_bump_or_up_command(message, settings)
        if detected is None:
            return

        command_type, user_id = detected
        self._processed_message_ids.append(message.id)

        delay_minutes = (
            settings.bump_delay_minutes
            if command_type == "bump"
            else settings.up_delay_minutes
        )
        target_channel_id = settings.channel_id or message.channel.id
        now_ts = time.time()
        notify_at = now_ts + delay_minutes * 60

        reminder = self.bot.storage.upsert_bump_reminder(
            guild_id=message.guild.id,
            command_type=command_type,
            channel_id=target_channel_id,
            user_id=user_id,
            notify_at=notify_at,
            created_at=now_ts,
        )
        self._schedule_task(reminder)
        logger.info(
            "%s 検知: guild=%s user=%s channel=%s delay=%d分",
            command_type.upper(),
            message.guild.id,
            user_id,
            target_channel_id,
            delay_minutes,
        )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self._restored:
            self.restore_reminders()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self._handle_bot_message(message)

    @commands.Cog.listener()
    async def on_message_edit(
        self, _before: discord.Message, after: discord.Message
    ) -> None:
        await self._handle_bot_message(after)

    async def _require_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "サーバー内で実行してください。", ephemeral=True
            )
            return False
        if not can_manage_bot(interaction.user, self.bot.storage, interaction.guild_id):
            await interaction.response.send_message(
                "このコマンドを実行する権限がありません。", ephemeral=True
            )
            return False
        return True

    @app_commands.command(
        name="status", description="現在の Bump/Up 通知設定と待機中のタイマーを表示する"
    )
    async def status(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "サーバー内で実行してください。", ephemeral=True
            )
            return

        settings = self.bot.storage.get_bump_notify_settings(interaction.guild_id)
        reminders = {
            r.command_type: r
            for r in self.bot.storage.list_bump_reminders(interaction.guild_id)
        }

        channel_text = (
            f"<#{settings.channel_id}>"
            if settings.channel_id is not None
            else "コマンド実行チャンネル（自動）"
        )
        embed = discord.Embed(
            title="🔔 Bump / Up 通知設定",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="通知先チャンネル", value=channel_text, inline=False)

        for cmd_type in ("bump", "up"):
            enabled = settings.bump_enabled if cmd_type == "bump" else settings.up_enabled
            delay = (
                settings.bump_delay_minutes
                if cmd_type == "bump"
                else settings.up_delay_minutes
            )
            bot_id = settings.bump_bot_id if cmd_type == "bump" else settings.up_bot_id
            reminder = reminders.get(cmd_type)
            timer_text = (
                f"<t:{int(reminder.notify_at)}:F> (<t:{int(reminder.notify_at)}:R>) → <#{reminder.channel_id}>"
                if reminder is not None
                else "待機中なし"
            )
            embed.add_field(
                name=_COMMAND_LABELS[cmd_type]["title"],
                value=(
                    f"状態: **{'有効' if enabled else '無効'}**\n"
                    f"設定時間: **{delay}分**\n"
                    f"対象Bot ID: `{bot_id}`\n"
                    f"次回通知: {timer_text}"
                ),
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="channel",
        description="Bump/Up 通知の送信先チャンネルを設定する（省略時は実行チャンネル）",
    )
    @app_commands.describe(channel="通知先テキストチャンネル（未指定で自動モードに戻す）")
    async def set_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        if not await self._require_admin(interaction):
            return
        assert interaction.guild_id is not None
        channel_id = channel.id if channel is not None else None
        self.bot.storage.update_bump_notify_settings(
            interaction.guild_id, channel_id=channel_id
        )
        if channel is not None:
            msg = f"Bump/Up 通知先チャンネルを {channel.mention} に設定しました。"
        else:
            msg = "Bump/Up 通知先を「コマンドが実行されたチャンネル（自動）」に設定しました。"
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(
        name="timer", description="bump / up それぞれの通知までの時間（分）を設定する"
    )
    @app_commands.describe(
        command="対象のコマンド（bump または up）",
        minutes="通知までの時間（分、1〜1440）",
    )
    @app_commands.choices(
        command=[
            app_commands.Choice(name="bump (DISBOARD)", value="bump"),
            app_commands.Choice(name="up (ディス速)", value="up"),
        ]
    )
    async def set_timer(
        self,
        interaction: discord.Interaction,
        command: app_commands.Choice[str],
        minutes: app_commands.Range[int, 1, 1440],
    ) -> None:
        if not await self._require_admin(interaction):
            return
        assert interaction.guild_id is not None
        if command.value == "bump":
            self.bot.storage.update_bump_notify_settings(
                interaction.guild_id, bump_delay_minutes=minutes
            )
        else:
            self.bot.storage.update_bump_notify_settings(
                interaction.guild_id, up_delay_minutes=minutes
            )
        await interaction.response.send_message(
            f"**{command.name}** の通知時間を **{minutes}分** に設定しました。",
            ephemeral=True,
        )

    @app_commands.command(
        name="toggle", description="Bump/Up 通知の有効・無効を切り替える"
    )
    @app_commands.describe(
        target="対象（bump / up / all）",
        enabled="有効にする場合は True、無効にする場合は False",
    )
    @app_commands.choices(
        target=[
            app_commands.Choice(name="bump (DISBOARD)", value="bump"),
            app_commands.Choice(name="up (ディス速)", value="up"),
            app_commands.Choice(name="all (両方)", value="all"),
        ]
    )
    async def toggle(
        self,
        interaction: discord.Interaction,
        target: app_commands.Choice[str],
        enabled: bool,
    ) -> None:
        if not await self._require_admin(interaction):
            return
        assert interaction.guild_id is not None
        kwargs: dict[str, bool] = {}
        if target.value in ("bump", "all"):
            kwargs["bump_enabled"] = enabled
        if target.value in ("up", "all"):
            kwargs["up_enabled"] = enabled
        self.bot.storage.update_bump_notify_settings(interaction.guild_id, **kwargs)
        state = "有効" if enabled else "無効"
        await interaction.response.send_message(
            f"**{target.name}** の通知を **{state}** にしました。",
            ephemeral=True,
        )

    @app_commands.command(
        name="bot_id", description="監視対象の Bot ID を変更する"
    )
    @app_commands.describe(
        command="対象のコマンド（bump または up）",
        bot_id="対象 Bot のユーザー ID（数値）",
    )
    @app_commands.choices(
        command=[
            app_commands.Choice(name="bump (DISBOARD)", value="bump"),
            app_commands.Choice(name="up (ディス速)", value="up"),
        ]
    )
    async def set_bot_id(
        self,
        interaction: discord.Interaction,
        command: app_commands.Choice[str],
        bot_id: str,
    ) -> None:
        if not await self._require_admin(interaction):
            return
        assert interaction.guild_id is not None
        raw = bot_id.strip()
        if not raw.isdigit() or int(raw) <= 0:
            await interaction.response.send_message(
                "Bot ID は正の整数で指定してください。", ephemeral=True
            )
            return
        parsed_id = int(raw)
        if command.value == "bump":
            self.bot.storage.update_bump_notify_settings(
                interaction.guild_id, bump_bot_id=parsed_id
            )
        else:
            self.bot.storage.update_bump_notify_settings(
                interaction.guild_id, up_bot_id=parsed_id
            )
        await interaction.response.send_message(
            f"**{command.name}** の監視対象 Bot ID を `{parsed_id}` に設定しました。",
            ephemeral=True,
        )


async def setup(bot: YajuBot) -> None:
    """BumpNotifyCog を Bot に登録する。"""
    await bot.add_cog(BumpNotifyCog(bot))
