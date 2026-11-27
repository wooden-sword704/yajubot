"""AICord API を利用したチャンネル別キャラクター対話 Cog。"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands

from yajubot.bot import YajuBot

logger = logging.getLogger(__name__)

AICORD_AUTHORIZE_URL = "https://aicordapp.com/oauth/authorize"
AICORD_TOKEN_URL = "https://aicordapp.com/api/v1/oauth/token"
AICORD_CHAT_URL = "https://aicordapp.com/api/v1/chat"
AICORD_ACCOUNT_URL = "https://aicordapp.com/api/v1/account"
AICORD_SCOPES = "account:read characters:read characters:use cores:use"

DEFAULT_REDIRECT_URI = "http://localhost:8765/aicord/callback"
MAX_HISTORY_TURNS = 20
OAUTH_STATE_TTL_SECONDS = 600.0
DISCORD_MESSAGE_MAX_LENGTH = 2000

AICORD_CHARACTERS: dict[str, tuple[str, str]] = {
    "dokachan": ("df932593-d15d-4d2c-8dc8-9f455fcebb03", "AIどかちゃん"),
    "takuya": ("e8df0cf2-8387-4d22-8f9d-b311afd49156", "AI拓也"),
}


@dataclass(slots=True)
class _PendingOAuthState:
    user_id: int
    code_verifier: str
    redirect_uri: str
    created_at: float


def _generate_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _split_discord_message(text: str, limit: int = DISCORD_MESSAGE_MAX_LENGTH) -> list[str]:
    if not text:
        return ["（応答が空だったゾ）"]
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    return chunks


class AICordCog(commands.GroupCog, group_name="aicord", group_description="AICord キャラクター対話の設定・ログイン"):
    """AICord の OAuth 認証とキャラクター対話を提供する Cog。"""

    def __init__(self, bot: YajuBot) -> None:
        self.bot = bot
        self._http_session: aiohttp.ClientSession | None = None
        self._oauth_runner: web.AppRunner | None = None
        self._pending_states: dict[str, _PendingOAuthState] = {}
        # (guild_id, channel_id, user_id) -> deque[{"role": ..., "content": ...}]
        self._histories: dict[tuple[int, int, int], deque[dict[str, str]]] = {}
        # user_id -> {"requests": int, "cores_used": float, "total_tokens": int}
        self._usage_stats: dict[int, dict[str, float | int]] = {}

    async def cog_load(self) -> None:
        self._http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60)
        )
        await self._start_oauth_callback_server()

    async def cog_unload(self) -> None:
        if self._oauth_runner is not None:
            await self._oauth_runner.cleanup()
            self._oauth_runner = None
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None

    def _get_redirect_uri(self) -> str:
        return self.bot.config.aicord_redirect_uri or DEFAULT_REDIRECT_URI

    async def _start_oauth_callback_server(self) -> None:
        if not self.bot.config.aicord_client_id:
            return
        parsed = urlparse(self._get_redirect_uri())
        host = parsed.hostname or "0.0.0.0"
        port = parsed.port or 8765
        path = parsed.path or "/aicord/callback"

        app = web.Application()
        app.router.add_get(path, self._handle_oauth_callback)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            site = web.TCPSite(runner, host=host, port=port)
            await site.start()
            self._oauth_runner = runner
            logger.info("AICord OAuth コールバックサーバーを起動しました: %s:%s%s", host, port, path)
        except OSError as exc:
            logger.warning("AICord OAuth コールバックサーバーの起動に失敗しました: %s", exc)
            await runner.cleanup()

    async def _handle_oauth_callback(self, request: web.Request) -> web.Response:
        error = request.query.get("error")
        if error:
            return web.Response(
                text=f"AICord 認証がキャンセルまたは失敗しました: {error}",
                status=400,
            )

        code = request.query.get("code")
        state = request.query.get("state")
        if not code or not state:
            return web.Response(text="パラメータが不足しています。", status=400)

        pending = self._pending_states.pop(state, None)
        if pending is None or (time.monotonic() - pending.created_at) > OAUTH_STATE_TTL_SECONDS:
            return web.Response(
                text="認証セッションの有効期限が切れています。Discord で /aicord login をやり直してください。",
                status=400,
            )

        client_id = self.bot.config.aicord_client_id
        if not client_id or self._http_session is None:
            return web.Response(text="Bot の OAuth 設定が未完了です。", status=500)

        payload: dict[str, str] = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": pending.redirect_uri,
            "code_verifier": pending.code_verifier,
        }
        if self.bot.config.aicord_client_secret:
            payload["client_secret"] = self.bot.config.aicord_client_secret

        try:
            async with self._http_session.post(AICORD_TOKEN_URL, data=payload) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    logger.warning("AICord トークン交換失敗 (%s): %s", resp.status, data)
                    return web.Response(
                        text=f"トークン取得に失敗しました (HTTP {resp.status})。",
                        status=400,
                    )
        except Exception as exc:
            logger.warning("AICord トークン交換中にエラー: %s", exc)
            return web.Response(text="トークン取得中に通信エラーが発生しました。", status=502)

        access_token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(access_token, str) or not access_token:
            return web.Response(text="アクセストークンが応答に含まれていませんでした。", status=502)

        self.bot.storage.set_aicord_user_token(pending.user_id, access_token)
        return web.Response(
            text="AICord の認証が完了しました！このタブを閉じて Discord に戻ってください。",
            content_type="text/plain; charset=utf-8",
        )

    @app_commands.command(name="login", description="自分の AICord アカウントと連携するゾ（クレジットは本人消費）")
    async def login_cmd(self, interaction: discord.Interaction) -> None:
        client_id = self.bot.config.aicord_client_id
        if not client_id:
            await interaction.response.send_message(
                "Bot 側に `AICORD_CLIENT_ID` が設定されていないゾ。管理者に連絡してくれよな～",
                ephemeral=True,
            )
            return

        now = time.monotonic()
        stale = [
            k for k, v in self._pending_states.items() if now - v.created_at > OAUTH_STATE_TTL_SECONDS
        ]
        for k in stale:
            del self._pending_states[k]

        verifier, challenge = _generate_pkce_pair()
        state = secrets.token_urlsafe(24)
        redirect_uri = self._get_redirect_uri()
        self._pending_states[state] = _PendingOAuthState(
            user_id=interaction.user.id,
            code_verifier=verifier,
            redirect_uri=redirect_uri,
            created_at=now,
        )

        params = urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": state,
                "scope": AICORD_SCOPES,
            }
        )
        auth_url = f"{AICORD_AUTHORIZE_URL}?{params}"
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="AICord でログイン", url=auth_url))
        await interaction.response.send_message(
            "下のボタンから AICord にログインしてくれよな～（有効期限 10 分）",
            view=view,
            ephemeral=True,
        )

    @app_commands.command(name="logout", description="保存されている AICord アカウント連携を解除するゾ")
    async def logout_cmd(self, interaction: discord.Interaction) -> None:
        deleted = self.bot.storage.delete_aicord_user_token(interaction.user.id)
        if deleted:
            await interaction.response.send_message("AICord の連携を解除したゾ。", ephemeral=True)
        else:
            await interaction.response.send_message("まだ AICord と連携されていないゾ。", ephemeral=True)

    @app_commands.command(name="set", description="指定チャンネルで対話する AICord キャラクターを設定するゾ")
    @app_commands.describe(
        channel="対話を有効にするテキストチャンネル",
        character="使用するキャラクター",
    )
    @app_commands.choices(
        character=[
            app_commands.Choice(name="AIどかちゃん", value="dokachan"),
            app_commands.Choice(name="AI拓也", value="takuya"),
        ]
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def set_channel_cmd(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        character: app_commands.Choice[str],
    ) -> None:
        if interaction.guild is None:
            return
        char_id, char_name = AICORD_CHARACTERS[character.value]
        self.bot.storage.set_aicord_channel(
            interaction.guild.id, channel.id, char_id, char_name
        )
        await interaction.response.send_message(
            f"{channel.mention} の対話キャラクターを **{char_name}** に設定したゾ。",
            ephemeral=True,
        )

    @app_commands.command(name="remove", description="指定チャンネルの AICord 対話設定を解除するゾ")
    @app_commands.describe(channel="設定を解除するテキストチャンネル")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def remove_channel_cmd(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        if interaction.guild is None:
            return
        removed = self.bot.storage.remove_aicord_channel(interaction.guild.id, channel.id)
        if removed:
            await interaction.response.send_message(
                f"{channel.mention} の AICord 対話設定を解除したゾ。", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"{channel.mention} には対話設定が登録されていないゾ。", ephemeral=True
            )

    @app_commands.command(name="status", description="このサーバーの AICord 対話設定一覧を表示するゾ")
    @app_commands.guild_only()
    async def status_cmd(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        rows = self.bot.storage.list_aicord_channels(interaction.guild.id)
        if not rows:
            await interaction.response.send_message(
                "このサーバーには AICord 対話チャンネルが設定されていないゾ。", ephemeral=True
            )
            return
        lines = [f"<#{ch_id}> → **{char_name}** (`{char_id}`)" for ch_id, char_id, char_name in rows]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="usage", description="自分の AICord クレジット残高・使用状況を確認するゾ")
    async def usage_cmd(self, interaction: discord.Interaction) -> None:
        token = self.bot.storage.get_aicord_user_token(interaction.user.id)
        if not token:
            await interaction.response.send_message(
                "AICord と未連携だゾ！先に `/aicord login` でログインしてくれよな～",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        account_lines: list[str] = []
        if self._http_session is not None:
            try:
                async with self._http_session.get(
                    AICORD_ACCOUNT_URL,
                    headers={"Authorization": f"Bearer {token}"},
                ) as resp:
                    if resp.status == 200:
                        acc: dict[str, Any] = await resp.json()
                        username = acc.get("username") or acc.get("name")
                        if username:
                            account_lines.append(f"アカウント: **{username}**")
                        for key in ("cores", "credits", "balance", "remaining_cores"):
                            if key in acc and acc[key] is not None:
                                account_lines.append(f"残高 ({key}): **{acc[key]}**")
                    elif resp.status == 401:
                        await interaction.followup.send(
                            "AICord の認証が切れているゾ。`/aicord login` で再ログインしてくれよな～",
                            ephemeral=True,
                        )
                        return
            except Exception as exc:
                logger.debug("AICord アカウント情報取得をスキップしました: %s", exc)

        stats = self._usage_stats.get(interaction.user.id)
        if stats:
            account_lines.append(
                f"この Bot 経由の利用: **{int(stats['requests'])} 回** / "
                f"消費 Cores 累計: **{stats['cores_used']:.4g}** / "
                f"トークン累計: **{int(stats['total_tokens']):,}**"
            )
        else:
            account_lines.append("この Bot 経由の利用履歴はまだないゾ。")

        await interaction.followup.send("\n".join(account_lines), ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if not isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            return

        config = self.bot.storage.get_aicord_channel(message.guild.id, message.channel.id)
        if config is None:
            return
        character_id, _character_name = config

        content = message.content.strip()
        if not content:
            return

        history_key = (message.guild.id, message.channel.id, message.author.id)
        if content.lower() == "new":
            self._histories.pop(history_key, None)
            await message.reply(
                f"{message.author.mention} の会話履歴が消されたゾ",
                mention_author=False,
            )
            return

        token = self.bot.storage.get_aicord_user_token(message.author.id)
        if not token:
            await message.reply(
                "AICord と未連携だゾ！先に `/aicord login` でログインしてくれよな～",
                mention_author=False,
            )
            return

        if self._http_session is None:
            return

        history = self._histories.setdefault(
            history_key, deque(maxlen=MAX_HISTORY_TURNS * 2)
        )
        payload_messages: list[dict[str, str]] = list(history)

        # 返信がある場合は返信先の1メッセージのみを一時コンテキストとして読み込む（永続化しない）
        if message.reference and message.reference.message_id:
            ref_msg = message.reference.resolved
            if not isinstance(ref_msg, discord.Message):
                try:
                    ref_msg = await message.channel.fetch_message(message.reference.message_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    ref_msg = None
            if ref_msg is not None and ref_msg.content.strip():
                ref_author = ref_msg.author.display_name
                payload_messages.append(
                    {
                        "role": "user",
                        "content": f"[返信先 {ref_author} の発言]: {ref_msg.content.strip()}",
                    }
                )

        payload_messages.append({"role": "user", "content": content})

        async with message.channel.typing():
            try:
                async with self._http_session.post(
                    AICORD_CHAT_URL,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json={"character": character_id, "messages": payload_messages},
                ) as resp:
                    if resp.status == 401:
                        await message.reply(
                            "AICord の認証が切れているゾ。`/aicord login` で再ログインしてくれよな～",
                            mention_author=False,
                        )
                        return
                    if resp.status == 402:
                        await message.reply(
                            "AICord のクレジット（Cores）が不足しているゾ！",
                            mention_author=False,
                        )
                        return
                    if resp.status >= 400:
                        logger.warning("AICord Chat API エラー (%s)", resp.status)
                        await message.reply(
                            f"AICord API エラーが発生したゾ (HTTP {resp.status})。",
                            mention_author=False,
                        )
                        return
                    data: dict[str, Any] = await resp.json()
            except Exception as exc:
                logger.warning("AICord Chat API 通信失敗: %s", exc)
                await message.reply(
                    "AICord との通信に失敗したゾ。少し待ってから試してくれよな～",
                    mention_author=False,
                )
                return

        reply_text = str(data.get("text") or "").strip()
        if not reply_text:
            reply_text = "（応答が空だったゾ）"

        usage = data.get("usage")
        stats = self._usage_stats.setdefault(
            message.author.id, {"requests": 0, "cores_used": 0.0, "total_tokens": 0}
        )
        stats["requests"] = int(stats["requests"]) + 1
        if isinstance(usage, dict):
            cores = usage.get("cores") or usage.get("cost") or 0
            tokens = usage.get("total_tokens") or (
                int(usage.get("prompt_tokens", 0) or 0)
                + int(usage.get("completion_tokens", 0) or 0)
            )
            try:
                stats["cores_used"] = float(stats["cores_used"]) + float(cores)
            except (TypeError, ValueError):
                pass
            try:
                stats["total_tokens"] = int(stats["total_tokens"]) + int(tokens)
            except (TypeError, ValueError):
                pass

        # 通常会話のみ永続履歴に保存（一時的な返信先コンテキストは保存しない）
        history.append({"role": "user", "content": content})
        history.append({"role": "assistant", "content": reply_text})

        for chunk in _split_discord_message(reply_text):
            await message.reply(chunk, mention_author=False)


async def setup(bot: YajuBot) -> None:
    await bot.add_cog(AICordCog(bot))
