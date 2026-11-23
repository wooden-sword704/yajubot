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
from yajubot.storage import OWNER_USER_ID, ProxyMessageRecord, ProxyStorage

logger = logging.getLogger(__name__)

WEBHOOK_NAME = "yajubot-proxy"
LAUNCHER_CUSTOM_ID = "proxy:open_modal"
LAUNCHER_THROTTLE_SECONDS = 5.0
PROXY_USER_COOLDOWN_SECONDS = 2.0
MAX_CACHE_ENTRIES = 5000
DISCORD_MESSAGE_MAX_LENGTH = 2000

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


# 返信1行目のヘッダーパターン（削除検知および編集時のヘッダー保持用）
REPLY_HEADER_PATTERN = re.compile(
    r"(?P<emojis>(?:<:reply1:1571547845683712101>|:reply1:)(?:<:reply2:1571547847264829490>|:reply2:))"
    r"(?P<summary>(?:@|<@!?\d+>).*?\s*:\s*\[.*?\])"
    r"(?P<link>\((?P<url>https://discord\.com/channels/\d+/\d+/(?P<msg_id>\d+))\))?"
)


def extract_reply_header(text: str) -> str | None:
    """メッセージ本文から1行目の返信ヘッダー（存在する場合）を取得する。"""
    if not text:
        return None
    lines = text.split("\n")
    if len(lines) > 1:
        first_line = lines[0]
        if REPLY1_EMOJI in first_line or ":reply1:" in first_line:
            return first_line
    return None


def mark_reply_header_as_deleted(
    text: str, deleted_message_id: int | None = None
) -> tuple[str, bool]:
    """返信ヘッダー内の「@ユーザー名 :[内容]」を「削除されたメッセージ」に置換する。

    deleted_message_id が指定されている場合、一致したメッセージ ID 宛てのヘッダーのみ置換する。
    置換が行われた場合は (新テキスト, True) を返す。
    """
    if not text:
        return text, False
    lines = text.split("\n")
    if not lines:
        return text, False
    first_line = lines[0]
    m = REPLY_HEADER_PATTERN.search(first_line)
    if not m:
        return text, False
    if deleted_message_id is not None:
        raw_msg_id = m.group("msg_id")
        if raw_msg_id is not None and int(raw_msg_id) != deleted_message_id:
            return text, False
    new_first_line = REPLY_HEADER_PATTERN.sub(r"\g<emojis>削除されたメッセージ", first_line)
    lines[0] = new_first_line
    return "\n".join(lines), True


# ゼロ幅文字を用いた不可視ユーザー ID エンコーディング（メッセージ本文に埋め込み）
ZW_0 = "\u200b"
ZW_1 = "\u200c"
ZW_WRAP = "\u200d"
INVISIBLE_META_PATTERN = re.compile(f"{ZW_WRAP}[{ZW_0}{ZW_1}]+{ZW_WRAP}")


def strip_invisible_meta(text: str) -> str:
    """テキストから不可視ゼロ幅メタデータを除去する。"""
    if not text:
        return ""
    return INVISIBLE_META_PATTERN.sub("", text)


def extract_editable_content(raw_content: str) -> str:
    """編集用ポップアップのテキストフィールドに初期入力する本文を抽出する。

    不可視メタデータおよび返信ヘッダー（存在する場合）を取り除いた純粋な本文を返す。
    """
    cleaned = strip_invisible_meta(raw_content)
    lines = cleaned.split("\n")
    if len(lines) > 1:
        first_line = lines[0]
        if REPLY1_EMOJI in first_line or ":reply1:" in first_line:
            return "\n".join(lines[1:]).strip()
    elif len(lines) == 1:
        if REPLY1_EMOJI in lines[0] or ":reply1:" in lines[0]:
            return ""
    return cleaned.strip()


def is_ratelimited_error(error: Exception) -> bool:
    """発生した例外がレート制限（429 / RateLimited / CommandOnCooldown）であるかを判定する。"""
    if isinstance(error, (discord.RateLimited, app_commands.CommandOnCooldown)):
        return True
    if isinstance(error, discord.HTTPException) and getattr(error, "status", None) == 429:
        return True
    return False


def extract_retry_after(error: Exception) -> float | None:
    """レート制限例外からリトライ可能秒数を抽出する。"""
    if isinstance(error, (discord.RateLimited, app_commands.CommandOnCooldown)):
        return getattr(error, "retry_after", None)
    if isinstance(error, discord.HTTPException) and getattr(error, "status", None) == 429:
        text = getattr(error, "text", "") or str(error)
        m = re.search(r"retry[_-]?after.*?([0-9.]+)", text, re.IGNORECASE)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return None


def format_ratelimit_message(
    error: Exception | None = None, retry_after: float | None = None
) -> str:
    """レート制限発生時の案内メッセージを生成する。"""
    if retry_after is None and error is not None:
        retry_after = extract_retry_after(error)
    if retry_after is not None and retry_after > 0:
        return f"レート制限冷えてるか～？あと {retry_after:.1f} 秒待ってからやり直してくれよな～頼むよ～"
    return "レート制限冷えてるか～？ちょっと時間をおいてからやり直してくれよな～頼むよ～"


