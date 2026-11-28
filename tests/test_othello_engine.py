import random
import unittest

from yajubot.othello_engine import (
    BLACK,
    WHITE,
    OthelloGame,
    coord_label,
    parse_coord_label,
    render_board_png,
)


class OthelloEngineTests(unittest.TestCase):
    def test_initial_legal_moves_and_place(self) -> None:
        game = OthelloGame(black_user_id=1, white_user_id=2)
        self.assertEqual(game.count_discs(), (2, 2))
        legal = game.get_legal_moves(BLACK)
        self.assertEqual(
            {coord_label(r, c) for r, c in legal},
            {"D3", "C4", "F5", "E6"},
        )
        r, c = parse_coord_label("D3")
        outcome = game.place_disc(r, c)
        self.assertEqual(outcome.flipped_count, 1)
        self.assertEqual(game.count_discs(), (4, 1))
        self.assertEqual(game.current_turn, WHITE)

    def test_render_board_png_produces_valid_png(self) -> None:
        game = OthelloGame(black_user_id=1, white_user_id=2)
        png = render_board_png(game)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(png), 1000)

    def test_full_cpu_vs_cpu_game_finishes(self) -> None:
        rng = random.Random(123)
        for diff in ("easy", "normal", "hard"):
            game = OthelloGame(black_user_id=1, white_user_id=2)
            steps = 0
            while not game.is_finished:
                steps += 1
                self.assertLessEqual(steps, 64)
                r, c = game.choose_cpu_move(diff, rng)
                game.place_disc(r, c)
            b, w = game.count_discs()
            self.assertTrue(game.is_finished)
            self.assertGreater(b + w, 4)
            self.assertLessEqual(b + w, 64)


if __name__ == "__main__":
    unittest.main()
