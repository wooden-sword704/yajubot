"""ボット経由（Webhook）のメッセージ代替送信システムを提供する Cog。"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from yajubot.bot import YajuBot

logger = logging.getLogger(__name__)

WEBHOOK_NAME = "yajubot-proxy"
LAUNCHER_CUSTOM_ID = "proxy:open_modal"
LAUNCHER_THROTTLE_SECONDS = 5.0

# 既存のカスタム絵文字 (<:name:id> や <a:name:id>) またはショートコード (:name:) をマッチング
EMOJI_TOKEN_PATTERN = re.compile(r"(<a?:[a-zA-Z0-9_]+:\d+>)|:([a-zA-Z0-9_]+):")

REPLY1_EMOJI = "<:reply1:1571547845683712101>"
REPLY2_EMOJI = "<:reply2:1571547847264829490>"


class ProxyError(RuntimeError):
    """代替送信処理におけるユーザーへ通知可能なエラー。"""


def build_launcher_embed() -> discord.Embed:
    """送信開始ボタン付きの待機メッセージ用 Embed を生成する。"""
    return discord.Embed(
        title="メッセージ送信したりしなかったりしろ",
        description=(
            "下の **「ほらいくどー」** ボタンから入力すると"
            "代わりにボットがメッセージを送信するゾ"
        ),
        color=discord.Color.blurple(),
    )


def expand_custom_emojis(content: str, emoji_map: dict[str, str]) -> str:
    """メッセージ本文中の `:emoji_name:` ショートコードを `<:name:id>` 等のカスタム絵文字構文へ置換する。

    すでに `<:name:id>` や `<a:name:id>` になっている部分はそのまま保持し、
    登録されている絵文字名に一致するもののみ展開する。
    """
    if not content or not emoji_map:
        return content

    def _repl(match: re.Match[str]) -> str:
        # すでにカスタム絵文字構文になっている場合はそのまま
        if match.group(1):
            return match.group(1)
        name = match.group(2)
        return emoji_map.get(name, match.group(0))

    return EMOJI_TOKEN_PATTERN.sub(_repl, content)


def strip_reply_header(text: str) -> str:
    """返信メッセージの場合、1行目の返信ヘッダー部分を取り除いて本文（2行目以降）を返す。"""
    if not text:
        return ""
    lines = text.split("\n")
    if len(lines) > 1:
        first_line = lines[0]
        if REPLY1_EMOJI in first_line or ":reply1:" in first_line:
            return "\n".join(lines[1:]).strip()
    return text.strip()


def extract_proxy_author_id(message: discord.Message) -> int | None:
    """メッセージがプロキシ送信（Webhook）されたものである場合、削除ボタン等の custom_id から送信者ユーザー ID を抽出する。"""
    if hasattr(message, "components"):
        for row in getattr(message, "components", []):
            for child in getattr(row, "children", []):
                custom_id = getattr(child, "custom_id", None)
                if custom_id:
                    m = re.match(r"^proxy:delete:(?P<author_id>\d+)$", custom_id)
                    if m:
                        return int(m.group("author_id"))
    return None


def format_reply_header(
    target_message: discord.Message,
    cached_content: str | None = None,
) -> str:
    """返信先メッセージの情報を基に、指定フォーマットの返信ヘッダーテキストを生成する。

    形式:
    <:reply1:1571547845683712101><:reply2:1571547847264829490>@返信先ユーザー :[クランプした内容](メッセージリンク)
    ※ Webhook メッセージへの返信の場合、Webhook 自身ではなく元の送信者ユーザーにメンションする。
    ※ 添付ファイルがある場合は内容部分を「添付ファイル」とする。
    ※ 返信先が返信メッセージの場合は1行目のヘッダーを除外して本文を読み込む。
    """
    author = target_message.author

    # Webhookメッセージへの返信の場合、削除ボタンのcustom_id等から実際の送信者ユーザーIDを取得
    proxy_author_id = extract_proxy_author_id(target_message)
    if proxy_author_id is not None:
        author_mention = f"<@{proxy_author_id}>"
    else:
        author_mention = getattr(author, "mention", f"@{getattr(author, 'display_name', 'user')}")

    # 添付ファイルが実際に存在する場合のみ「添付ファイル」とする
    has_attachments = bool(getattr(target_message, "attachments", None))

    if has_attachments:
        content_text = "添付ファイル"
    else:
        # メッセージ本文（キャッシュ、clean_content、contentの順で取得）
        raw_text = cached_content or target_message.clean_content or target_message.content or ""
        # 返信への返信の場合、1行目の返信ヘッダーを除去して本文から取得
        stripped_text = strip_reply_header(raw_text)
        single_line = " ".join(stripped_text.split())
        if single_line:
            if len(single_line) > 35:
                content_text = single_line[:32] + "..."
            else:
                content_text = single_line
        else:
            # メッセージ本文が空（埋め込みのみ、またはMessage Content Intent未付与時）
            if getattr(target_message, "embeds", None):
                content_text = "埋め込み"
            else:
                content_text = "メッセージ"

    # Markdown リンク破壊防止
    content_text = content_text.replace("[", r"\[").replace("]", r"\]")

    return f"{REPLY1_EMOJI}{REPLY2_EMOJI}{author_mention} :[{content_text}]({target_message.jump_url})"


async def resolve_webhook_target(
    channel: discord.abc.Messageable,
) -> tuple[Any, discord.Thread | None]:
    """チャンネルから Webhook 操作対象の親チャンネルとスレッドを解決する。"""
    if isinstance(channel, discord.Thread):
        parent = channel.parent
        if parent is None or not hasattr(parent, "webhooks"):
            raise ProxyError("このスレッドでは Webhook が使えないゾ")
        return parent, channel

    if hasattr(channel, "webhooks"):
        return channel, None

    raise ProxyError("（こ↑こ↓のチャンネル種別では Webhook が使え）ないです")


async def get_or_create_proxy_webhook(
    target_channel: Any,
    bot_user_id: int,
) -> discord.Webhook:
    """Bot 自身が所有する（コンポーネント送信可能な）Webhook を取得または作成する。"""
    try:
        webhooks = await target_channel.webhooks()
    except discord.Forbidden as exc:
        raise ProxyError(
            "（Botにウェブフックの管理権限が）ないです。管理者に報告してくれよな～頼むよ～"
        ) from exc

    for webhook in webhooks:
        if (
            webhook.token is not None
            and webhook.user is not None
            and webhook.user.id == bot_user_id
        ):
            return webhook

    try:
        return await target_channel.create_webhook(
            name=WEBHOOK_NAME,
            reason="メッセージ代替送信システム用",
        )
    except discord.Forbidden as exc:
        raise ProxyError(
            "Bot に「ウェブフックの管理 (Manage Webhooks)」権限がないため、"
            "Webhook を作成できなかったゾ。管理者に報告してくれよな～頼むよ～"
        ) from exc


async def delete_webhook_message(
    target_message: discord.Message,
    bot_user_id: int | None,
) -> None:
    """Webhook 経由（不可時は通常削除）で対象メッセージを削除する。"""
    if bot_user_id is not None:
        try:
            target_channel, thread = await resolve_webhook_target(target_message.channel)
            webhook = await get_or_create_proxy_webhook(target_channel, bot_user_id)
            if thread is not None:
                await webhook.delete_message(target_message.id, thread=thread)
            else:
                await webhook.delete_message(target_message.id)
            return
        except (ProxyError, discord.Forbidden, discord.HTTPException):
            # Webhook 削除が失敗した場合は Bot の通常削除へフォールバックする
            pass

    await target_message.delete()


class ConfirmDeleteView(discord.ui.View):
    """送信者本人向けのエフェメラル削除確認ダイアログ。"""

    def __init__(self, target_message: discord.Message, author_id: int) -> None:
        super().__init__(timeout=60)
        self.target_message = target_message
        self.author_id = author_id

    @discord.ui.button(label="はい、ヨロシクぅ！", style=discord.ButtonStyle.danger)
    async def confirm_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "あっおい待てい（江戸っ子）、本人以外は削除できないゾ", ephemeral=True
            )
            return

        await interaction.response.defer()
        bot_user_id = interaction.client.user.id if interaction.client.user else None

        try:
            await delete_webhook_message(self.target_message, bot_user_id)
        except discord.NotFound:
            await interaction.edit_original_response(
                content="メッセージはすでに削除されてるゾ。", view=None
            )
        except (discord.Forbidden, discord.HTTPException):
            await interaction.edit_original_response(
                content="メッセージの削除ができなかっ...たぁ!（Bot の権限を確認してください）。",
                view=None,
            )
        else:
            await interaction.edit_original_response(
                content="メッセージを消し飛ばしたゾ", view=None
            )
        self.stop()

    @discord.ui.button(label="やっぱやめとく", style=discord.ButtonStyle.secondary)
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(
            content="キャンセルしたゾ", view=None
        )
        self.stop()


class ProxyDeleteButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"proxy:delete:(?P<author_id>\d+)",
):
    """Webhook メッセージに付与する永続削除ボタン（送信者 ID を custom_id に保持）。"""

    def __init__(self, author_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="削除",
                emoji="🗑️",
                style=discord.ButtonStyle.secondary,
                custom_id=f"proxy:delete:{author_id}",
            )
        )
        self.author_id = author_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Item,
        match: re.Match[str],
        /,
    ) -> ProxyDeleteButton:
        return cls(int(match.group("author_id")))

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "あっおい待てい（江戸っ子）、本人以外は削除できないゾ",
                ephemeral=True,
            )
            return

        if interaction.message is None:
            await interaction.response.send_message(
                "削除対象のメッセージが読み込めなかったゾ...",
                ephemeral=True,
            )
            return

        view = ConfirmDeleteView(
            target_message=interaction.message,
            author_id=self.author_id,
        )
        await interaction.response.send_message(
            "メッセージ削除いいすかぁ？",
            view=view,
            ephemeral=True,
        )


class ProxyMessageView(discord.ui.View):
    """Webhook で送信されたメッセージに添付する View。"""

    def __init__(self, author_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(ProxyDeleteButton(author_id))


class ProxyMessageModal(discord.ui.Modal, title="メッセージを送信"):
    """代替送信するメッセージ内容を入力するポップアップモーダル。"""

    # メンション通知の入切セレクト
    mention_select = discord.ui.Label(
        text="返信時のメンション通知",
        component=discord.ui.Select(
            custom_id="proxy:modal_mention_select",
            options=[
                discord.SelectOption(
                    label="メンション通知する",
                    value="true",
                    default=True,
                ),
                discord.SelectOption(
                    label="メンション通知しない",
                    value="false",
                ),
            ],
        ),
    )

    message_input = discord.ui.Label(
        text="送信するメッセージを書き込み",
        component=discord.ui.TextInput(
            custom_id="proxy:modal_message_input",
            style=discord.TextStyle.paragraph,
            placeholder="ナニガキモチーノ",
            required=True,
            max_length=2000,
        ),
    )

    def __init__(
        self,
        cog: ProxyCog,
        launcher_message: discord.Message | None,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.launcher_message = launcher_message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        # メンション選択値（デフォルト: True）
        mention_author = True
        try:
            selected_vals = self.mention_select.component.values
            if selected_vals and selected_vals[0] == "false":
                mention_author = False
        except Exception:
            mention_author = True

        raw_content = self.message_input.component.value or ""

        # ポップアップ経由でも事前にセットされた返信先があれば適用
        channel = interaction.channel
        reply_message: discord.Message | None = None
        reply_content_override: str | None = None
        pending_key = (interaction.user.id, channel.id) if channel else None

        if channel and pending_key:
            pending_data = self.cog._pending_replies.get(pending_key)
            if pending_data:
                target_msg_id, reply_content_override = pending_data
                try:
                    reply_message = await channel.fetch_message(target_msg_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    reply_message = None

        try:
            await self.cog.send_proxy_message(
                interaction=interaction,
                content=raw_content,
                clicked_launcher=self.launcher_message,
                repost_launcher=True,
                reply_message=reply_message,
                reply_content_override=reply_content_override,
                mention_author=mention_author,
            )
            if pending_key:
                self.cog._pending_replies.pop(pending_key, None)
        except ProxyError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except discord.HTTPException as exc:
            logger.warning("エラー！滅！！！！: %s", exc)
            await interaction.followup.send(
                "送信ダメみたいですね（失敗）ちょっと待ってから送りなおしてくれよな～頼むよ～",
                ephemeral=True,
            )
        else:
            await interaction.delete_original_response()


class ProxyLauncherView(discord.ui.View):
    """「ほらいくどー」待機ボタンを持つ永続 View。"""

    def __init__(self, cog: ProxyCog) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="ほらいくどー",
        emoji="✉️",
        style=discord.ButtonStyle.primary,
        custom_id=LAUNCHER_CUSTOM_ID,
    )
    async def open_modal(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        modal = ProxyMessageModal(
            cog=self.cog,
            launcher_message=interaction.message,
        )
        await interaction.response.send_modal(modal)


class ProxyCog(commands.Cog):
    """ボット経由（Webhook）のメッセージ代替送信機能を提供する Cog。"""

    def __init__(self, bot: YajuBot) -> None:
        self.bot = bot
        self._channel_locks: dict[int, asyncio.Lock] = {}
        self._latest_launcher_ids: dict[int, int] = {}
        # ユーザーごとの返信先記憶: (user_id, channel_id) -> (message_id, cached_content)
        self._pending_replies: dict[tuple[int, int], tuple[int, str]] = {}
        self._app_emojis_cache: dict[str, str] = {}
        self._app_emojis_fetched: bool = False

        # 待機ボタン再送信のスロットリング管理（5秒間隔）
        self._last_launcher_repost_time: dict[int, float] = {}
        self._pending_throttle_tasks: dict[int, asyncio.Task[None]] = {}
        self._has_pending_throttle_request: dict[int, bool] = {}

        self.reply_ctx_menu = app_commands.ContextMenu(
            name="返信先にセット",
            callback=self._context_menu_toggle_reply,
        )
        self.bot.tree.add_command(self.reply_ctx_menu)

    def _get_lock(self, channel_id: int) -> asyncio.Lock:
        lock = self._channel_locks.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            self._channel_locks[channel_id] = lock
        return lock

    async def get_available_emojis(self, guild: discord.Guild | None) -> dict[str, str]:
        """サーバー絵文字およびアプリ絵文字の {名前: '<:name:id>'} マップを取得する。"""
        emoji_map: dict[str, str] = {}

        # 1. サーバーカスタム絵文字
        if guild is not None:
            for emoji in guild.emojis:
                tag = f"<a:{emoji.name}:{emoji.id}>" if emoji.animated else f"<:{emoji.name}:{emoji.id}>"
                emoji_map[emoji.name] = tag

        # 2. ボットのアプリ絵文字（キャッシュ）
        if not self._app_emojis_fetched:
            try:
                app_emojis = await self.bot.fetch_application_emojis()
                for emoji in app_emojis:
                    tag = f"<a:{emoji.name}:{emoji.id}>" if emoji.animated else f"<:{emoji.name}:{emoji.id}>"
                    self._app_emojis_cache[emoji.name] = tag
                self._app_emojis_fetched = True
            except Exception:
                pass

        for name, tag in self._app_emojis_cache.items():
            emoji_map.setdefault(name, tag)

        # 3. ボット参加中の他ギルド絵文字もフォールバックとして統合
        for emoji in self.bot.emojis:
            tag = f"<a:{emoji.name}:{emoji.id}>" if emoji.animated else f"<:{emoji.name}:{emoji.id}>"
            emoji_map.setdefault(emoji.name, tag)

        return emoji_map

    async def cog_load(self) -> None:
        self.bot.add_view(ProxyLauncherView(self))
        self.bot.add_dynamic_items(ProxyDeleteButton)

    async def cog_unload(self) -> None:
        self.bot.remove_dynamic_items(ProxyDeleteButton)
        self.bot.tree.remove_command(self.reply_ctx_menu.name, type=self.reply_ctx_menu.type)
        # 進行中のスロットリングタスクをすべてキャンセル
        for task in self._pending_throttle_tasks.values():
            if not task.done():
                task.cancel()

    async def _repost_launcher_now(
        self,
        channel: discord.abc.Messageable,
        clicked_launcher: discord.Message | None = None,
    ) -> None:
        """待機ボタンを安全に削除して最下部へ再送信する。"""
        async with self._get_lock(channel.id):
            await self._delete_previous_launcher(channel, clicked_launcher)
            await self.post_launcher(channel)
            self._last_launcher_repost_time[channel.id] = time.monotonic()

    def schedule_launcher_repost(
        self,
        channel: discord.abc.Messageable,
        clicked_launcher: discord.Message | None = None,
    ) -> asyncio.Task[None]:
        """待機ボタンの再送信を5秒ごとにスロットリングしてスケジュールする。

        5秒以内に連続で呼び出された場合は、最新のメッセージが送信された後に
        最下部に必ず1度だけボタンが再送信されるようにタイマー制御（トレーリングスロットル）します。
        """
        channel_id = channel.id

        if channel_id in self._pending_throttle_tasks and not self._pending_throttle_tasks[channel_id].done():
            self._has_pending_throttle_request[channel_id] = True
            return self._pending_throttle_tasks[channel_id]

        async def _worker() -> None:
            try:
                while True:
                    self._has_pending_throttle_request[channel_id] = False
                    now = time.monotonic()
                    last_time = self._last_launcher_repost_time.get(channel_id, 0.0)
                    elapsed = now - last_time
                    wait_sec = LAUNCHER_THROTTLE_SECONDS - elapsed
                    if wait_sec > 0:
                        await asyncio.sleep(wait_sec)

                    await self._repost_launcher_now(channel, clicked_launcher)

                    # スリープ中や再送信処理中にさらにリクエストが来ていた場合はもう一度最新位置へ更新
                    if not self._has_pending_throttle_request.get(channel_id, False):
                        break
            except asyncio.CancelledError:
                pass
            finally:
                self._pending_throttle_tasks.pop(channel_id, None)

        task = asyncio.create_task(_worker())
        self._pending_throttle_tasks[channel_id] = task
        return task

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """通常メッセージが送られた際にも待機ボタンを最新位置（最下部）へ再送信する（5秒スロットリング）。"""
        # Bot 自身の送信（Webhook含む）やギルド外メッセージは無視
        if message.guild is None or message.author.bot or message.webhook_id is not None:
            return

        channel = message.channel
        # 該当チャンネルに一度でも /proxy が設置されている場合のみ発動
        if channel.id not in self._latest_launcher_ids:
            return

        self.schedule_launcher_repost(channel)

    async def _context_menu_toggle_reply(
        self, interaction: discord.Interaction, message: discord.Message
    ) -> None:
        """メッセージ右クリックで返信先をセット/トグル解除する。"""
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message(
                "サーバー内のチャンネルでだけ利用できるからね。そして天は鳴き、大地は震えるだろうね", ephemeral=True
            )
            return

        key = (interaction.user.id, interaction.channel.id)
        current_data = self._pending_replies.get(key)
        current_target_id = current_data[0] if current_data else None

        if current_target_id == message.id:
            # 同じメッセージに対して2回発火された場合はキャンセル
            del self._pending_replies[key]
            await interaction.response.send_message(
                "返信先の指定を解除したゾ。喜ぶん宿",
                ephemeral=True,
            )
        else:
            # 新規セットまたは別メッセージで上書き（本文キャッシュを保持）
            raw_content = message.clean_content or message.content or ""
            # 返信付きメッセージの場合は1行目を除去して本文のみ記録
            content_preview = strip_reply_header(raw_content)
            self._pending_replies[key] = (message.id, content_preview)

            display_author = message.author.display_name
            proxy_author_id = extract_proxy_author_id(message)
            if proxy_author_id is not None and interaction.guild is not None:
                real_member = interaction.guild.get_member(proxy_author_id)
                if real_member:
                    display_author = real_member.display_name

            display_snippet = content_preview if content_preview else "(添付ファイルなど)"
            if len(display_snippet) > 30:
                display_snippet = display_snippet[:27] + "..."
            await interaction.response.send_message(
                f"返信先をセットしたゾ。オッスお願いしまーす（次回 `/say` 送信時に引用）:\n"
                f"> **{display_author}**: {display_snippet}\n"
                f"※ 同じメッセージで再度「返信先にセット」を実行すると解除できるゾ",
                ephemeral=True,
            )

    async def post_launcher(self, channel: discord.abc.Messageable) -> discord.Message:
        """指定チャンネルに送信開始ボタン付きメッセージを送信し、最新 ID を記録する。"""
        message = await channel.send(
            embed=build_launcher_embed(),
            view=ProxyLauncherView(self),
        )
        self._latest_launcher_ids[channel.id] = message.id
        return message

    async def _delete_previous_launcher(
        self,
        channel: discord.abc.Messageable,
        clicked_launcher: discord.Message | None,
    ) -> None:
        latest_id = self._latest_launcher_ids.get(channel.id)
        if latest_id is not None:
            try:
                partial = channel.get_partial_message(latest_id)
                await partial.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        if clicked_launcher is not None and clicked_launcher.id != latest_id:
            try:
                await clicked_launcher.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    async def send_proxy_message(
        self,
        interaction: discord.Interaction,
        content: str,
        clicked_launcher: discord.Message | None,
        *,
        repost_launcher: bool = True,
        reply_message: discord.Message | None = None,
        reply_content_override: str | None = None,
        mention_author: bool = True,
    ) -> None:
        """Webhook で代替メッセージを送信し、必要に応じて待機ボタンを再送信する。"""
        channel = interaction.channel
        if channel is None or interaction.guild is None:
            raise ProxyError("この機能はサーバー内のチャンネルでだけ利用できるからね。そして天は鳴き、大地は震えるだろうね")

        if self.bot.user is None:
            raise ProxyError("Bot の初期化ができてないゾ。ちょっと歯ぁ当たんよ～")

        bot_user_id = self.bot.user.id
        async with self._get_lock(channel.id):
            target_channel, thread = await resolve_webhook_target(channel)
            webhook = await get_or_create_proxy_webhook(target_channel, bot_user_id)

            username = (interaction.user.display_name or "User")[:80]
            avatar_url = interaction.user.display_avatar.url

            # 絵文字ショートコード (:name:) の自動展開
            emoji_map = await self.get_available_emojis(interaction.guild)
            processed_content = expand_custom_emojis(content, emoji_map)

            full_content = processed_content
            if reply_message is not None:
                reply_header = format_reply_header(
                    reply_message, cached_content=reply_content_override
                )
                full_content = f"{reply_header}\n{processed_content}"

            # メンション入切: 切った場合は users=False とすることで、
            # <@123> の青文字メンションリンク表示を維持しつつ黄色ハイライトと通知（Ping）を無効化する
            allowed_mentions = discord.AllowedMentions(
                everyone=False,
                roles=False,
                users=True if mention_author else False,
            )

            send_kwargs: dict[str, Any] = {
                "content": full_content,
                "username": username,
                "avatar_url": avatar_url,
                "allowed_mentions": allowed_mentions,
                "view": ProxyMessageView(author_id=interaction.user.id),
                "wait": True,
            }
            if thread is not None:
                send_kwargs["thread"] = thread

            await webhook.send(**send_kwargs)

        if repost_launcher:
            self.schedule_launcher_repost(channel, clicked_launcher)

    @app_commands.command(
        name="proxy",
        description="メッセージ代替送信用の待機ボタンをこのチャンネルに設置します",
    )
    @app_commands.guild_only()
    async def setup_proxy(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if channel is None:
            await interaction.response.send_message(
                "チャンネル情報を取得できなかったゾ。お兄さん許して～", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            async with self._get_lock(channel.id):
                await self._delete_previous_launcher(channel, None)
                await self.post_launcher(channel)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "送信開始メッセージの設置に失敗たゾ。（権限不足の可能性）濃いすか？",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            "代替送信パネル、工事完了です...", ephemeral=True
        )

    @app_commands.command(
        name="say",
        description="メッセージをボットが代わりに送信するゾ",
    )
    @app_commands.describe(
        message="送信するメッセージ内容",
        reply_to="返信先メッセージ（候補から選択、またはメッセージID）",
        mention="返信時に相手へ通知するか（デフォルト: ON）",
    )
    @app_commands.guild_only()
    async def say_proxy(
        self,
        interaction: discord.Interaction,
        message: app_commands.Range[str, 1, 2000],
        reply_to: str | None = None,
        mention: bool = True,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        guild = interaction.guild
        if channel is None or guild is None:
            await interaction.followup.send(
                "サーバー内のチャンネルでだけ利用できるからね。そして天は鳴き、大地は震えるだろうね", ephemeral=True
            )
            return

        reply_message: discord.Message | None = None
        reply_content_override: str | None = None

        # 1. 引数 reply_to が指定された場合（案4）
        target_msg_id: int | None = None
        if reply_to:
            try:
                target_msg_id = int(reply_to.strip())
            except ValueError:
                pass

        # 2. 引数がなく、コンテキストメニューで事前にセットされていた場合（案3）
        pending_key = (interaction.user.id, channel.id)
        if target_msg_id is None:
            pending_data = self._pending_replies.get(pending_key)
            if pending_data:
                target_msg_id, reply_content_override = pending_data

        if target_msg_id is not None:
            try:
                reply_message = await channel.fetch_message(target_msg_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                reply_message = None

        try:
            # チャンネルに待機パネルが設置されている場合は、/say 実行後も最下部へ再送信する
            should_repost = channel.id in self._latest_launcher_ids
            await self.send_proxy_message(
                interaction=interaction,
                content=message,
                clicked_launcher=None,
                repost_launcher=should_repost,
                reply_message=reply_message,
                reply_content_override=reply_content_override,
                mention_author=mention,
            )
            # 送信成功したら案3の記憶は消費
            self._pending_replies.pop(pending_key, None)
        except ProxyError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except discord.HTTPException as exc:
            logger.warning("直接代替送信に失敗しました: %s", exc)
            await interaction.followup.send(
                "送信ダメみたいですね（失敗）ちょっと待ってから送りなおしてくれよな～頼むよ～",
                ephemeral=True,
            )
        else:
            await interaction.delete_original_response()

    @say_proxy.autocomplete("reply_to")
    async def say_reply_to_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """直近のチャンネル内メッセージをサジェストする（案4）。"""
        channel = interaction.channel
        if channel is None or not hasattr(channel, "history"):
            return []

        choices: list[app_commands.Choice[str]] = []
        try:
            async for msg in channel.history(limit=15):
                # 待機パネルメッセージ自身などは除外
                if (
                    msg.author.id == interaction.client.user.id
                    and msg.embeds
                    and msg.embeds[0].title == "メッセージ送信したりしなかったりしろ"
                ):
                    continue

                author_name = msg.author.display_name
                proxy_author_id = extract_proxy_author_id(msg)
                if proxy_author_id is not None and interaction.guild is not None:
                    real_member = interaction.guild.get_member(proxy_author_id)
                    if real_member:
                        author_name = real_member.display_name

                raw_content = msg.clean_content or msg.content or ""
                # 返信メッセージの場合は1行目（ヘッダー）を除外して2行目から表示
                content = strip_reply_header(raw_content)

                if content:
                    content_snippet = " ".join(content.split())
                    if len(content_snippet) > 40:
                        content_snippet = content_snippet[:37] + "..."
                else:
                    if msg.attachments:
                        content_snippet = "(添付ファイル)"
                    elif msg.embeds:
                        content_snippet = "(埋め込み)"
                    else:
                        # Message Content Intent が無効な場合は投稿日時を表示
                        created_jst = msg.created_at.strftime("%m/%d %H:%M")
                        content_snippet = f"({created_jst}のメッセージ)"

                label = f"{author_name}: {content_snippet}"[:100]
                value = str(msg.id)

                if current:
                    if current.lower() in label.lower() or current in value:
                        choices.append(app_commands.Choice(name=label, value=value))
                else:
                    choices.append(app_commands.Choice(name=label, value=value))

                if len(choices) >= 10:
                    break
        except (discord.Forbidden, discord.HTTPException):
            return []

        return choices


async def setup(bot: YajuBot) -> None:
    await bot.add_cog(ProxyCog(bot))
