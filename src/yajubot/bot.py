"""Bot 本体・拡張機能（Cog/Event）の自動読み込み・スラッシュコマンド同期を管理するモジュール。"""

from __future__ import annotations

import hashlib
import json
import logging
import pkgutil
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from yajubot.config import BotConfig
from yajubot.storage import ProxyStorage

logger = logging.getLogger(__name__)


def discover_extensions(package_name: str, package_dir: Path) -> list[str]:
    """指定パッケージ配下から自動ロード対象の拡張モジュール名一覧を返す。

    先頭が `_` で始まるモジュール（`__init__.py` や `_helper.py` 等）は除外し、
    読み込み順を安定させるためソートして返す。
    """
    if not package_dir.is_dir():
        return []

    modules = [
        f"{package_name}.{module_info.name}"
        for module_info in pkgutil.iter_modules([str(package_dir)])
        if not module_info.name.startswith("_")
    ]
    return sorted(modules)


async def _send_ephemeral_error(
    interaction: discord.Interaction, message: str
) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        logger.debug("エラー応答の送信に失敗しました（応答期限切れ等）。")


class YajuCommandTree(app_commands.CommandTree):
    """全スラッシュコマンド共通のエラーハンドリングを備えた CommandTree。"""

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
        /,
    ) -> None:
        command = interaction.command
        if command is not None and command._has_any_error_handlers():
            return

        command_name = command.qualified_name if command is not None else "unknown"
        original = (
            error.original
            if isinstance(error, app_commands.CommandInvokeError)
            else error
        )

        if isinstance(original, app_commands.CommandOnCooldown):
            message = (
                f"冷えてるか～？（レート制限） "
                f"あと {original.retry_after:.1f} 秒待ってから再試行してくれよな～頼むよ～"
            )
        elif isinstance(original, discord.RateLimited):
            message = (
                f"冷えてるか～？（レート制限） "
                f"あと {original.retry_after:.1f} 秒待ってからやり直してくれよな～頼むよ～"
            )
        elif isinstance(original, discord.HTTPException) and getattr(original, "status", None) == 429:
            message = "冷えてるか～？（レート制限） ちょっと時間をおいてからやり直してくれよな～頼むよ～"
        elif isinstance(original, app_commands.MissingPermissions):
            message = "実行権限がありませんねぇスギィ！"
        elif isinstance(original, app_commands.BotMissingPermissions):
            message = "これマジ？上半身に比べてボット権限が貧弱すぎるだろ…"
        elif isinstance(original, app_commands.CheckFailure):
            message = "このコマンドを実行する条件確認できず"
        else:
            logger.error(
                "スラッシュコマンド '/%s' の実行中にエラーが発生しました。",
                command_name,
                exc_info=original,
            )
            message = "コマンドの実行中にエラーが発生しました。時間をおいて再度お試しください。"

        await _send_ephemeral_error(interaction, message)


