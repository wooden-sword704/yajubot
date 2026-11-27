"""タドコイン連携対応マルチプレイ・テキサスホールデムポーカー Cog。"""

from __future__ import annotations

import asyncio
import logging
import random

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from yajubot.bot import YajuBot
from yajubot.poker_engine import PokerTable, evaluate_best_hand

logger = logging.getLogger(__name__)

MAX_POKER_PLAYERS = 6
MIN_POKER_PLAYERS = 2
TURN_TIMEOUT_SECONDS = 60.0
FREE_PRACTICE_CHIPS = 1000
NPC_BOT_ID = -1


def _format_player_name(user_id: int) -> str:
    return "🤖 **練習Bot**" if user_id == NPC_BOT_ID else f"<@{user_id}>"

PHASE_LABELS: dict[str, str] = {
    "WAITING": "待機中",
    "PREFLOP": "プリフロップ（手札のみ）",
    "FLOP": "フロップ（場札3枚）",
    "TURN": "ターン（場札4枚）",
    "RIVER": "リバー（場札5枚）",
    "SHOWDOWN": "ショーダウン（決着）",
}

POKER_HELP_TEXT = (
    "**🃏 テキサスホールデム 超かんたんルール**\n"
    "1. 全員に**自分だけが見えるカード2枚**が配られます（「🃏 手札を見る」で確認）。\n"
    "2. 場の真ん中に**全員共通のカードが最大5枚**開かれます（3枚 → 4枚目 → 5枚目）。\n"
    "3. **自分の2枚 ＋ 場の5枚＝計7枚**から、最強の5枚の組み合わせ（役）を作った人が勝ち！\n\n"
    "**🏆 役の強さ（強い順）**\n"
    "1. **ロイヤルストレートフラッシュ**: 同じマークの `10 J Q K A`\n"
    "2. **ストレートフラッシュ**: 同じマークで数字が5連番\n"
    "3. **フォーカード**: 同じ数字が4枚\n"
    "4. **フルハウス**: 同じ数字3枚 ＋ 別の数字2枚\n"
    "5. **フラッシュ**: 同じマークが5枚\n"
    "6. **ストレート**: 数字が5連番（マーク不問）\n"
    "7. **スリーカード**: 同じ数字が3枚\n"
    "8. **ツーペア**: 同じ数字のペアが2組\n"
    "9. **ワンペア**: 同じ数字のペアが1組\n"
    "10. **ハイカード**: 役なし（一番大きい数字で勝負）\n\n"
    "**🎮 操作ボタン**\n"
    "・**チェック**: 追加コインなしで次の人へ回す\n"
    "・**コール**: 相手のベット額と同額を出して勝負に乗る\n"
    "・**ベット / レイズ**: 賭け金を上乗せする\n"
    "・**オールイン**: 手持ちチップを全部賭ける\n"
    "・**フォールド**: 今回の勝負を降りる（賭けた分は戻らない）"
)


class PokerRaiseModal(discord.ui.Modal, title="ベット / レイズ額の入力"):
    """ベット・レイズ先の合計額を入力するモーダル。"""

    amount_input: discord.ui.TextInput = discord.ui.TextInput(
        label="このラウンドでの合計ベット額",
        placeholder="例: 100",
        required=True,
        max_length=10,
    )

    def __init__(
        self,
        table_view: PokerTableView,
        min_raise_to: int,
        max_raise_to: int,
        is_bet: bool,
    ) -> None:
        super().__init__()
        self.table_view = table_view
        self.min_raise_to = min_raise_to
        self.max_raise_to = max_raise_to
        self.is_bet = is_bet
        self.amount_input.label = (
            f"{'ベット' if is_bet else 'レイズ'}先の合計額（{min_raise_to:,}〜{max_raise_to:,}）"
        )
        self.amount_input.placeholder = str(min_raise_to)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.amount_input.value.strip().replace(",", "")
        try:
            target_amount = int(raw)
        except ValueError:
            await interaction.response.send_message(
                "半角の整数で入力してくれよな〜。", ephemeral=True
            )
            return

        action = "bet" if self.is_bet else "raise"
        await self.table_view.handle_player_action(
            interaction, action, raise_to=target_amount
        )


