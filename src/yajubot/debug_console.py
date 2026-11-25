"""プロセスコンソール上で動作する CUI デバッグコンソール（矢印キー / Enter / Esc 操作）。"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from yajubot.bot import YajuBot
from yajubot.cogs.binjou import BINJOU_MESSAGES
from yajubot.cogs.tadocoin import process_sub_role_billing
from yajubot.omikuji_data import OMIKUJI_TIERS

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MenuItem:
    """CUI メニューの1項目。"""

    label: str
    action: Callable[[], str] | None = None
    submenu_id: str | None = None


def _enable_windows_ansi() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def _read_key(stop_event: threading.Event) -> str | None:
    """1キー入力を読み取り、正規化したキー名を返す。"""
    if sys.platform == "win32":
        import msvcrt

        while not stop_event.is_set():
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    ch2 = msvcrt.getwch()
                    return {
                        "H": "UP",
                        "P": "DOWN",
                        "K": "LEFT",
                        "M": "RIGHT",
                    }.get(ch2, "")
                if ch == "\x03":
                    return "CTRL_C"
                if ch in ("\r", "\n"):
                    return "ENTER"
                if ch == "\x1b":
                    return "ESC"
                if ch in ("k", "w"):
                    return "UP"
                if ch in ("j", "s"):
                    return "DOWN"
                return ""
            time.sleep(0.05)
        return None

    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while not stop_event.is_set():
            r, _, _ = select.select([sys.stdin], [], [], 0.1)
            if not r:
                continue
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                r2, _, _ = select.select([sys.stdin], [], [], 0.02)
                if not r2:
                    return "ESC"
                ch2 = sys.stdin.read(1)
                if ch2 == "[":
                    ch3 = sys.stdin.read(1)
                    return {
                        "A": "UP",
                        "B": "DOWN",
                        "C": "RIGHT",
                        "D": "LEFT",
                    }.get(ch3, "")
                return "ESC"
            if ch == "\x03":
                return "CTRL_C"
            if ch in ("\r", "\n"):
                return "ENTER"
            if ch in ("k", "w"):
                return "UP"
            if ch in ("j", "s"):
                return "DOWN"
            return ""
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


class DebugConsoleController:
    """矢印キー・Enter・Esc で操作できる CUI デバッグコンソールの状態管理。"""

    def __init__(self, bot: YajuBot) -> None:
        self.bot = bot
        self.menu_stack: list[str] = ["root"]
        self.cursor_stack: list[int] = [0]
        self.last_status: str = "待機中（矢印キーで選択、Enterで実行、Escで戻る）"
        self.should_exit: bool = False

    @property
    def current_menu_id(self) -> str:
        return self.menu_stack[-1]

    @property
    def current_cursor(self) -> int:
        return self.cursor_stack[-1]

    def _run_coro(self, coro, *, timeout: float = 15.0):
        loop = getattr(self.bot, "loop", None)
        if loop is None or not loop.is_running():
            coro.close()
            raise RuntimeError("Bot のイベントループがまだ起動していません。")
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=timeout)

    def _action_force_billing(self) -> str:
        renewed, revoked, skipped = self._run_coro(
            process_sub_role_billing(self.bot, force=True)
        )
        return (
            f"強制引き落とし完了: 更新 {renewed}件 / 残高不足等で剥奪・中止 {revoked}件 / スキップ {skipped}件"
        )

    def _action_due_billing(self) -> str:
        renewed, revoked, skipped = self._run_coro(
            process_sub_role_billing(self.bot, force=False)
        )
        return (
            f"期限到来分チェック完了: 更新 {renewed}件 / 剥奪・中止 {revoked}件 / スキップ {skipped}件"
        )

    def _action_set_omikuji_tier(self, tier_name: str | None) -> str:
        self.bot.debug_next_omikuji_tier = tier_name
        if tier_name is None:
            return "次回おみくじティア指定を解除しました（通常抽選）。"
        return f"次回おみくじティアを「{tier_name}」に指定しました。"

    def _action_set_slot_outcome(self, outcome: str | None, label: str) -> str:
        self.bot.debug_next_slot_outcome = outcome
        if outcome is None:
            return "次回スロット出目指定を解除しました（通常抽選）。"
        return f"次回スロット出目を「{label}」に指定しました。"

    def _action_set_shinkei_result(self, result_key: str | None, label: str) -> str:
        self.bot.debug_next_shinkei_result = result_key
        if result_key is None:
            return "次回神経衰弱の結果すり替えを解除しました（通常結果）。"
        return f"次回神経衰弱の結果を「{label}」に指定しました。"

    def _action_trigger_binjou(self) -> str:
        async def _send() -> str:
            for guild in self.bot.guilds:
                settings = self.bot.storage.get_guild_settings(guild.id)
                if settings.binjou_channel_ids:
                    ch_id = settings.binjou_channel_ids[0]
                    ch = self.bot.get_channel(ch_id)
                    if ch is not None and hasattr(ch, "send"):
                        msg = random.choice(BINJOU_MESSAGES)
                        await ch.send(msg)
                        return f"ギルド「{guild.name}」の <#{ch_id}> に便乗イベントを発火しました。"
            return "便乗有効チャンネルが設定されているギルドが見つかりませんでした。"

        return self._run_coro(_send())

    def _action_sync_commands(self) -> str:
        synced = self._run_coro(self.bot.sync_commands(force=True))
        return f"スラッシュコマンドを {len(synced)} 件強制再同期しました。"

    def _action_show_status(self) -> str:
        subs = self.bot.storage.get_all_subscriptions()
        return (
            f"ギルド数: {len(self.bot.guilds)} / キャッシュユーザー数: {len(self.bot.users)} / "
            f"有効サブスク契約数: {len(subs)}"
        )

    def _safe_close_bot(self) -> None:
        try:
            loop = getattr(self.bot, "loop", None)
            if loop is not None and loop.is_running():
                fut = asyncio.run_coroutine_threadsafe(self.bot.close(), loop)
                try:
                    fut.result(timeout=5.0)
                except Exception:
                    pass
        except AttributeError:
            pass

    def _action_shutdown(self) -> str:
        self.should_exit = True
        self._safe_close_bot()
        return "Bot の終了処理を開始しました..."

    def _action_restart(self) -> str:
        self.bot.debug_restart_requested = True
        self.should_exit = True
        self._safe_close_bot()
        return "Bot の再起動処理を開始しました..."

    def _action_reset_all_data(self) -> str:
        self.bot.storage.reset_all_data()
        self.menu_stack = ["root"]
        self.cursor_stack = [0]
        return "💥 全データを完全に初期化しました（DB・設定・タドコイン・ログ等）。"

    def _action_cancel_reset(self) -> str:
        self.menu_stack = ["root"]
        self.cursor_stack = [0]
        return "全データリセットをキャンセルしました。"

    def current_items(self) -> list[MenuItem]:
        menu_id = self.current_menu_id
        if menu_id == "root":
            return [
                MenuItem(
                    "💳 サブスクロール強制引き落とし実行",
                    action=self._action_force_billing,
                ),
                MenuItem(
                    "🎴 次回おみくじ（ガチャ）ティア指定",
                    submenu_id="omikuji",
                ),
                MenuItem(
                    "🎰 次回スロット出目指定",
                    submenu_id="slot",
                ),
                MenuItem(
                    "⚡ 強制イベント発火・管理操作",
                    submenu_id="events",
                ),
                MenuItem(
                    "💥 全データリセット（危険）",
                    submenu_id="reset_confirm_1",
                ),
                MenuItem(
                    "🔄 コマンド強制同期",
                    action=self._action_sync_commands,
                ),
                MenuItem(
                    "🃏 次回神経衰弱の結果すり替え指定",
                    submenu_id="shinkei",
                ),
                MenuItem(
                    "🔄 Bot を再起動",
                    action=self._action_restart,
                ),
                MenuItem(
                    "🛑 Bot を安全に終了",
                    action=self._action_shutdown,
                ),
            ]

        if menu_id == "omikuji":
            items = [
                MenuItem(
                    f"指定: {t.name}",
                    action=lambda name=t.name: self._action_set_omikuji_tier(name),
                )
                for t in OMIKUJI_TIERS
            ]
            items.append(
                MenuItem(
                    "指定解除（通常抽選に戻す）",
                    action=lambda: self._action_set_omikuji_tier(None),
                )
            )
            return items

        if menu_id == "slot":
            return [
                MenuItem(
                    "指定: ｲｷｽｷﾞｼﾞｬｯｸﾎﾟｯﾄ (特賞3つ揃い)",
                    action=lambda: self._action_set_slot_outcome(
                        "jackpot", "ｲｷｽｷﾞｼﾞｬｯｸﾎﾟｯﾄ"
                    ),
                ),
                MenuItem(
                    "指定: ヤジュヤジュ勝利 (通常3つ揃い)",
                    action=lambda: self._action_set_slot_outcome(
                        "three_kind", "ヤジュヤジュ勝利"
                    ),
                ),
                MenuItem(
                    "指定: 小当たり (2つ揃い)",
                    action=lambda: self._action_set_slot_outcome(
                        "two_kind", "小当たり"
                    ),
                ),
                MenuItem(
                    "指定: ハズレ",
                    action=lambda: self._action_set_slot_outcome("loss", "ハズレ"),
                ),
                MenuItem(
                    "指定解除（通常抽選に戻す）",
                    action=lambda: self._action_set_slot_outcome(None, ""),
                ),
            ]

        if menu_id == "shinkei":
            return [
                MenuItem(
                    "指定: 🌟 完全ノーミス最短クリア（隠し実績）",
                    action=lambda: self._action_set_shinkei_result(
                        "perfect", "完全ノーミス最短クリア"
                    ),
                ),
                MenuItem(
                    "指定: ⚡ 1.5倍（ハヤスギィ！ボーナス）",
                    action=lambda: self._action_set_shinkei_result(
                        "fast", "1.5倍ボーナス"
                    ),
                ),
                MenuItem(
                    "指定: ✨ 1.0倍（規定手数内）",
                    action=lambda: self._action_set_shinkei_result(
                        "normal", "1.0倍"
                    ),
                ),
                MenuItem(
                    "指定: 🐢 0.5倍（ギリギリクリア）",
                    action=lambda: self._action_set_shinkei_result(
                        "slow", "0.5倍"
                    ),
                ),
                MenuItem(
                    "指定: 💀 手数オーバー（獲得なし）",
                    action=lambda: self._action_set_shinkei_result(
                        "over", "手数オーバー"
                    ),
                ),
                MenuItem(
                    "指定解除（通常結果に戻す）",
                    action=lambda: self._action_set_shinkei_result(None, ""),
                ),
            ]

        if menu_id == "events":
            return [
                MenuItem(
                    "🗣️ 便乗メッセージ強制発火",
                    action=self._action_trigger_binjou,
                ),
                MenuItem(
                    "🪙 サブスク定期課金即時チェック（期限到来分）",
                    action=self._action_due_billing,
                ),
                MenuItem(
                    "🔄 スラッシュコマンド再同期",
                    action=self._action_sync_commands,
                ),
                MenuItem(
                    "📊 Bot ステータス表示",
                    action=self._action_show_status,
                ),
            ]

        if menu_id == "reset_confirm_1":
            return [
                MenuItem(
                    "キャンセル（メインメニューへ戻る）",
                    action=self._action_cancel_reset,
                ),
                MenuItem(
                    "⚠️ [確認 1/3] 本当に全データを削除しますか？ -> 次へ進む",
                    submenu_id="reset_confirm_2",
                ),
            ]

        if menu_id == "reset_confirm_2":
            return [
                MenuItem(
                    "キャンセル（メインメニューへ戻る）",
                    action=self._action_cancel_reset,
                ),
                MenuItem(
                    "⚠️ [確認 2/3] 取り消せません！本当に初期化しますか？ -> 最終確認へ",
                    submenu_id="reset_confirm_3",
                ),
            ]

        if menu_id == "reset_confirm_3":
            return [
                MenuItem(
                    "キャンセル（メインメニューへ戻る）",
                    action=self._action_cancel_reset,
                ),
                MenuItem(
                    "💥 [最終確認 3/3] 全データを完全に消去して実行する",
                    action=self._action_reset_all_data,
                ),
            ]

        return []

    def handle_key(self, key: str) -> None:
        items = self.current_items()
        if not items:
            return
        if key == "UP":
            self.cursor_stack[-1] = (self.current_cursor - 1) % len(items)
        elif key == "DOWN":
            self.cursor_stack[-1] = (self.current_cursor + 1) % len(items)
        elif key == "ESC":
            if len(self.menu_stack) > 1:
                self.menu_stack.pop()
                self.cursor_stack.pop()
                self.last_status = "前のメニューに戻りました。"
        elif key == "ENTER":
            item = items[self.current_cursor]
            if item.submenu_id is not None:
                self.menu_stack.append(item.submenu_id)
                self.cursor_stack.append(0)
            elif item.action is not None:
                try:
                    self.last_status = item.action()
                except Exception as exc:
                    self.last_status = f"エラー: {exc}"

    def render(self) -> str:
        menu_titles = {
            "root": "メインメニュー",
            "omikuji": "次回おみくじ（ガチャ）ティア指定",
            "slot": "次回スロット出目指定",
            "shinkei": "次回神経衰弱の結果すり替え指定",
            "events": "強制イベント発火・管理操作",
            "reset_confirm_1": "⚠️ 全データリセット 確認 1/3",
            "reset_confirm_2": "⚠️ 全データリセット 確認 2/3",
            "reset_confirm_3": "💥 全データリセット 最終確認 3/3",
        }
        lines = [
            "============================================================",
            f" yajubot デバッグコンソール [{menu_titles.get(self.current_menu_id, '')}]",
            " 操作: ↑/↓ 移動 | Enter 決定 | Esc 戻る",
            "------------------------------------------------------------",
            f" 次回おみくじ強制: {self.bot.debug_next_omikuji_tier or 'なし（通常）'}",
            f" 次回スロット強制: {self.bot.debug_next_slot_outcome or 'なし（通常）'}",
            f" 次回神経衰弱強制: {self.bot.debug_next_shinkei_result or 'なし（通常）'}",
            "------------------------------------------------------------",
        ]
        for idx, item in enumerate(self.current_items()):
            prefix = " ▶ " if idx == self.current_cursor else "   "
            lines.append(f"{prefix}{item.label}")
        lines.extend(
            [
                "------------------------------------------------------------",
                f" 状態: {self.last_status}",
                "============================================================",
            ]
        )
        return "\n".join(lines)


def start_debug_console_thread(bot: YajuBot) -> tuple[threading.Thread, threading.Event] | None:
    """ENABLE_DEBUG_CONSOLE=1 かつ対話的コンソール（TTY）が利用可能な場合に CUI デバッグスレッドを起動する。"""
    enabled = os.environ.get("ENABLE_DEBUG_CONSOLE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if not enabled:
        return None
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None

    _enable_windows_ansi()
    stop_event = threading.Event()
    controller = DebugConsoleController(bot)

    def _worker() -> None:
        # Bot の起動ログが少し落ち着いてから初回描画
        time.sleep(1.0)
        while not stop_event.is_set() and not controller.should_exit:
            sys.stdout.write("\x1b[2J\x1b[H" + controller.render() + "\n")
            sys.stdout.flush()
            key = _read_key(stop_event)
            if key is None:
                break
            if key == "CTRL_C":
                controller._action_shutdown()
                break
            if key:
                controller.handle_key(key)

    thread = threading.Thread(
        target=_worker, name="yajubot-debug-console", daemon=True
    )
    thread.start()
    return thread, stop_event