def encode_proxy_author_id(author_id: int) -> str:
    """ユーザー ID を不可視のゼロ幅文字列にエンコードする。"""
    bin_str = bin(author_id)[2:]
    encoded = "".join(ZW_1 if b == "1" else ZW_0 for b in bin_str)
    return f"{ZW_WRAP}{encoded}{ZW_WRAP}"


def decode_proxy_author_id(text: str) -> int | None:
    """テキスト末尾に埋め込まれた不可視ゼロ幅文字から送信者ユーザー ID を復元する。

    ユーザー入力への偽装対策として、テキスト中に複数含まれる場合は最後のブロックを採用する。
    """
    if not text:
        return None
    matches = re.findall(f"{ZW_WRAP}([{ZW_0}{ZW_1}]+){ZW_WRAP}", text)
    if not matches:
        return None
    bin_str = "".join("1" if c == ZW_1 else "0" for c in matches[-1])
    try:
        return int(bin_str, 2)
    except ValueError:
        return None


def can_manage_bot(
    user: discord.abc.User,
    storage: ProxyStorage | None,
    guild_id: int | None,
) -> bool:
    """Bot の管理設定変更（/admin）やパネル設置（/proxy）が許可されているか判定する。

    1. 例外アカウント（OWNER_USER_ID）は常に許可
    2. サーバーの管理者権限（Administrator）保持者は許可
    3. /admin で管理ロールが設定されている場合、そのロール保持者を許可
    """
    if getattr(user, "id", None) == OWNER_USER_ID:
        return True
    perms = getattr(user, "guild_permissions", None)
    if isinstance(perms, discord.Permissions) and perms.administrator:
        return True
    if storage is not None and guild_id is not None:
        settings = storage.get_guild_settings(guild_id)
        if settings.admin_role_ids:
            user_roles = getattr(user, "roles", None)
            if user_roles:
                user_role_ids = {r.id for r in user_roles}
                if user_role_ids & set(settings.admin_role_ids):
                    return True
    return False


def can_use_proxy(
    user: discord.abc.User,
    storage: ProxyStorage | None,
    guild_id: int | None,
) -> bool:
    """プロキシ発信（/say・ほらいくどー・返信）が許可されているか判定する。

    1. 管理権限保持者（例外アカウント・サーバー管理者・管理ロール）は常に許可
    2. /admin で発信許可ロールが設定されている場合、そのロール保持者のみ許可
    3. 未設定時は一般メンバーには許可しない
    """
    if can_manage_bot(user, storage, guild_id):
        return True
    if storage is not None and guild_id is not None:
        settings = storage.get_guild_settings(guild_id)
        if not settings.proxy_role_ids:
            return False
        user_roles = getattr(user, "roles", None)
        if user_roles:
            user_role_ids = {r.id for r in user_roles}
            return bool(user_role_ids & set(settings.proxy_role_ids))
    return False


def extract_proxy_author_id(
    message: discord.Message,
    *,
    storage: ProxyStorage | None = None,
) -> int | None:
    """メッセージがプロキシ送信（Webhook）されたものである場合、送信者ユーザー ID を抽出する。

    1. DB（暗号化ログストア）のメッセージ ID 照合（偽装不可の正情報）
    2. Webhook メッセージ本文末尾に埋め込まれた不可視ゼロ幅文字（DB未登録の過去メッセージ用）
    3. 旧仕様の削除ボタンコンポーネントの custom_id (proxy:delete:<id>)
    の順で判定する。
    """
    # 1. DB からの確実な照合（偽装不可能）
    if storage is not None and isinstance(getattr(message, "id", None), int):
        db_author_id = storage.get_proxy_author_id(message.id)
        if db_author_id is not None:
            return db_author_id

    # 2. 不可視文字からの復元（一般ユーザーの通常メッセージによる偽装を防止）
    webhook_id = getattr(message, "webhook_id", None)
    raw_content = getattr(message, "content", "")
    if webhook_id is not None and isinstance(raw_content, str):
        decoded_id = decode_proxy_author_id(raw_content)
        if decoded_id is not None:
            return decoded_id

    # 3. 旧仕様のコンポーネント (proxy:delete:<author_id>) からのフォールバック
    if hasattr(message, "components"):
        for row in getattr(message, "components", []):
            for child in getattr(row, "children", []):
                custom_id = getattr(child, "custom_id", None)
                if custom_id and isinstance(custom_id, str):
                    m = re.match(r"^proxy:delete:(?P<author_id>\d+)$", custom_id)
                    if m:
                        return int(m.group("author_id"))
    return None


def ensure_channel_send_permission(channel: Any, user: discord.abc.User) -> None:
    """実行ユーザーが対象チャンネルでメッセージを送信する権限を持つか検証する。"""
    if not isinstance(user, discord.Member) or not hasattr(channel, "permissions_for"):
        return
    perms = channel.permissions_for(user)
    if not isinstance(perms, discord.Permissions):
        return
    if isinstance(channel, discord.Thread):
        if not perms.send_messages_in_threads:
            raise ProxyError("このスレッドでメッセージを送信する権限がないゾ。")
    elif not perms.send_messages:
        raise ProxyError("このチャンネルでメッセージを送信する権限がないゾ。")