class YajuBot(commands.Bot):
    """yajubot のメイン Bot クラス。"""

    def __init__(
        self,
        config: BotConfig,
        *,
        storage: ProxyStorage | None = None,
    ) -> None:
        intents = discord.Intents.default()
        # Message Content Intent を有効化（Developer Portalで有効化されている場合、メッセージ本文が取得可能）
        intents.message_content = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            tree_cls=YajuCommandTree,
            help_command=None,
            # 1GB RAM / 9000人規模環境向けにメモリ消費を抑制
            max_messages=200,
            chunk_guilds_at_startup=False,
        )
        self.config = config
        self.started_at: datetime = datetime.now(timezone.utc)
        self.storage: ProxyStorage = storage or ProxyStorage()
        self.debug_next_omikuji_tier: str | None = None
        self.debug_next_slot_outcome: str | None = None
        self.debug_next_shinkei_result: str | None = None
        self.debug_restart_requested: bool = False
        self.active_games: dict[int, str] = {}
        self.active_game_interrupts: dict[
            int, Callable[[], Awaitable[discord.Message | None]]
        ] = {}

    def try_acquire_game(self, user_id: int, game_name: str) -> str | None:
        """ユーザーが別ゲームを進行中でなければ登録して None を返す。進行中ならそのゲーム名を返す。"""
        if user_id <= 0:
            return None
        existing = self.active_games.get(user_id)
        if existing is not None:
            return existing
        self.active_games[user_id] = game_name
        return None

    def release_game(self, user_id: int) -> None:
        """ユーザーのアクティブゲーム登録を解除する。"""
        if user_id > 0:
            self.active_games.pop(user_id, None)
            self.active_game_interrupts.pop(user_id, None)

    def register_game_interrupt(
        self,
        user_id: int,
        callback: Callable[[], Awaitable[discord.Message | None]],
    ) -> None:
        """ユーザーの現在のゲームから退出する処理を登録する。"""
        if user_id > 0 and user_id in self.active_games:
            self.active_game_interrupts[user_id] = callback

    async def interrupt_game(
        self, user_id: int
    ) -> tuple[bool, discord.Message | None]:
        """登録済みの退出処理を実行し、中断成否と元メッセージを返す。"""
        callback = self.active_game_interrupts.pop(user_id, None)
        if callback is None:
            return False, None
        try:
            message = await callback()
        finally:
            self.release_game(user_id)
        return True, message

    def release_games(self, user_ids: list[int] | tuple[int, ...]) -> None:
        """複数ユーザーのアクティブゲーム登録をまとめて解除する。"""
        for uid in user_ids:
            self.release_game(uid)

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            try:
                self.storage.close()
            except Exception:
                logger.debug("ストレージのクローズ中にエラーが発生しました。", exc_info=True)

    async def load_extensions_from(
        self, package_name: str, package_dir: Path
    ) -> list[str]:
        """指定ディレクトリ内の拡張モジュールをすべてロードする。"""
        extensions = discover_extensions(package_name, package_dir)
        for ext in extensions:
            try:
                await self.load_extension(ext)
                logger.info("拡張機能を読み込みました: %s", ext)
            except Exception:
                logger.exception("拡張機能の読み込みに失敗しました: %s", ext)
                raise
        return extensions

    def _compute_command_tree_hash(
        self, *, guild: discord.abc.Snowflake | None = None
    ) -> str:
        """現在のコマンドツリー定義のハッシュ値を算出する。"""
        cmds = self.tree.get_commands(guild=guild)
        payload = {
            "target": str(guild.id) if guild is not None else "global",
            "commands": sorted(
                [cmd.to_dict(self.tree) for cmd in cmds],
                key=lambda item: (str(item.get("type", "")), str(item.get("name", ""))),
            ),
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    async def sync_commands(
        self, *, force: bool = False
    ) -> list[app_commands.AppCommand]:
        """設定に応じてギルド限定またはグローバルでスラッシュコマンドを同期する。

        コマンド定義のハッシュが前回同期時と一致する場合は API 呼び出しをスキップする。
        """
        try:
            guild: discord.Object | None = None
            if self.config.guild_id is not None:
                guild = discord.Object(id=self.config.guild_id)
                self.tree.copy_global_to(guild=guild)

            current_hash = self._compute_command_tree_hash(guild=guild)
            if not force and self.storage.get_meta("command_sync_hash") == current_hash:
                logger.info(
                    "スラッシュコマンド定義に変更がないため同期をスキップしました。"
                )
                return []

            if guild is not None:
                synced = await self.tree.sync(guild=guild)
                logger.info(
                    "サーバー (ID: %s) へ %d 件のスラッシュコマンドを同期しました。",
                    self.config.guild_id,
                    len(synced),
                )
            else:
                synced = await self.tree.sync()
                logger.info(
                    "グローバルへ %d 件のスラッシュコマンドを同期しました。",
                    len(synced),
                )
            self.storage.set_meta("command_sync_hash", current_hash)
            return synced
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.error(
                "スラッシュコマンドの同期に失敗しました: %s"
                "（DISCORD_GUILD_ID の値、および Bot が対象サーバーに"
                " applications.commands スコープ付きで招待されているか確認してください）",
                exc,
            )
            return []

    async def setup_hook(self) -> None:
        """起動時初期化フック: イベント・コマンドの読み込みとコマンド同期を行う。"""
        base_dir = Path(__file__).resolve().parent
        await self.load_extensions_from("yajubot.events", base_dir / "events")
        await self.load_extensions_from("yajubot.cogs", base_dir / "cogs")
        await self.sync_commands()
