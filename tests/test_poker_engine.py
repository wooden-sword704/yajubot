import random
import unittest

from yajubot.poker_engine import Card, PokerTable, evaluate_5cards, evaluate_best_hand


def _cards(spec: str) -> list[Card]:
    suit_map = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}
    rank_map = {"J": 11, "Q": 12, "K": 13, "A": 14}
    result: list[Card] = []
    for token in spec.split():
        suit = suit_map[token[0]]
        raw_rank = token[1:]
        rank = rank_map[raw_rank] if raw_rank in rank_map else int(raw_rank)
        result.append(Card(suit=suit, rank=rank))
    return result


class PokerEngineTests(unittest.TestCase):
    def test_hand_rankings_order(self) -> None:
        royal = evaluate_5cards(_cards("S10 SJ SQ SK SA"))
        s_flush = evaluate_5cards(_cards("H5 H6 H7 H8 H9"))
        four_kind = evaluate_5cards(_cards("SA HA DA CA S2"))
        full_house = evaluate_5cards(_cards("SK HK DK S3 H3"))
        flush = evaluate_5cards(_cards("D2 D5 D7 D9 DJ"))
        straight = evaluate_5cards(_cards("S4 H5 D6 C7 S8"))
        wheel = evaluate_5cards(_cards("SA H2 D3 C4 S5"))
        three_kind = evaluate_5cards(_cards("S7 H7 D7 C2 S9"))
        two_pair = evaluate_5cards(_cards("SK HK D4 C4 S9"))
        one_pair = evaluate_5cards(_cards("SQ HQ D2 C5 S9"))
        high_card = evaluate_5cards(_cards("SA H9 D7 C4 S2"))

        self.assertEqual(royal.name_ja, "ロイヤルストレートフラッシュ")
        self.assertEqual(wheel.tiebreakers, (5,))
        ordered = [
            high_card,
            one_pair,
            two_pair,
            three_kind,
            wheel,
            straight,
            flush,
            full_house,
            four_kind,
            s_flush,
            royal,
        ]
        for i in range(len(ordered) - 1):
            self.assertLess(ordered[i], ordered[i + 1])

    def test_evaluate_best_hand_from_7_cards(self) -> None:
        cards = _cards("SA HA D2 C7 DK HK SK")
        best = evaluate_best_hand(cards)
        self.assertEqual(best.category, 6)  # フルハウス (K-K-K-A-A)
        self.assertEqual(best.tiebreakers, (13, 14))

    def test_fold_win_flow(self) -> None:
        table = PokerTable([1, 2], starting_chips=500, small_blind=10, rng=random.Random(42))
        table.start_hand()
        self.assertEqual(table.phase, "PREFLOP")
        # ヘッズアップではディーラー(1)が SB で先手
        cp = table.current_player
        assert cp is not None
        table.apply_action(cp.user_id, "fold")
        self.assertEqual(table.phase, "SHOWDOWN")
        assert table.last_result is not None
        self.assertTrue(table.last_result.won_by_fold)
        winner_id = next(iter(table.last_result.total_payouts))
        self.assertNotEqual(winner_id, cp.user_id)
        self.assertEqual(sum(p.chips for p in table.players), 1000)

    def test_full_hand_to_showdown_preserves_total_chips(self) -> None:
        table = PokerTable([10, 20, 30], starting_chips=1000, small_blind=10, rng=random.Random(7))
        table.start_hand()
        while table.phase != "SHOWDOWN":
            cp = table.current_player
            assert cp is not None
            legal = table.get_legal_actions(cp.user_id)
            assert legal is not None
            action = "check" if legal.can_check else "call"
            table.apply_action(cp.user_id, action)
        self.assertEqual(len(table.community_cards), 5)
        self.assertEqual(sum(p.chips for p in table.players), 3000)

    def test_side_pot_all_in_distribution(self) -> None:
        table = PokerTable([1, 2, 3], starting_chips=100, small_blind=10, rng=random.Random(1))
        table.players[0].chips = 100
        table.players[1].chips = 300
        table.players[2].chips = 300
        table.start_hand()
        # 全員オールインさせる
        while table.phase != "SHOWDOWN":
            cp = table.current_player
            assert cp is not None
            table.apply_action(cp.user_id, "all_in")
        self.assertEqual(sum(p.chips for p in table.players), 700)
        assert table.last_result is not None
        self.assertGreaterEqual(len(table.last_result.pot_payouts), 1)


if __name__ == "__main__":
    unittest.main()