class PokerTableView(discord.ui.View):
    """進行中のポーカーテーブル View。"""

    def __init__(
        self,
        bot: YajuBot,
        guild_id: int,
        host_id: int,
        table: PokerTable,
        *,
        is_real_tadocoin: bool,
        is_practice: bool = False,
        board_message: discord.Message | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.guild_id = guild_id
        self.host_id = host_id
        self.table = table
        self.is_real_tadocoin = is_real_tadocoin
        self.is_practice = is_practice
        self._board_message: discord.Message | None = board_message
        self.lock = asyncio.Lock()
        self._turn_timer_task: asyncio.Task[None] | None = None
        self.is_closed = False
        self._cashed_out_ids: set[int] = set()

        self._run_npc_turns()
        self._build_controls()
        self._restart_turn_timer()

    def _run_npc_turns(self) -> None:
        """練習モード時、手番が NPC_BOT_ID の間自動でアクションを進める。"""
        if not self.is_practice or self.is_closed:
            return
        for _ in range(32):
            if (
                self.is_closed
                or self.table.phase in ("WAITING", "SHOWDOWN")
                or self.table.current_player is None
                or self.table.current_player.user_id != NPC_BOT_ID
            ):
                break
            legal = self.table.get_legal_actions(NPC_BOT_ID)
            if legal is None:
                break
            if legal.can_check:
                if legal.can_bet_or_raise and random.random() < 0.25:
                    self.table.apply_action(
                        NPC_BOT_ID, "bet", raise_to=legal.min_raise_to
                    )
                else:
                    self.table.apply_action(NPC_BOT_ID, "check")
            else:
                fold_prob = 0.15 if legal.to_call <= self.table.big_blind * 3 else 0.45
                if random.random() < fold_prob:
                    self.table.apply_action(NPC_BOT_ID, "fold")
                else:
                    self.table.apply_action(NPC_BOT_ID, "call")
            self.table.last_action_text = self.table.last_action_text.replace(
                f"<@{NPC_BOT_ID}>", "🤖 練習Bot"
            )

    def stop(self) -> None:
        self._cancel_turn_timer()
        super().stop()

    def _cancel_turn_timer(self) -> None:
        current = asyncio.current_task()
        if (
            self._turn_timer_task is not None
            and not self._turn_timer_task.done()
            and self._turn_timer_task is not current
        ):
            self._turn_timer_task.cancel()
        self._turn_timer_task = None

    def _restart_turn_timer(self) -> None:
        self._cancel_turn_timer()
        if self.is_closed or self.table.phase in ("WAITING", "SHOWDOWN"):
            return
        cp = self.table.current_player
        if cp is None:
            return
        expected_hand = self.table.hand_number
        expected_user_id = cp.user_id
        try:
            loop = asyncio.get_running_loop()
            self._turn_timer_task = loop.create_task(
                self._turn_timeout_worker(expected_hand, expected_user_id)
            )
        except RuntimeError:
            pass

    async def _turn_timeout_worker(
        self, expected_hand: int, expected_user_id: int
    ) -> None:
        try:
            await asyncio.sleep(TURN_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            return

        async with self.lock:
            if self.is_closed or self.table.phase in ("WAITING", "SHOWDOWN"):
                return
            cp = self.table.current_player
            if (
                cp is None
                or self.table.hand_number != expected_hand
                or cp.user_id != expected_user_id
            ):
                return
            legal = self.table.get_legal_actions(expected_user_id)
            if legal is None:
                return
            auto_action = "check" if legal.can_check else "fold"
            try:
                self.table.apply_action(expected_user_id, auto_action)  # type: ignore[arg-type]
                self.table.last_action_text += "（時間切れ自動）"
            except ValueError:
                return

            self._post_action_housekeeping()
            if self._board_message is not None:
                try:
                    await self._board_message.edit(
                        embed=self.build_embed(), view=self
                    )
                except (discord.NotFound, discord.HTTPException, aiohttp.ClientError):
                    pass

    def _refund_player(self, user_id: int, chips: int) -> None:
        if user_id in self._cashed_out_ids:
            return
        self._cashed_out_ids.add(user_id)
        if self.is_real_tadocoin and chips > 0:
            self.bot.storage.add_tadocoin(self.guild_id, user_id, chips)

    def _close_and_cashout_all(self) -> None:
        self.is_closed = True
        for p in self.table.players:
            self._refund_player(p.user_id, p.chips)
        self.stop()

    def _post_action_housekeeping(self) -> None:
        self._run_npc_turns()
        if self.table.phase == "SHOWDOWN":
            # チップが 2 人以上残っていなければ自動精算してテーブル終了
            survivors = [p for p in self.table.players if p.chips > 0]
            if len(survivors) < MIN_POKER_PLAYERS:
                self._close_and_cashout_all()
        self._build_controls()
        self._restart_turn_timer()

    def _build_controls(self) -> None:
        self.clear_items()

        if self.is_closed:
            return

        if self.table.phase == "SHOWDOWN":
            next_btn = discord.ui.Button(
                label="次のハンドへ",
                emoji="▶",
                style=discord.ButtonStyle.success,
                row=0,
            )
            next_btn.callback = self._on_next_hand
            self.add_item(next_btn)

            if not self.is_practice:
                cashout_btn = discord.ui.Button(
                    label="精算して抜ける",
                    emoji="💰",
                    style=discord.ButtonStyle.secondary,
                    row=0,
                )
                cashout_btn.callback = self._on_cashout_leave
                self.add_item(cashout_btn)

            close_btn = discord.ui.Button(
                label="練習を終了する" if self.is_practice else "テーブル解散（ホスト）",
                emoji="🛑",
                style=discord.ButtonStyle.danger,
                row=0,
            )
            close_btn.callback = self._on_host_close
            self.add_item(close_btn)

            help_btn = discord.ui.Button(
                label="ルール・役一覧",
                emoji="📖",
                style=discord.ButtonStyle.secondary,
                row=1,
            )
            help_btn.callback = self._on_help
            self.add_item(help_btn)
            return

        cp = self.table.current_player
        legal = (
            self.table.get_legal_actions(cp.user_id) if cp is not None else None
        )

        # Row 0: 手番アクション
        if legal is not None:
            check_call_label = (
                "チェック" if legal.can_check else f"コール ({legal.to_call:,})"
            )
            check_call_btn = discord.ui.Button(
                label=check_call_label,
                style=discord.ButtonStyle.primary,
                row=0,
            )
            check_call_btn.callback = self._on_check_or_call
            self.add_item(check_call_btn)

            if legal.can_bet_or_raise:
                raise_label = "ベット" if self.table.current_bet == 0 else "レイズ"
                raise_btn = discord.ui.Button(
                    label=raise_label,
                    emoji="📈",
                    style=discord.ButtonStyle.success,
                    row=0,
                )
                raise_btn.callback = self._on_open_raise_modal
                self.add_item(raise_btn)

            if legal.can_all_in:
                all_in_btn = discord.ui.Button(
                    label=f"オールイン ({cp.chips:,})" if cp else "オールイン",
                    emoji="🔥",
                    style=discord.ButtonStyle.danger,
                    row=0,
                )
                all_in_btn.callback = self._on_all_in
                self.add_item(all_in_btn)

            fold_btn = discord.ui.Button(
                label="フォールド",
                emoji="🏳️",
                style=discord.ButtonStyle.secondary,
                row=0,
            )
            fold_btn.callback = self._on_fold
            self.add_item(fold_btn)

        # Row 1: 盤面を変更しない補助ボタン（誰がいつ押しても安全）
        hand_btn = discord.ui.Button(
            label="手札を見る",
            emoji="🃏",
            style=discord.ButtonStyle.primary,
            row=1,
        )
        hand_btn.callback = self._on_view_hand
        self.add_item(hand_btn)

        help_btn = discord.ui.Button(
            label="ルール・役一覧",
            emoji="📖",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        help_btn.callback = self._on_help
        self.add_item(help_btn)

    def build_embed(self) -> discord.Embed:
        phase_label = PHASE_LABELS.get(self.table.phase, self.table.phase)
        unit = "タドコイン" if self.is_real_tadocoin else "練習チップ"

        if self.is_closed:
            title = f"🃏 ポーカー終了（ハンド #{self.table.hand_number}）"
            color = discord.Color.dark_grey()
        elif self.table.phase == "SHOWDOWN":
            title = f"🏆 ショーダウン（ハンド #{self.table.hand_number}）"
            color = discord.Color.gold()
        else:
            title = f"🃏 ポーカー（ハンド #{self.table.hand_number} / {phase_label}）"
            color = discord.Color.green()

        embed = discord.Embed(title=title, color=color)

        board_cards = (
            " ".join(f"`{c.format_short()}`" for c in self.table.community_cards)
            if self.table.community_cards
            else "（まだ開かれていません）"
        )
        embed.add_field(name="場札（コミュニティカード）", value=board_cards, inline=False)
        embed.add_field(
            name="ポット合計", value=f"**{self.table.pot:,}** {unit}", inline=True
        )
        embed.add_field(
            name="現在のベット基準",
            value=f"**{self.table.current_bet:,}** {unit}",
            inline=True,
        )

        cp = self.table.current_player
        if cp is not None and not self.is_closed:
            embed.add_field(
                name="現在の手番（60秒以内）",
                value=_format_player_name(cp.user_id),
                inline=True,
            )

        player_lines: list[str] = []
        for idx, p in enumerate(self.table.players):
            dealer_mark = "🔘" if idx == self.table.dealer_idx else ""
            if p.folded:
                state = "🏳️ フォールド"
            elif p.all_in:
                state = "🔥 オールイン"
            elif cp is not None and p.user_id == cp.user_id:
                state = "👈 **思考中**"
            elif p.chips == 0 and self.table.phase == "SHOWDOWN":
                state = "💀 チップ0"
            else:
                state = "待機"

            bet_info = f" / 場: {p.street_bet:,}" if p.street_bet > 0 else ""
            reveal = ""
            if (
                self.table.phase == "SHOWDOWN"
                and self.table.last_result is not None
                and not self.table.last_result.won_by_fold
                and p.is_in_hand
            ):
                cards_str = " ".join(f"`{c.format_short()}`" for c in p.hole_cards)
                rank = self.table.last_result.hand_ranks.get(p.user_id)
                rank_name = rank.name_ja if rank is not None else ""
                reveal = f" — {cards_str} (**{rank_name}**)"

            player_lines.append(
                f"{dealer_mark}{_format_player_name(p.user_id)}: **{p.chips:,}** {unit}{bet_info} [{state}]{reveal}"
            )

        embed.add_field(
            name="参加プレイヤー",
            value="\n".join(player_lines) if player_lines else "なし",
            inline=False,
        )

        if self.table.phase == "SHOWDOWN" and self.table.last_result is not None:
            payout_lines = [
                f"🏆 {_format_player_name(uid)}: **+{amt:,}** {unit}"
                for uid, amt in self.table.last_result.total_payouts.items()
            ]
            if payout_lines:
                embed.add_field(
                    name="今回の獲得", value="\n".join(payout_lines), inline=False
                )

        if self.table.last_action_text:
            embed.set_footer(text=self.table.last_action_text)
        if self.is_closed:
            embed.description = "全プレイヤーの精算が完了し、テーブルを終了しました。"

        return embed

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        logger.exception("PokerTableView でエラーが発生しました: %s", error)
        msg = "ポーカーの処理中にエラーが発生したゾ。もう一度試してくれよな〜。"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except (discord.NotFound, discord.HTTPException):
            pass

    async def handle_player_action(
        self,
        interaction: discord.Interaction,
        action: str,
        *,
        raise_to: int = 0,
    ) -> None:
        if self._board_message is None and interaction.message is not None:
            self._board_message = interaction.message

        async with self.lock:
            if self.is_closed or self.table.phase in ("WAITING", "SHOWDOWN"):
                await interaction.response.send_message(
                    "このハンドはすでに終了しているゾ。", ephemeral=True
                )
                return

            cp = self.table.current_player
            if cp is None or interaction.user.id != cp.user_id:
                who = f"<@{cp.user_id}>" if cp is not None else "他のプレイヤー"
                await interaction.response.send_message(
                    f"今は {who} の手番だゾ。", ephemeral=True
                )
                return

            try:
                self.table.apply_action(interaction.user.id, action, raise_to=raise_to)  # type: ignore[arg-type]
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return

            self._post_action_housekeeping()
            try:
                await interaction.response.edit_message(
                    embed=self.build_embed(), view=self
                )
            except (discord.NotFound, discord.HTTPException) as exc:
                logger.warning("ポーカー盤面の更新に失敗しました: %s", exc)

    async def _on_check_or_call(self, interaction: discord.Interaction) -> None:
        cp = self.table.current_player
        legal = self.table.get_legal_actions(cp.user_id) if cp else None
        action = "check" if (legal and legal.can_check) else "call"
        await self.handle_player_action(interaction, action)

    async def _on_all_in(self, interaction: discord.Interaction) -> None:
        await self.handle_player_action(interaction, "all_in")

    async def _on_fold(self, interaction: discord.Interaction) -> None:
        await self.handle_player_action(interaction, "fold")

    async def _on_open_raise_modal(self, interaction: discord.Interaction) -> None:
        async with self.lock:
            if self.is_closed or self.table.phase in ("WAITING", "SHOWDOWN"):
                await interaction.response.send_message(
                    "このハンドはすでに終了しているゾ。", ephemeral=True
                )
                return
            cp = self.table.current_player
            if cp is None or interaction.user.id != cp.user_id:
                who = f"<@{cp.user_id}>" if cp is not None else "他のプレイヤー"
                await interaction.response.send_message(
                    f"今は {who} の手番だゾ。", ephemeral=True
                )
                return
            legal = self.table.get_legal_actions(cp.user_id)
            if legal is None or not legal.can_bet_or_raise:
                await interaction.response.send_message(
                    "今はベット / レイズできない状態だゾ。", ephemeral=True
                )
                return
            is_bet = self.table.current_bet == 0
            modal = PokerRaiseModal(
                self, legal.min_raise_to, legal.max_raise_to, is_bet
            )
        await interaction.response.send_modal(modal)

    async def _on_view_hand(self, interaction: discord.Interaction) -> None:
        player = self.table.get_player(interaction.user.id)
        if player is None:
            await interaction.response.send_message(
                "このテーブルに参加していないゾ。", ephemeral=True
            )
            return
        if not player.hole_cards:
            await interaction.response.send_message(
                "まだ手札が配られていないゾ。", ephemeral=True
            )
            return
        cards_str = " ".join(f"`{c.format_short()}`" for c in player.hole_cards)
        current_rank = evaluate_best_hand(
            [*player.hole_cards, *self.table.community_cards]
        )
        status = "（フォールド済み）" if player.folded else ""
        await interaction.response.send_message(
            f"🃏 **あなたの手札** {status}\n{cards_str}\n現在の役: **{current_rank.name_ja}**",
            ephemeral=True,
        )

    async def _on_help(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(POKER_HELP_TEXT, ephemeral=True)

    async def _on_next_hand(self, interaction: discord.Interaction) -> None:
        async with self.lock:
            if self.is_closed or self.table.phase != "SHOWDOWN":
                await interaction.response.send_message(
                    "今は次のハンドを開始できないゾ。", ephemeral=True
                )
                return
            if self.table.get_player(interaction.user.id) is None:
                await interaction.response.send_message(
                    "このテーブルに参加していないゾ。", ephemeral=True
                )
                return
            try:
                self.table.start_hand()
            except ValueError as exc:
                self._close_and_cashout_all()
                await interaction.response.edit_message(
                    embed=self.build_embed(), view=self
                )
                await interaction.followup.send(str(exc), ephemeral=True)
                return

            self._post_action_housekeeping()
            try:
                await interaction.response.edit_message(
                    embed=self.build_embed(), view=self
                )
            except (discord.NotFound, discord.HTTPException) as exc:
                logger.warning("ポーカー盤面の更新に失敗しました: %s", exc)

    async def _on_cashout_leave(self, interaction: discord.Interaction) -> None:
        async with self.lock:
            if self.is_closed or self.table.phase != "SHOWDOWN":
                await interaction.response.send_message(
                    "精算離脱はハンド終了後（ショーダウン後）にできるゾ。",
                    ephemeral=True,
                )
                return
            player = self.table.get_player(interaction.user.id)
            if player is None:
                await interaction.response.send_message(
                    "このテーブルに参加していないゾ。", ephemeral=True
                )
                return

            refund_chips = player.chips
            self._refund_player(player.user_id, refund_chips)
            self.table.players = [
                p for p in self.table.players if p.user_id != interaction.user.id
            ]
            self.table.last_action_text = f"<@{interaction.user.id}> が精算して離脱しました"

            survivors = [p for p in self.table.players if p.chips > 0]
            if len(survivors) < MIN_POKER_PLAYERS:
                self._close_and_cashout_all()
            else:
                self._build_controls()

            await interaction.response.edit_message(
                embed=self.build_embed(), view=self
            )

        unit = "タドコイン" if self.is_real_tadocoin else "練習チップ"
        await interaction.followup.send(
            f"💰 **{refund_chips:,}** {unit} を精算してテーブルから抜けたゾ。",
            ephemeral=True,
        )

    async def _on_host_close(self, interaction: discord.Interaction) -> None:
        async with self.lock:
            if interaction.user.id != self.host_id:
                await interaction.response.send_message(
                    "テーブルを解散できるのはホストだけだゾ。", ephemeral=True
                )
                return
            if self.is_closed:
                return
            self._close_and_cashout_all()
            self._build_controls()
            await interaction.response.edit_message(
                embed=self.build_embed(), view=self
            )


class PokerLobbyView(discord.ui.View):
    """ポーカーの参加者募集ロビー View。"""

    def __init__(
        self,
        bot: YajuBot,
        guild_id: int,
        host_id: int,
        buy_in: int,
        small_blind: int,
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id
        self.host_id = host_id
        self.buy_in = buy_in
        self.small_blind = small_blind
        self.players: list[int] = [host_id]
        self.lock = asyncio.Lock()
        self.is_closed = False

    @property
    def is_real_tadocoin(self) -> bool:
        return self.buy_in > 0

    def build_embed(self) -> discord.Embed:
        if self.is_real_tadocoin:
            fee_text = f"**{self.buy_in:,}** タドコイン"
        else:
            fee_text = f"無料（練習チップ **{FREE_PRACTICE_CHIPS:,}** 枚配布）"

        embed = discord.Embed(
            title="🃏 ポーカー（テキサスホールデム）参加者募集中！",
            description=(
                "「参加する」ボタンを押して席に着こう！\n"
                "ホストが「ゲーム開始」を押すとスタートします（2〜6人）。"
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="参加費（バイイン）", value=fee_text, inline=True)
        embed.add_field(
            name="ブラインド（SB / BB）",
            value=f"{self.small_blind:,} / {self.small_blind * 2:,}",
            inline=True,
        )
        member_lines = [
            f"{i}. <@{uid}>" + ("（ホスト）" if uid == self.host_id else "")
            for i, uid in enumerate(self.players, start=1)
        ]
        embed.add_field(
            name=f"参加者 ({len(self.players)} / {MAX_POKER_PLAYERS}人)",
            value="\n".join(member_lines),
            inline=False,
        )
        return embed

    @discord.ui.button(label="参加する", emoji="✋", style=discord.ButtonStyle.success)
    async def join_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        async with self.lock:
            if self.is_closed:
                await interaction.response.send_message(
                    "このロビーはすでに終了しているゾ。", ephemeral=True
                )
                return
            if interaction.user.id in self.players:
                await interaction.response.send_message(
                    "すでに参加しているゾ。", ephemeral=True
                )
                return
            if len(self.players) >= MAX_POKER_PLAYERS:
                await interaction.response.send_message(
                    f"満席だゾ（最大 {MAX_POKER_PLAYERS} 人まで）。", ephemeral=True
                )
                return
            if self.is_real_tadocoin:
                ok, bal = self.bot.storage.try_spend_tadocoin(
                    self.guild_id, interaction.user.id, self.buy_in
                )
                if not ok:
                    await interaction.response.send_message(
                        f"タドコインが足りないゾ！（必要: **{self.buy_in:,}** / 所持: **{bal:,}**）",
                        ephemeral=True,
                    )
                    return
            self.players.append(interaction.user.id)
            await interaction.response.edit_message(
                embed=self.build_embed(), view=self
            )

    @discord.ui.button(label="抜ける / 解散", emoji="🚪", style=discord.ButtonStyle.secondary)
    async def leave_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        async with self.lock:
            if self.is_closed:
                await interaction.response.send_message(
                    "このロビーはすでに終了しているゾ。", ephemeral=True
                )
                return
            if interaction.user.id not in self.players:
                await interaction.response.send_message(
                    "このロビーに参加していないゾ。", ephemeral=True
                )
                return

            if interaction.user.id == self.host_id:
                self.is_closed = True
                if self.is_real_tadocoin:
                    for uid in self.players:
                        self.bot.storage.add_tadocoin(self.guild_id, uid, self.buy_in)
                self.stop()
                embed = discord.Embed(
                    title="🃏 ポーカー募集解散",
                    description="ホストが解散したため、参加費を全員に返金しました。",
                    color=discord.Color.dark_grey(),
                )
                await interaction.response.edit_message(embed=embed, view=None)
                return

            self.players.remove(interaction.user.id)
            if self.is_real_tadocoin:
                self.bot.storage.add_tadocoin(
                    self.guild_id, interaction.user.id, self.buy_in
                )
            await interaction.response.edit_message(
                embed=self.build_embed(), view=self
            )

    @discord.ui.button(label="ゲーム開始", emoji="▶️", style=discord.ButtonStyle.primary)
    async def start_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        async with self.lock:
            if self.is_closed:
                await interaction.response.send_message(
                    "このロビーはすでに開始・終了しているゾ。", ephemeral=True
                )
                return
            if interaction.user.id != self.host_id:
                await interaction.response.send_message(
                    "ゲームを開始できるのはホストだけだゾ。", ephemeral=True
                )
                return
            if len(self.players) < MIN_POKER_PLAYERS:
                await interaction.response.send_message(
                    f"ポーカーは {MIN_POKER_PLAYERS} 人以上いないと開始できないゾ！",
                    ephemeral=True,
                )
                return

            starting_chips = (
                self.buy_in if self.is_real_tadocoin else FREE_PRACTICE_CHIPS
            )
            table = PokerTable(
                self.players,
                starting_chips=starting_chips,
                small_blind=self.small_blind,
            )
            table.start_hand()
            self.is_closed = True
            self.stop()

            table_view = PokerTableView(
                self.bot,
                self.guild_id,
                self.host_id,
                table,
                is_real_tadocoin=self.is_real_tadocoin,
                board_message=interaction.message,
            )
            await interaction.response.edit_message(
                embed=table_view.build_embed(), view=table_view
            )

    @discord.ui.button(label="ルール・役一覧", emoji="📖", style=discord.ButtonStyle.secondary)
    async def help_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_message(POKER_HELP_TEXT, ephemeral=True)

    async def on_timeout(self) -> None:
        async with self.lock:
            if self.is_closed:
                return
            self.is_closed = True
            if self.is_real_tadocoin:
                for uid in self.players:
                    self.bot.storage.add_tadocoin(self.guild_id, uid, self.buy_in)


class PokerCog(commands.Cog):
    """マルチプレイ・ポーカーコマンドを提供する Cog。"""

    def __init__(self, bot: YajuBot) -> None:
        self.bot = bot

    @app_commands.command(
        name="poker",
        description="タドコインを賭けてマルチプレイ・ポーカー（テキサスホールデム）を開始するゾ",
    )
    @app_commands.describe(
        buy_in="参加費（タドコイン。1以上）",
        small_blind="スモールブラインド額（ビッグブラインドはその2倍）",
    )
    @app_commands.guild_only()
    async def poker_cmd(
        self,
        interaction: discord.Interaction,
        buy_in: app_commands.Range[int, 1, 1_000_000] = 1000,
        small_blind: app_commands.Range[int, 1, 10_000] = 10,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "サーバー内のチャンネルでのみ利用できるゾ。", ephemeral=True
            )
            return

        if buy_in < small_blind * 2:
            await interaction.response.send_message(
                f"参加費（{buy_in:,}）はビッグブラインド（{small_blind * 2:,}）以上にしてくれよな〜。",
                ephemeral=True,
            )
            return

        ok, bal = self.bot.storage.try_spend_tadocoin(
            guild.id, interaction.user.id, buy_in
        )
        if not ok:
            await interaction.response.send_message(
                f"タドコインが足りないゾ！（必要: **{buy_in:,}** / 所持: **{bal:,}**）",
                ephemeral=True,
            )
            return

        lobby = PokerLobbyView(
            self.bot,
            guild.id,
            interaction.user.id,
            buy_in=buy_in,
            small_blind=small_blind,
        )
        await interaction.response.send_message(embed=lobby.build_embed(), view=lobby)

    @app_commands.command(
        name="poker_practice",
        description="練習Bot相手に1人でポーカー（テキサスホールデム）を練習するゾ（無料）",
    )
    @app_commands.guild_only()
    async def poker_practice_cmd(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "サーバー内のチャンネルでのみ利用できるゾ。", ephemeral=True
            )
            return

        table = PokerTable(
            [interaction.user.id, NPC_BOT_ID],
            starting_chips=FREE_PRACTICE_CHIPS,
            small_blind=10,
        )
        table.start_hand()
        table_view = PokerTableView(
            self.bot,
            guild.id,
            interaction.user.id,
            table,
            is_real_tadocoin=False,
            is_practice=True,
        )
        await interaction.response.send_message(
            embed=table_view.build_embed(), view=table_view
        )
        try:
            table_view._board_message = await interaction.original_response()
        except (discord.NotFound, discord.HTTPException):
            pass


async def setup(bot: YajuBot) -> None:
    await bot.add_cog(PokerCog(bot))
