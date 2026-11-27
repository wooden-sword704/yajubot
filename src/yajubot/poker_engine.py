"""ノーリミット・テキサスホールデムの純粋 Python ゲームエンジン。"""

from __future__ import annotations

import itertools
import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal, Sequence

SUITS: tuple[str, ...] = ("♠", "♥", "♦", "♣")
RANK_LABELS: dict[int, str] = {
    2: "2",
    3: "3",
    4: "4",
    5: "5",
    6: "6",
    7: "7",
    8: "8",
    9: "9",
    10: "10",
    11: "J",
    12: "Q",
    13: "K",
    14: "A",
}

HAND_CATEGORY_NAMES: tuple[str, ...] = (
    "ハイカード（ブタ）",
    "ワンペア",
    "ツーペア",
    "スリーカード",
    "ストレート",
    "フラッシュ",
    "フルハウス",
    "フォーカード",
    "ストレートフラッシュ",
)

PhaseType = Literal["WAITING", "PREFLOP", "FLOP", "TURN", "RIVER", "SHOWDOWN"]
ActionType = Literal["check", "call", "bet", "raise", "all_in", "fold"]


@dataclass(frozen=True, slots=True)
class Card:
    """トランプのカード1枚。"""

    suit: str
    rank: int  # 2..14 (14 = A)

    def format_short(self) -> str:
        return f"{self.suit}{RANK_LABELS[self.rank]}"


def create_shuffled_deck(rng: random.Random | None = None) -> list[Card]:
    """52枚のシャッフル済みデッキを生成する。"""
    deck = [Card(suit=s, rank=r) for s in SUITS for r in range(2, 15)]
    (rng or random).shuffle(deck)
    return deck


@dataclass(frozen=True, slots=True, order=True)
class HandRank:
    """比較可能なポーカー役判定結果。"""

    category: int
    tiebreakers: tuple[int, ...]
    name_ja: str = field(compare=False)


def _check_straight_high(ranks: Sequence[int]) -> int | None:
    """5枚のランクからストレート成立時の最高ランクを返す（A-2-3-4-5 は 5）。"""
    unique = sorted(set(ranks), reverse=True)
    if len(unique) != 5:
        return None
    if unique[0] - unique[4] == 4:
        return unique[0]
    if unique == [14, 5, 4, 3, 2]:
        return 5
    return None


def evaluate_5cards(cards: Sequence[Card]) -> HandRank:
    """ちょうど5枚のカードから役を判定する。"""
    if len(cards) != 5:
        raise ValueError("5枚のカードが必要です")
    ranks = sorted((c.rank for c in cards), reverse=True)
    is_flush = len({c.suit for c in cards}) == 1
    straight_high = _check_straight_high(ranks)

    if is_flush and straight_high is not None:
        name = (
            "ロイヤルストレートフラッシュ"
            if straight_high == 14
            else HAND_CATEGORY_NAMES[8]
        )
        return HandRank(8, (straight_high,), name)

    counts = Counter(ranks)
    # 出現回数降順、同数ならランク降順
    groups = sorted(counts.items(), key=lambda x: (x[1], x[0]), reverse=True)
    freq_pattern = [g[1] for g in groups]
    ordered_ranks = tuple(g[0] for g in groups)

    if freq_pattern == [4, 1]:
        return HandRank(7, ordered_ranks, HAND_CATEGORY_NAMES[7])
    if freq_pattern == [3, 2]:
        return HandRank(6, ordered_ranks, HAND_CATEGORY_NAMES[6])
    if is_flush:
        return HandRank(5, tuple(ranks), HAND_CATEGORY_NAMES[5])
    if straight_high is not None:
        return HandRank(4, (straight_high,), HAND_CATEGORY_NAMES[4])
    if freq_pattern == [3, 1, 1]:
        return HandRank(3, ordered_ranks, HAND_CATEGORY_NAMES[3])
    if freq_pattern == [2, 2, 1]:
        return HandRank(2, ordered_ranks, HAND_CATEGORY_NAMES[2])
    if freq_pattern == [2, 1, 1, 1]:
        return HandRank(1, ordered_ranks, HAND_CATEGORY_NAMES[1])
    return HandRank(0, tuple(ranks), HAND_CATEGORY_NAMES[0])