def format_reply_header(
    target_message: discord.Message,
    cached_content: str | None = None,
    *,
    storage: ProxyStorage | None = None,
) -> str:
    """返信先メッセージの情報を基に、指定フォーマットの返信ヘッダーテキストを生成する。

    形式:
    <:reply1:1571547845683712101><:reply2:1571547847264829490>@返信先ユーザー :[クランプした内容](メッセージリンク)
    ※ Webhook メッセージへの返信の場合、Webhook 自身ではなく元の送信者ユーザーにメンションする。
    ※ 添付ファイルがある場合は内容部分を「添付ファイル」とする。
    ※ 返信先が返信メッセージの場合は1行目のヘッダーを除外して本文を読み込む。
    """
    author = target_message.author

    # Webhookメッセージへの返信の場合、DBまたは不可視メタデータから実際の送信者ユーザーIDを取得
    proxy_author_id = extract_proxy_author_id(target_message, storage=storage)
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


def build_edit_audit_embed(
    *,
    author_id: int,
    channel_id: int,
    message_id: int,
    old_content: str | None,
    new_content: str,
    edited_at: str,
    jump_url: str | None = None,
) -> discord.Embed:
    """ログチャンネル送信用のプロキシメッセージ編集ログ Embed を生成する。"""
    embed = discord.Embed(
        title="✏️ プロキシメッセージ編集ログ",
        color=discord.Color.orange(),
    )
    embed.add_field(name="送信者", value=f"<@{author_id}> (`{author_id}`)", inline=True)
    embed.add_field(name="チャンネル", value=f"<#{channel_id}> (`{channel_id}`)", inline=True)
    embed.add_field(name="編集日時", value=edited_at, inline=True)
    if old_content is not None:
        old_snip = old_content if len(old_content) <= 900 else old_content[:897] + "..."
        embed.add_field(name="編集前", value=old_snip or "(空)", inline=False)
    new_snip = new_content if len(new_content) <= 900 else new_content[:897] + "..."
    embed.add_field(name="編集後", value=new_snip or "(空)", inline=False)
    if jump_url:
        embed.add_field(name="メッセージリンク", value=jump_url, inline=False)
    embed.set_footer(text=f"メッセージ ID: {message_id}")
    return embed


def build_delete_audit_embed(record: ProxyMessageRecord) -> discord.Embed:
    """ログチャンネル送信用のプロキシメッセージ削除ログ Embed を生成する。"""
    embed = discord.Embed(
        title="🗑️ プロキシメッセージ削除ログ",
        color=discord.Color.red(),
    )
    embed.add_field(name="送信者", value=f"<@{record.author_id}> (`{record.author_id}`)", inline=True)
    embed.add_field(name="チャンネル", value=f"<#{record.channel_id}> (`{record.channel_id}`)", inline=True)
    embed.add_field(name="削除日時", value=record.deleted_at or "不明", inline=True)
    embed.add_field(name="元の送信日時", value=record.created_at, inline=True)
    snip = record.content if len(record.content) <= 1000 else record.content[:997] + "..."
    embed.add_field(name="削除された内容", value=snip or "(空)", inline=False)
    embed.set_footer(text=f"メッセージ ID: {record.proxy_msg_id}")
    return embed


async def send_audit_log(
    bot: Any,
    guild_id: int | None,
    embed: discord.Embed,
) -> None:
    """ギルドに設定されたログチャンネルへ監査ログ Embed を送信する。"""
    if guild_id is None:
        return
    storage = getattr(bot, "storage", None)
    if storage is None:
        return
    settings = storage.get_guild_settings(guild_id)
    if settings.log_channel_id is None:
        return

    log_channel = bot.get_channel(settings.log_channel_id)
    if log_channel is None:
        try:
            log_channel = await bot.fetch_channel(settings.log_channel_id)
        except Exception:
            return
    if log_channel is None or not hasattr(log_channel, "send"):
        return

    try:
        await log_channel.send(embed=embed)
    except Exception as exc:
        logger.debug("監査ログチャンネルへの送信に失敗しました: %s", exc)


class ConfirmDeleteView(discord.ui.View):
    """送信者本人向けのエフェメラル削除確認ダイアログ。"""

    def __init__(
        self,
        target_message: discord.Message,
        author_id: int,
        prompt_interaction: discord.Interaction | None = None,
    ) -> None:
        super().__init__(timeout=60)
        self.target_message = target_message
        self.author_id = author_id
        self.prompt_interaction = prompt_interaction

    async def on_timeout(self) -> None:
        """タイムアウト（60秒放置）時にボタンを無効化してメッセージを更新する。"""
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        try:
            if self.prompt_interaction is not None:
                await self.prompt_interaction.edit_original_response(
                    content="時間切れだゾ。削除ボタンは無効化されたゾ。",
                    view=self,
                )
        except Exception:
            pass

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
            storage = getattr(interaction.client, "storage", None)
            if storage is not None:
                if storage.mark_proxy_message_deleted(self.target_message.id):
                    rec = storage.get_proxy_message(self.target_message.id)
                    if rec is not None:
                        await send_audit_log(
                            interaction.client,
                            rec.guild_id,
                            build_delete_audit_embed(rec),
                        )
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


