"""Bot 本体・拡張機能（Cog/Event）の自動読み込み・スラッシュコマンド同期を管理するモジュール。"""

from __future__ import annotations

import logging
import pkgutil
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

        if isinstance(error, app_commands.CommandOnCooldown):
            message = (
                f"冷えてるか～？（レート制限） "
                f"あと {error.retry_after:.1f} 秒待ってから再試行してくれよな～頼むよ～"
            )
        elif isinstance(error, discord.RateLimited):
            message = (
                f"冷えてるか～？（レート制限） "
                f"あと {error.retry_after:.1f} 秒待ってからやり直してくれよな～頼むよ～"
            )
        elif isinstance(error, discord.HTTPException) and getattr(error, "status", None) == 429:
            message = "冷えてるか～？（レート制限） ちょっと時間をおいてからやり直してくれよな～頼むよ～"
        elif isinstance(error, app_commands.MissingPermissions):
            message = "実行権限がありませんねぇスギィ！"
        elif isinstance(error, app_commands.BotMissingPermissions):
            message = "これマジ？上半身に比べてボット権限が貧弱すぎるだろ…"
        elif isinstance(error, app_commands.CheckFailure):
            message = "このコマンドを実行する条件確認できず"
        else:
            logger.error(
                "スラッシュコマンド '/%s' の実行中にエラーが発生しました。",
                command_name,
                exc_info=error,
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

    async def close(self) -> None:
        try:
            self.storage.close()
        except Exception:
            logger.debug("ストレージのクローズ中にエラーが発生しました。", exc_info=True)
        await super().close()

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

    async def sync_commands(self) -> list[app_commands.AppCommand]:
        """設定に応じてギルド限定またはグローバルでスラッシュコマンドを同期する。"""
        try:
            if self.config.guild_id is not None:
                guild = discord.Object(id=self.config.guild_id)
                self.tree.copy_global_to(guild=guild)
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