def evaluate_best_hand(cards: Sequence[Card]) -> HandRank:
    """5〜7枚のカードから最強の5枚役を判定する（5枚未満はプレビュー用簡易判定）。"""
    if len(cards) < 5:
        ranks = sorted((c.rank for c in cards), reverse=True)
        counts = Counter(ranks)
        groups = sorted(counts.items(), key=lambda x: (x[1], x[0]), reverse=True)
        freq = [g[1] for g in groups]
        ordered = tuple(g[0] for g in groups)
        if freq and freq[0] == 4:
            return HandRank(7, ordered, HAND_CATEGORY_NAMES[7])
        if freq and freq[0] == 3:
            return HandRank(3, ordered, HAND_CATEGORY_NAMES[3])
        if len(freq) >= 2 and freq[0] == 2 and freq[1] == 2:
            return HandRank(2, ordered, HAND_CATEGORY_NAMES[2])
        if freq and freq[0] == 2:
            return HandRank(1, ordered, HAND_CATEGORY_NAMES[1])
        return HandRank(0, tuple(ranks), HAND_CATEGORY_NAMES[0])

    best: HandRank | None = None
    for combo in itertools.combinations(cards, 5):
        rank = evaluate_5cards(combo)
        if best is None or rank > best:
            best = rank
    assert best is not None
    return best


@dataclass(slots=True)
class PokerPlayer:
    """テーブル上の1プレイヤー状態。"""

    user_id: int
    chips: int
    hole_cards: list[Card] = field(default_factory=list)
    street_bet: int = 0
    total_bet: int = 0
    folded: bool = False
    all_in: bool = False
    acted_this_street: bool = False

    @property
    def is_in_hand(self) -> bool:
        return not self.folded

    @property
    def can_act(self) -> bool:
        return not self.folded and not self.all_in and self.chips > 0


@dataclass(frozen=True, slots=True)
class LegalActions:
    """手番プレイヤーが現在選択可能なアクション情報。"""

    can_check: bool
    to_call: int
    min_raise_to: int
    max_raise_to: int
    can_bet_or_raise: bool
    can_all_in: bool


@dataclass(frozen=True, slots=True)
class PotPayout:
    """ポットごとの分配結果。"""

    pot_amount: int
    winner_ids: tuple[int, ...]
    payouts: dict[int, int]


@dataclass(frozen=True, slots=True)
class ShowdownResult:
    """1ハンド終了時の精算結果。"""

    pot_payouts: tuple[PotPayout, ...]
    total_payouts: dict[int, int]
    hand_ranks: dict[int, HandRank]
    won_by_fold: bool