class ProxyMessageModal(discord.ui.Modal):
    """代替送信するメッセージ内容を入力するポップアップモーダル。"""

    def __init__(
        self,
        cog: ProxyCog,
        launcher_message: discord.Message | None,
        *,
        is_reply: bool = False,
        reply_label: str | None = None,
    ) -> None:
        title = "返信メッセージを送信" if is_reply else "メッセージを送信"
        super().__init__(title=title, timeout=300)
        self.cog = cog
        self.launcher_message = launcher_message
        self.is_reply = is_reply

        self.mention_select: discord.ui.Label[discord.ui.Select] | None = None
        if is_reply:
            # 返信時のみメンション通知の入切セレクトを表示
            self.mention_select = discord.ui.Label(
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
            self.add_item(self.mention_select)

        input_label = "送信するメッセージを書き込み"
        if is_reply and reply_label:
            input_label = reply_label[:45]

        self.message_input: discord.ui.Label[discord.ui.TextInput] = discord.ui.Label(
            text=input_label,
            component=discord.ui.TextInput(
                custom_id="proxy:modal_message_input",
                style=discord.TextStyle.paragraph,
                placeholder="ナニガ・キモチーノ",
                required=True,
                max_length=2000,
            ),
        )
        self.add_item(self.message_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        # メンション選択値（デフォルト: True）
        mention_author = True
        if self.mention_select is not None:
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
        except Exception as exc:
            if is_ratelimited_error(exc):
                await interaction.followup.send(format_ratelimit_message(exc), ephemeral=True)
            elif isinstance(exc, discord.HTTPException):
                logger.warning("エラー！滅！！！！: %s", exc)
                await interaction.followup.send(
                    "送信ダメみたいですね（失敗）ちょっと待ってから送りなおしてくれよな～頼むよ～",
                    ephemeral=True,
                )
            else:
                raise exc
        else:
            await interaction.delete_original_response()

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        logger.error("ProxyMessageModal 処理中に予期せぬエラーが発生しました。", exc_info=error)
        msg = "処理中にエラーが発生したゾ。ちょっと時間をおいてやり直してくれよな～"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


class ProxyEditModal(discord.ui.Modal, title="メッセージを編集"):
    """送信済みプロキシメッセージを編集するモーダル。"""

    def __init__(
        self,
        cog: ProxyCog,
        target_message: discord.Message,
        author_id: int,
        initial_content: str,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.target_message = target_message
        self.author_id = author_id

        self.message_input = discord.ui.TextInput(
            label="編集後のメッセージ",
            style=discord.TextStyle.paragraph,
            default=initial_content[:2000] if initial_content else None,
            placeholder="メッセージを入力してくれよな～頼むよ～" if not initial_content else None,
            required=True,
            max_length=2000,
        )
        self.add_item(self.message_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        new_content = self.message_input.value or ""
        channel = interaction.channel
        guild = interaction.guild

        if channel is None or guild is None:
            await interaction.followup.send(
                "サーバー内のチャンネルでだけ利用できるからね。そして天は鳴き、大地は震えるだろうね",
                ephemeral=True,
            )
            return

        bot_user = self.cog.bot.user
        if bot_user is None:
            await interaction.followup.send("Bot の初期化ができてないゾ。", ephemeral=True)
            return

        try:
            ensure_channel_send_permission(channel, interaction.user)
            target_channel, thread = await resolve_webhook_target(channel)
            webhook = await get_or_create_proxy_webhook(target_channel, bot_user.id)

            # ユーザー入力内のゼロ幅偽装メタデータを除去してから絵文字展開
            sanitized_content = strip_invisible_meta(new_content)
            emoji_map = await self.cog.get_available_emojis(guild)
            processed_content = expand_custom_emojis(sanitized_content, emoji_map)

            # 既存メッセージの返信ヘッダーを保持
            raw_target_content = getattr(self.target_message, "content", "") or ""
            existing_header = extract_reply_header(raw_target_content)

            if existing_header:
                full_content = f"{existing_header}\n{processed_content}"
            else:
                full_content = processed_content

            # 不可視メタデータを付与して本人識別を維持
            invisible_meta = encode_proxy_author_id(self.author_id)
            final_content = f"{full_content}{invisible_meta}"

            if len(final_content) > DISCORD_MESSAGE_MAX_LENGTH:
                raise ProxyError(
                    f"文字数制限（{DISCORD_MESSAGE_MAX_LENGTH}文字）を超えているゾ（現在 {len(final_content)} 文字）。"
                    "もう少し短くしてくれよな～頼むよ～"
                )

            edit_kwargs: dict[str, Any] = {
                "message_id": self.target_message.id,
                "content": final_content,
            }
            if thread is not None:
                edit_kwargs["thread"] = thread

            await webhook.edit_message(**edit_kwargs)
            old_content, edited_at = self.cog.bot.storage.update_proxy_message_content(
                self.target_message.id, processed_content
            )
            await send_audit_log(
                self.cog.bot,
                guild.id,
                build_edit_audit_embed(
                    author_id=self.author_id,
                    channel_id=channel.id,
                    message_id=self.target_message.id,
                    old_content=old_content,
                    new_content=processed_content,
                    edited_at=edited_at,
                    jump_url=getattr(self.target_message, "jump_url", None),
                ),
            )
            await interaction.followup.send("メッセージを編集したゾ", ephemeral=True)
        except ProxyError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
        except Exception as exc:
            if is_ratelimited_error(exc):
                await interaction.followup.send(format_ratelimit_message(exc), ephemeral=True)
            elif isinstance(exc, discord.HTTPException):
                logger.warning("メッセージ編集に失敗しました: %s", exc)
                await interaction.followup.send(
                    "編集ダメみたいですね（失敗）ちょっと待ってからやり直してくれよな～頼むよ～",
                    ephemeral=True,
                )
            else:
                raise exc

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        logger.error("ProxyEditModal 処理中に予期せぬエラーが発生しました。", exc_info=error)
        msg = "編集中にエラーが発生したゾ。ちょっと時間をおいてやり直してくれよな～"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


class ReplyActionChoiceView(discord.ui.View):
    """返信コンテキストメニュー実行時に「直接返信」か「返信先に設定」かを選ばせる View。"""

    def __init__(
        self,
        cog: ProxyCog,
        target_message: discord.Message,
        content_preview: str,
        display_author: str,
        prompt_interaction: discord.Interaction | None = None,
    ) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.target_message = target_message
        self.content_preview = content_preview
        self.display_author = display_author
        self.prompt_interaction = prompt_interaction

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        try:
            if self.prompt_interaction is not None:
                await self.prompt_interaction.edit_original_response(
                    content="時間切れだゾ。もう一度やり直してくれよな～",
                    view=self,
                )
        except Exception:
            pass

    @discord.ui.button(label="直接返信する", emoji="💬", style=discord.ButtonStyle.primary)
    async def direct_reply_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        channel_id = interaction.channel.id if interaction.channel else interaction.channel_id
        if channel_id is None:
            return
        key = (interaction.user.id, channel_id)
        self.cog.set_pending_reply(key, (self.target_message.id, self.content_preview))

        reply_label = f"返信先: {self.display_author}"
        modal = ProxyMessageModal(
            cog=self.cog,
            launcher_message=None,
            is_reply=True,
            reply_label=reply_label,
        )
        await interaction.response.send_modal(modal)
        self.stop()

    @discord.ui.button(label="返信先に設定する", emoji="📌", style=discord.ButtonStyle.secondary)
    async def set_reply_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        channel_id = interaction.channel.id if interaction.channel else interaction.channel_id
        if channel_id is None:
            return
        key = (interaction.user.id, channel_id)
        self.cog.set_pending_reply(key, (self.target_message.id, self.content_preview))

        display_snippet = self.content_preview if self.content_preview else "(添付ファイルなど)"
        if len(display_snippet) > 30:
            display_snippet = display_snippet[:27] + "..."
        await interaction.response.edit_message(
            content=(
                f"返信先をセットしたゾ。オッスお願いしまーす（次回 `/say` または「ほらいくどー」送信時に引用）:\n"
                f"> **{self.display_author}**: {display_snippet}\n"
                f"※ 同じメッセージで再度「返信先にセット」を実行すると解除できるゾ"
            ),
            view=None,
        )
        self.stop()


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
        guild_id = interaction.guild.id if interaction.guild else None
        if not can_use_proxy(interaction.user, self.cog.bot.storage, guild_id):
            await interaction.response.send_message(
                "この機能を使う権限がないゾ。`/admin` で許可されたロールを確認してくれよな～",
                ephemeral=True,
            )
            return

        is_reply = False
        reply_label: str | None = None
        if interaction.channel is not None:
            pending_key = (interaction.user.id, interaction.channel.id)
            pending_data = self.cog._pending_replies.get(pending_key)
            if pending_data is not None:
                is_reply = True
                _, preview = pending_data
                snippet = preview[:20] + "..." if len(preview) > 20 else preview
                reply_label = f"返信中: {snippet}" if snippet else "返信中（返信メッセージを入力）"

        modal = ProxyMessageModal(
            cog=self.cog,
            launcher_message=interaction.message,
            is_reply=is_reply,
            reply_label=reply_label,
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

        # 削除連動用の返信先逆引きインデックス: reply_target_msg_id -> {(channel_id, proxy_msg_id)}
        self._reply_target_index: dict[int, set[tuple[int, int]]] = {}
        # ユーザー単位の送信クールダウン管理
        self._user_cooldowns: dict[int, float] = {}
        # プロキシが一度でも使われたチャンネルID（再起動後フォールバックの限定用）
        self._active_proxy_channels: set[int] = set()

        self.reply_ctx_menu = app_commands.ContextMenu(
            name="返信先にセット",
            callback=self._context_menu_toggle_reply,
        )
        self.bot.tree.add_command(self.reply_ctx_menu)

        self.delete_ctx_menu = app_commands.ContextMenu(
            name="メッセージを削除",
            callback=self._context_menu_delete_proxy,
        )
        self.bot.tree.add_command(self.delete_ctx_menu)

        self.edit_ctx_menu = app_commands.ContextMenu(
            name="メッセージを編集",
            callback=self._context_menu_edit_proxy,
        )
        self.bot.tree.add_command(self.edit_ctx_menu)

    def _trim_dict(self, target: dict[Any, Any], max_size: int) -> None:
        """メモリ肥大化防止のため、上限超過時に古いエントリから削除する。"""
        while len(target) > max_size:
            oldest_key = next(iter(target))
            del target[oldest_key]

    def set_pending_reply(
        self, key: tuple[int, int], value: tuple[int, str]
    ) -> None:
        """返信先をメモリ上限付きで保存する。"""
        if key in self._pending_replies:
            del self._pending_replies[key]
        self._pending_replies[key] = value
        self._trim_dict(self._pending_replies, MAX_CACHE_ENTRIES)

    def _register_reply_target(
        self, reply_target_id: int, channel_id: int, proxy_msg_id: int
    ) -> None:
        """返信元メッセージ削除時の高速更新用に逆引きインデックスへ登録する。"""
        bucket = self._reply_target_index.get(reply_target_id)
        if bucket is None:
            bucket = set()
            self._reply_target_index[reply_target_id] = bucket
        bucket.add((channel_id, proxy_msg_id))
        self._trim_dict(self._reply_target_index, MAX_CACHE_ENTRIES)

    def _get_lock(self, channel_id: int) -> asyncio.Lock:
        lock = self._channel_locks.get(channel_id)
        if lock is None:
            # 上限を超えた場合、未ロックの古いロックを掃除してメモリを抑える
            if len(self._channel_locks) >= 500:
                idle_keys = [
                    cid for cid, lk in self._channel_locks.items() if not lk.locked()
                ]
                for cid in idle_keys[: len(idle_keys) // 2 + 1]:
                    del self._channel_locks[cid]
            lock = asyncio.Lock()
            self._channel_locks[channel_id] = lock
        return lock

    async def get_available_emojis(self, guild: discord.Guild | None) -> dict[str, str]:
        """実行サーバーのカスタム絵文字およびアプリ絵文字の {名前: '<:name:id>'} マップを取得する。"""
        emoji_map: dict[str, str] = {}

        # 1. 実行サーバーのカスタム絵文字
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

        return emoji_map

    async def cog_load(self) -> None:
        self.bot.add_view(ProxyLauncherView(self))

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.reply_ctx_menu.name, type=self.reply_ctx_menu.type)
        self.bot.tree.remove_command(self.delete_ctx_menu.name, type=self.delete_ctx_menu.type)
        self.bot.tree.remove_command(self.edit_ctx_menu.name, type=self.edit_ctx_menu.type)
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

    async def _handle_message_delete_event(
        self, channel_id: int, deleted_message_id: int
    ) -> None:
        """メッセージ削除を検知し、削除ログ記録と該当返信概要の「削除されたメッセージ」更新を行う。"""
        # 1. 削除されたのがプロキシ送信メッセージ自身であれば削除済みフラグを立ててログチャンネルへ通知
        if self.bot.storage.mark_proxy_message_deleted(deleted_message_id):
            rec = self.bot.storage.get_proxy_message(deleted_message_id)
            if rec is not None:
                await send_audit_log(
                    self.bot,
                    rec.guild_id,
                    build_delete_audit_embed(rec),
                )

        # 2. 削除されたメッセージに返信していたプロキシメッセージを DB インデックスから O(1) 検索
        reply_targets = self.bot.storage.get_replies_to_target(deleted_message_id)
        if not reply_targets:
            return

        bot_user = self.bot.user
        if bot_user is None:
            return

        # チャンネルごとにまとめてピンポイントで更新（9000人規模でのAPI呼び出しを最小限に抑制）
        by_channel: dict[int, list[int]] = {}
        for ch_id, proxy_msg_id in reply_targets:
            by_channel.setdefault(ch_id, []).append(proxy_msg_id)

        for ch_id, msg_ids in by_channel.items():
            channel = self.bot.get_channel(ch_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(ch_id)
                except Exception:
                    continue

            if not hasattr(channel, "fetch_message"):
                continue

            try:
                target_channel, thread = await resolve_webhook_target(channel)
                webhook = await get_or_create_proxy_webhook(target_channel, bot_user.id)
            except Exception:
                continue

            for proxy_msg_id in msg_ids:
                try:
                    msg = await channel.fetch_message(proxy_msg_id)
                    raw_content = msg.content or ""
                    new_content, changed = mark_reply_header_as_deleted(
                        raw_content, deleted_message_id=deleted_message_id
                    )
                    if changed and new_content != raw_content:
                        edit_kwargs: dict[str, Any] = {
                            "message_id": msg.id,
                            "content": new_content,
                        }
                        if thread is not None:
                            edit_kwargs["thread"] = thread
                        await webhook.edit_message(**edit_kwargs)
                except Exception as exc:
                    logger.debug("削除連動の返信ヘッダー更新に失敗しました: %s", exc)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        """メッセージ削除検知リスナー。"""
        if payload.cached_message and payload.cached_message.guild is None:
            return
        await self._handle_message_delete_event(payload.channel_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(
        self, payload: discord.RawBulkMessageDeleteEvent
    ) -> None:
        """メッセージ一括削除検知リスナー。"""
        for msg_id in payload.message_ids:
            await self._handle_message_delete_event(payload.channel_id, msg_id)

    async def _context_menu_toggle_reply(
        self, interaction: discord.Interaction, message: discord.Message
    ) -> None:
        """メッセージ右クリックで返信先をセット/トグル解除する。"""
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message(
                "サーバー内のチャンネルでだけ利用できるからね。そして天は鳴き、大地は震えるだろうね", ephemeral=True
            )
            return

        if not can_use_proxy(interaction.user, self.bot.storage, interaction.guild.id):
            await interaction.response.send_message(
                "申し訳ないが権限がないのでNG",
                ephemeral=True,
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
            # 新規または別メッセージの場合は「直接返信する」「返信先に設定する」を選択させる
            raw_content = message.clean_content or message.content or ""
            # 返信付きメッセージの場合は1行目を除去して本文のみ記録
            content_preview = strip_reply_header(raw_content)

            display_author = message.author.display_name
            proxy_author_id = extract_proxy_author_id(message, storage=self.bot.storage)
            if proxy_author_id is not None and interaction.guild is not None:
                real_member = interaction.guild.get_member(proxy_author_id)
                if real_member:
                    display_author = real_member.display_name

            display_snippet = content_preview if content_preview else "(添付ファイルなど)"
            if len(display_snippet) > 30:
                display_snippet = display_snippet[:27] + "..."

            view = ReplyActionChoiceView(
                cog=self,
                target_message=message,
                content_preview=content_preview,
                display_author=display_author,
                prompt_interaction=interaction,
            )
            await interaction.response.send_message(
                f"返信のやり方を選んでくれよな～頼むよ～:\n"
                f"> **{display_author}**: {display_snippet}",
                view=view,
                ephemeral=True,
            )

    async def _context_menu_delete_proxy(
        self, interaction: discord.Interaction, message: discord.Message
    ) -> None:
        """メッセージ右クリック「メッセージを削除」から実行された場合の削除確認ハンドラ。"""
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message(
                "サーバー内のチャンネルでのみ利用できます。", ephemeral=True
            )
            return

        # プロキシメッセージ判定（DB 照合・不可視埋め込み ID・旧コンポーネント ID）
        author_id = extract_proxy_author_id(message, storage=self.bot.storage)

        if author_id is None:
            # 該当メッセージがボット自身の通常メッセージか確認（待機パネル等）
            if (
                interaction.client.user
                and message.author.id == interaction.client.user.id
                and interaction.user.guild_permissions.manage_messages
            ):
                author_id = interaction.user.id
            else:
                await interaction.response.send_message(
                    "これ代替送信メッセージじゃないやん！（削除でき）ないです",
                    ephemeral=True,
                )
                return

        if interaction.user.id != author_id:
            await interaction.response.send_message(
                "あっおい待てい（江戸っ子）、本人以外は削除できないゾ",
                ephemeral=True,
            )
            return

        view = ConfirmDeleteView(
            target_message=message,
            author_id=author_id,
            prompt_interaction=interaction,
        )
        await interaction.response.send_message(
            "メッセージ削除いいすかぁ？",
            view=view,
            ephemeral=True,
        )

    async def _context_menu_edit_proxy(
        self, interaction: discord.Interaction, message: discord.Message
    ) -> None:
        """メッセージ右クリック「メッセージを編集」から実行された場合の編集モーダル起動ハンドラ。"""
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message(
                "サーバー内のチャンネルでのみ利用できます。", ephemeral=True
            )
            return

        author_id = extract_proxy_author_id(message, storage=self.bot.storage)
        if author_id is None:
            await interaction.response.send_message(
                "これ代替送信メッセージじゃないやん！（編集でき）ないです",
                ephemeral=True,
            )
            return

        if interaction.user.id != author_id:
            await interaction.response.send_message(
                "あっおい待てい（江戸っ子）、本人以外は編集できないゾ",
                ephemeral=True,
            )
            return

        raw_content = getattr(message, "content", "") or ""
        initial_content = extract_editable_content(raw_content)

        modal = ProxyEditModal(
            cog=self,
            target_message=message,
            author_id=author_id,
            initial_content=initial_content,
        )
        await interaction.response.send_modal(modal)

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

        if not can_use_proxy(interaction.user, self.bot.storage, interaction.guild.id):
            raise ProxyError(
                "この機能を使う権限がないゾ。`/admin` で許可されたロールを確認してくれよな～"
            )

        ensure_channel_send_permission(channel, interaction.user)

        # 連打防止クールダウン（ユーザー単位 2 秒）
        now = time.monotonic()
        last_sent = self._user_cooldowns.get(interaction.user.id)
        if last_sent is not None:
            elapsed = now - last_sent
            if elapsed < PROXY_USER_COOLDOWN_SECONDS:
                remaining = PROXY_USER_COOLDOWN_SECONDS - elapsed
                raise ProxyError(
                    f"連打しすぎだゾ！あと {remaining:.1f} 秒待ってから送り直してくれよな～頼むよ～"
                )

        bot_user_id = self.bot.user.id
        async with self._get_lock(channel.id):
            target_channel, thread = await resolve_webhook_target(channel)
            webhook = await get_or_create_proxy_webhook(target_channel, bot_user_id)

            username = (interaction.user.display_name or "User")[:80]
            avatar_url = interaction.user.display_avatar.url

            # ユーザー入力内のゼロ幅偽装メタデータを除去してから絵文字展開
            sanitized_content = strip_invisible_meta(content)
            emoji_map = await self.get_available_emojis(interaction.guild)
            processed_content = expand_custom_emojis(sanitized_content, emoji_map)

            full_content = processed_content
            if reply_message is not None:
                reply_header = format_reply_header(
                    reply_message,
                    cached_content=reply_content_override,
                    storage=self.bot.storage,
                )
                full_content = f"{reply_header}\n{processed_content}"

            # メンション入切: 切った場合は users=False とすることで、
            # <@123> の青文字メンションリンク表示を維持しつつ黄色ハイライトと通知（Ping）を無効化する
            allowed_mentions = discord.AllowedMentions(
                everyone=False,
                roles=False,
                users=True if mention_author else False,
            )

            # 不可視ゼロ幅文字で送信者ユーザー ID を本文末尾に埋め込む（互換フォールバック用）
            invisible_meta = encode_proxy_author_id(interaction.user.id)
            final_content = f"{full_content}{invisible_meta}"

            if len(final_content) > DISCORD_MESSAGE_MAX_LENGTH:
                raise ProxyError(
                    f"文字数制限（{DISCORD_MESSAGE_MAX_LENGTH}文字）を超えているゾ（現在 {len(final_content)} 文字）。"
                    "もう少し短くしてくれよな～頼むよ～"
                )

            send_kwargs: dict[str, Any] = {
                "content": final_content,
                "username": username,
                "avatar_url": avatar_url,
                "allowed_mentions": allowed_mentions,
                "wait": True,
            }
            if thread is not None:
                send_kwargs["thread"] = thread

            sent_msg = await webhook.send(**send_kwargs)

            # クールダウン時刻の記録と上限管理
            self._user_cooldowns[interaction.user.id] = time.monotonic()
            self._trim_dict(self._user_cooldowns, MAX_CACHE_ENTRIES)

            # 暗号化ログ・返信ツリー逆引き用に DB へ記録
            sent_msg_id = getattr(sent_msg, "id", None)
            if isinstance(sent_msg_id, int):
                self.bot.storage.record_proxy_message(
                    proxy_msg_id=sent_msg_id,
                    guild_id=interaction.guild.id,
                    channel_id=channel.id,
                    author_id=interaction.user.id,
                    content=processed_content,
                    reply_target_id=reply_message.id if reply_message is not None else None,
                )

        if repost_launcher:
            self.schedule_launcher_repost(channel, clicked_launcher)

    @app_commands.command(
        name="proxy",
        description="メッセージ代替送信用の待機ボタンぶち込んでやるぜ",
    )
    @app_commands.guild_only()
    async def setup_proxy(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        guild = interaction.guild
        if channel is None or guild is None:
            await interaction.response.send_message(
                "チャンネル情報を取得できなかったゾ。お兄さん許して～", ephemeral=True
            )
            return

        if not can_manage_bot(interaction.user, self.bot.storage, guild.id):
            await interaction.response.send_message(
                "パネルの設置は管理者または許可された管理ロールのみ実行できるゾ。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            async with self._get_lock(channel.id):
                await self._delete_previous_launcher(channel, None)
                await self.post_launcher(channel)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "送信開始メッセージの設置に失敗したゾ。（権限不足の可能性）濃いすか？",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            "工事完了です...", ephemeral=True
        )

    @app_commands.command(
        name="proxy_remove",
        description="設置されたプロキシ送信パネルを削除するゾ",
    )
    @app_commands.guild_only()
    async def remove_proxy(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        guild = interaction.guild
        if channel is None or guild is None:
            await interaction.response.send_message(
                "チャンネル情報を取得できなかったゾ。お兄さん許して～", ephemeral=True
            )
            return

        if not can_manage_bot(interaction.user, self.bot.storage, guild.id):
            await interaction.response.send_message(
                "パネルの削除は管理者または許可された管理ロールのみ実行できるゾ。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        async with self._get_lock(channel.id):
            latest_id = self._latest_launcher_ids.get(channel.id)
            if latest_id is None:
                await interaction.followup.send(
                    "このチャンネルに設置されているプロキシパネルが見つからなかったゾ。",
                    ephemeral=True,
                )
                return
            await self._delete_previous_launcher(channel, None)
            self._latest_launcher_ids.pop(channel.id, None)
            self._active_proxy_channels.discard(channel.id)

        await interaction.followup.send(
            "プロキシパネルを削除したゾ。", ephemeral=True
        )

    @app_commands.command(
        name="say",
        description="メッセージをボットが代わりに送信するゾ。（メッセージが）いっぱいいっぱい裕次郎",
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
        except Exception as exc:
            if is_ratelimited_error(exc):
                await interaction.followup.send(format_ratelimit_message(exc), ephemeral=True)
            elif isinstance(exc, discord.HTTPException):
                logger.warning("直接代替送信に失敗しました: %s", exc)
                await interaction.followup.send(
                    "送信ダメみたいですね（失敗）ちょっと待ってから送りなおしてくれよな～頼むよ～",
                    ephemeral=True,
                )
            else:
                raise exc
        else:
            await interaction.delete_original_response()

    @say_proxy.autocomplete("reply_to")
    async def say_reply_to_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """直近のチャンネル内メッセージをサジェストする。"""
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
                proxy_author_id = extract_proxy_author_id(msg, storage=self.bot.storage)
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