class PokerTable:
    """ノーリミット・テキサスホールデムのテーブル進行管理クラス。"""

    def __init__(
        self,
        player_ids: Sequence[int],
        starting_chips: int,
        small_blind: int = 10,
        *,
        rng: random.Random | None = None,
    ) -> None:
        if len(player_ids) < 2:
            raise ValueError("ポーカーは2人以上必要です")
        self.players: list[PokerPlayer] = [
            PokerPlayer(user_id=uid, chips=starting_chips) for uid in player_ids
        ]
        self.small_blind = max(1, small_blind)
        self.big_blind = self.small_blind * 2
        self._rng = rng
        self.dealer_idx: int = -1
        self.phase: PhaseType = "WAITING"
        self.deck: list[Card] = []
        self.community_cards: list[Card] = []
        self.pot: int = 0
        self.current_bet: int = 0
        self.min_raise_increment: int = self.big_blind
        self.current_turn_idx: int | None = None
        self.hand_number: int = 0
        self.last_action_text: str = ""
        self.last_result: ShowdownResult | None = None

    def get_player(self, user_id: int) -> PokerPlayer | None:
        for p in self.players:
            if p.user_id == user_id:
                return p
        return None

    @property
    def current_player(self) -> PokerPlayer | None:
        if self.current_turn_idx is None:
            return None
        return self.players[self.current_turn_idx]

    def active_players(self) -> list[PokerPlayer]:
        return [p for p in self.players if p.chips > 0]

    def start_hand(self) -> None:
        """新しいハンドを開始する（チップ0のプレイヤーは除外済み前提）。"""
        self.players = [p for p in self.players if p.chips > 0]
        if len(self.players) < 2:
            raise ValueError("チップを持つプレイヤーが2人未満のため開始できません")

        self.hand_number += 1
        self.dealer_idx = (self.dealer_idx + 1) % len(self.players)
        self.deck = create_shuffled_deck(self._rng)
        self.community_cards = []
        self.pot = 0
        self.current_bet = 0
        self.min_raise_increment = self.big_blind
        self.last_result = None
        self.phase = "PREFLOP"

        for p in self.players:
            p.hole_cards = [self.deck.pop(), self.deck.pop()]
            p.street_bet = 0
            p.total_bet = 0
            p.folded = False
            p.all_in = False
            p.acted_this_street = False

        n = len(self.players)
        if n == 2:
            # ヘッズアップではディーラーが SB、相手が BB
            sb_idx = self.dealer_idx
            bb_idx = (self.dealer_idx + 1) % n
        else:
            sb_idx = (self.dealer_idx + 1) % n
            bb_idx = (self.dealer_idx + 2) % n

        self._post_blind(self.players[sb_idx], self.small_blind)
        self._post_blind(self.players[bb_idx], self.big_blind)
        self.current_bet = max(p.street_bet for p in self.players)

        # プリフロップ最初の手番は BB の左隣（2人時は SB=ディーラー）
        first_idx = sb_idx if n == 2 else (bb_idx + 1) % n
        self.current_turn_idx = self._find_next_actor(first_idx, include_start=True)
        self.last_action_text = (
            f"ハンド #{self.hand_number} 開始（SB: {self.small_blind} / BB: {self.big_blind}）"
        )

        # 全員オールイン状態なら即ショーダウンまで進める
        if self._should_auto_runout():
            self._runout_to_showdown()

    def _post_blind(self, player: PokerPlayer, amount: int) -> None:
        paid = min(player.chips, amount)
        player.chips -= paid
        player.street_bet += paid
        player.total_bet += paid
        self.pot += paid
        if player.chips == 0:
            player.all_in = True

    def get_legal_actions(self, user_id: int) -> LegalActions | None:
        cp = self.current_player
        if cp is None or cp.user_id != user_id or self.phase in ("WAITING", "SHOWDOWN"):
            return None
        to_call = max(0, self.current_bet - cp.street_bet)
        can_check = to_call == 0
        max_total = cp.street_bet + cp.chips
        # 最小レイズ先（ベット0なら BB、既存ベットがあれば + min_raise_increment）
        min_raise_to = (
            self.big_blind
            if self.current_bet == 0
            else self.current_bet + self.min_raise_increment
        )
        can_bet_or_raise = cp.chips > to_call and max_total >= min_raise_to
        return LegalActions(
            can_check=can_check,
            to_call=min(to_call, cp.chips),
            min_raise_to=min(min_raise_to, max_total),
            max_raise_to=max_total,
            can_bet_or_raise=can_bet_or_raise,
            can_all_in=cp.chips > 0,
        )

    def apply_action(
        self, user_id: int, action: ActionType, raise_to: int = 0
    ) -> str:
        """手番プレイヤーのアクションを適用し、状態を進める。"""
        cp = self.current_player
        if cp is None or cp.user_id != user_id:
            raise ValueError("あなたの手番ではありません")
        if self.phase in ("WAITING", "SHOWDOWN"):
            raise ValueError("現在はアクションできないフェーズです")

        legal = self.get_legal_actions(user_id)
        assert legal is not None

        if action == "fold":
            cp.folded = True
            cp.acted_this_street = True
            desc = "フォールド"
        elif action == "check":
            if not legal.can_check:
                raise ValueError("ベットが入っているためチェックできません")
            cp.acted_this_street = True
            desc = "チェック"
        elif action == "call":
            if legal.can_check:
                cp.acted_this_street = True
                desc = "チェック"
            else:
                paid = legal.to_call
                cp.chips -= paid
                cp.street_bet += paid
                cp.total_bet += paid
                self.pot += paid
                if cp.chips == 0:
                    cp.all_in = True
                    desc = f"コール（オールイン {paid:,}）"
                else:
                    desc = f"コール（{paid:,}）"
                cp.acted_this_street = True
        elif action in ("bet", "raise"):
            if not legal.can_bet_or_raise:
                raise ValueError("レイズできるだけのチップがありません")
            if raise_to < legal.min_raise_to or raise_to > legal.max_raise_to:
                raise ValueError(
                    f"ベット/レイズ額は {legal.min_raise_to:,}〜{legal.max_raise_to:,} の範囲で指定してください"
                )
            diff = raise_to - cp.street_bet
            cp.chips -= diff
            cp.street_bet += diff
            cp.total_bet += diff
            self.pot += diff
            increment = cp.street_bet - self.current_bet
            if increment > self.min_raise_increment:
                self.min_raise_increment = increment
            self.current_bet = cp.street_bet
            for other in self.players:
                if other.user_id != cp.user_id and other.can_act:
                    other.acted_this_street = False
            cp.acted_this_street = True
            if cp.chips == 0:
                cp.all_in = True
                desc = f"オールイン（計 {cp.street_bet:,}）"
            else:
                verb = "ベット" if action == "bet" else "レイズ"
                desc = f"{verb}（→ {cp.street_bet:,}）"
        elif action == "all_in":
            paid = cp.chips
            new_total = cp.street_bet + paid
            cp.chips = 0
            cp.street_bet = new_total
            cp.total_bet += paid
            self.pot += paid
            cp.all_in = True
            cp.acted_this_street = True
            if new_total > self.current_bet:
                increment = new_total - self.current_bet
                if increment >= self.min_raise_increment:
                    self.min_raise_increment = increment
                self.current_bet = new_total
                for other in self.players:
                    if other.user_id != cp.user_id and other.can_act:
                        other.acted_this_street = False
            desc = f"オールイン（+{paid:,}）"
        else:
            raise ValueError(f"不明なアクションです: {action}")

        self.last_action_text = f"<@{cp.user_id}>: {desc}"
        self._advance_state()
        return desc

    def _find_next_actor(
        self, start_idx: int, *, include_start: bool = False
    ) -> int | None:
        n = len(self.players)
        offset_start = 0 if include_start else 1
        for offset in range(offset_start, n + offset_start):
            idx = (start_idx + offset) % n
            if self.players[idx].can_act:
                return idx
        return None

    def _should_auto_runout(self) -> bool:
        in_hand = [p for p in self.players if p.is_in_hand]
        if len(in_hand) <= 1:
            return False
        actors = [p for p in in_hand if p.can_act]
        if len(actors) == 0:
            return True
        if len(actors) == 1 and actors[0].street_bet >= self.current_bet:
            return True
        return False

    def _advance_state(self) -> None:
        in_hand = [p for p in self.players if p.is_in_hand]
        if len(in_hand) <= 1:
            self._finish_by_fold()
            return

        # 現在のストリートが終了したか判定
        actors = [p for p in in_hand if p.can_act]
        street_done = all(
            p.acted_this_street and p.street_bet == self.current_bet for p in actors
        )
        if not street_done:
            assert self.current_turn_idx is not None
            self.current_turn_idx = self._find_next_actor(self.current_turn_idx)
            return

        if self._should_auto_runout():
            self._runout_to_showdown()
            return

        self._next_street()

    def _next_street(self) -> None:
        for p in self.players:
            p.street_bet = 0
            p.acted_this_street = False
        self.current_bet = 0
        self.min_raise_increment = self.big_blind

        if self.phase == "PREFLOP":
            self.phase = "FLOP"
            self.community_cards.extend([self.deck.pop(), self.deck.pop(), self.deck.pop()])
        elif self.phase == "FLOP":
            self.phase = "TURN"
            self.community_cards.append(self.deck.pop())
        elif self.phase == "TURN":
            self.phase = "RIVER"
            self.community_cards.append(self.deck.pop())
        elif self.phase == "RIVER":
            self._resolve_showdown()
            return

        self.current_turn_idx = self._find_next_actor(self.dealer_idx)
        if self.current_turn_idx is None:
            self._runout_to_showdown()

    def _runout_to_showdown(self) -> None:
        while len(self.community_cards) < 5:
            self.community_cards.append(self.deck.pop())
        self._resolve_showdown()

    def _finish_by_fold(self) -> None:
        self.phase = "SHOWDOWN"
        self.current_turn_idx = None
        winner = next(p for p in self.players if p.is_in_hand)
        winner.chips += self.pot
        payout = PotPayout(
            pot_amount=self.pot,
            winner_ids=(winner.user_id,),
            payouts={winner.user_id: self.pot},
        )
        self.last_result = ShowdownResult(
            pot_payouts=(payout,),
            total_payouts={winner.user_id: self.pot},
            hand_ranks={},
            won_by_fold=True,
        )

    def _resolve_showdown(self) -> None:
        self.phase = "SHOWDOWN"
        self.current_turn_idx = None

        hand_ranks: dict[int, HandRank] = {}
        for p in self.players:
            if p.is_in_hand:
                hand_ranks[p.user_id] = evaluate_best_hand(
                    [*p.hole_cards, *self.community_cards]
                )

        # サイドポット計算: 拠出額のユニークな昇順レベルごとにポットを分割
        levels = sorted({p.total_bet for p in self.players if p.total_bet > 0})
        prev_level = 0
        pot_payouts: list[PotPayout] = []
        total_payouts: dict[int, int] = {}

        # ディーラーの左隣から順に並べた座席順（引き分けの端数配分用）
        n = len(self.players)
        seat_order = [
            self.players[(self.dealer_idx + 1 + i) % n].user_id for i in range(n)
        ]

        for level in levels:
            slice_amount = 0
            for p in self.players:
                contrib = max(0, min(p.total_bet, level) - prev_level)
                slice_amount += contrib
            prev_level = level
            if slice_amount <= 0:
                continue

            eligible = [
                p for p in self.players if p.is_in_hand and p.total_bet >= level
            ]
            if not eligible:
                continue

            best_rank = max(hand_ranks[p.user_id] for p in eligible)
            winners = [
                p.user_id for p in eligible if hand_ranks[p.user_id] == best_rank
            ]
            winners_ordered = [uid for uid in seat_order if uid in winners]

            share, remainder = divmod(slice_amount, len(winners_ordered))
            payout_map: dict[int, int] = {}
            for idx, uid in enumerate(winners_ordered):
                amt = share + (1 if idx < remainder else 0)
                payout_map[uid] = amt
                total_payouts[uid] = total_payouts.get(uid, 0) + amt
                player_obj = self.get_player(uid)
                if player_obj is not None:
                    player_obj.chips += amt

            pot_payouts.append(
                PotPayout(
                    pot_amount=slice_amount,
                    winner_ids=tuple(winners_ordered),
                    payouts=payout_map,
                )
            )

        self.last_result = ShowdownResult(
            pot_payouts=tuple(pot_payouts),
            total_payouts=total_payouts,
            hand_ranks=hand_ranks,
            won_by_fold=False,
        )
