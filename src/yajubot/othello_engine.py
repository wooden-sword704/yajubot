"""8×8 オセロ（リバーシ）のゲームエンジンおよび Pillow 盤面画像レンダラー。"""

from __future__ import annotations

import io
import random
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

EMPTY = 0
BLACK = 1
WHITE = -1

BOARD_SIZE = 8
COL_LABELS = "ABCDEFGH"
DIRECTIONS: tuple[tuple[int, int], ...] = (
    (-1, -1),
    (-1, 0),
    (-1, 1),
    (0, -1),
    (0, 1),
    (1, -1),
    (1, 0),
    (1, 1),
)

# 位置評価テーブル（CPU 思考用）
POSITION_WEIGHTS: tuple[tuple[int, ...], ...] = (
    (100, -20, 10, 5, 5, 10, -20, 100),
    (-20, -40, -5, -5, -5, -5, -40, -20),
    (10, -5, 5, 1, 1, 5, -5, 10),
    (5, -5, 1, 1, 1, 1, -5, 5),
    (5, -5, 1, 1, 1, 1, -5, 5),
    (10, -5, 5, 1, 1, 5, -5, 10),
    (-20, -40, -5, -5, -5, -5, -40, -20),
    (100, -20, 10, 5, 5, 10, -20, 100),
)


def coord_label(row: int, col: int) -> str:
    return f"{COL_LABELS[col]}{row + 1}"


def parse_coord_label(label: str) -> tuple[int, int]:
    s = label.strip().upper()
    if len(s) != 2 or s[0] not in COL_LABELS or not s[1].isdigit():
        raise ValueError(f"不正な座標です: {label}")
    col = COL_LABELS.index(s[0])
    row = int(s[1]) - 1
    if not (0 <= row < BOARD_SIZE):
        raise ValueError(f"不正な行番号です: {label}")
    return row, col


@dataclass(frozen=True, slots=True)
class MoveOutcome:
    """1手打った結果。"""

    row: int
    col: int
    flipped_count: int
    passed_opponent: bool
    is_finished: bool


class OthelloGame:
    """8×8 オセロの盤面・ルール管理クラス。"""

    def __init__(self, black_user_id: int, white_user_id: int) -> None:
        self.black_user_id = black_user_id
        self.white_user_id = white_user_id
        self.board: list[list[int]] = [[EMPTY] * BOARD_SIZE for _ in range(BOARD_SIZE)]
        self.board[3][3] = WHITE
        self.board[3][4] = BLACK
        self.board[4][3] = BLACK
        self.board[4][4] = WHITE
        self.current_turn: int = BLACK
        self.last_move: tuple[int, int] | None = None
        self.move_count: int = 0
        self.is_finished: bool = False
        self.is_aborted: bool = False
        self.aborted_by: int | None = None

    @property
    def current_player_id(self) -> int:
        return self.black_user_id if self.current_turn == BLACK else self.white_user_id

    def color_of(self, user_id: int) -> int | None:
        if user_id == self.black_user_id:
            return BLACK
        if user_id == self.white_user_id:
            return WHITE
        return None

    def count_discs(self) -> tuple[int, int]:
        """(黒の石数, 白の石数) を返す。"""
        black = sum(1 for row in self.board for cell in row if cell == BLACK)
        white = sum(1 for row in self.board for cell in row if cell == WHITE)
        return black, white

    def get_flips(self, row: int, col: int, color: int) -> list[tuple[int, int]]:
        if not (0 <= row < BOARD_SIZE and 0 <= col < BOARD_SIZE):
            return []
        if self.board[row][col] != EMPTY:
            return []
        opponent = -color
        flips: list[tuple[int, int]] = []
        for dr, dc in DIRECTIONS:
            line: list[tuple[int, int]] = []
            r, c = row + dr, col + dc
            while 0 <= r < BOARD_SIZE and 0 <= c < BOARD_SIZE:
                cell = self.board[r][c]
                if cell == opponent:
                    line.append((r, c))
                elif cell == color:
                    if line:
                        flips.extend(line)
                    break
                else:
                    break
                r += dr
                c += dc
        return flips

    def get_legal_moves(
        self, color: int | None = None
    ) -> dict[tuple[int, int], list[tuple[int, int]]]:
        target = self.current_turn if color is None else color
        moves: dict[tuple[int, int], list[tuple[int, int]]] = {}
        for r in range(BOARD_SIZE):
            for c in range(BOARD_SIZE):
                flips = self.get_flips(r, c, target)
                if flips:
                    moves[(r, c)] = flips
        return moves

    def place_disc(self, row: int, col: int) -> MoveOutcome:
        if self.is_finished or self.is_aborted:
            raise ValueError("ゲームはすでに終了しています")
        flips = self.get_flips(row, col, self.current_turn)
        if not flips:
            raise ValueError(f"{coord_label(row, col)} には置けません")

        self.board[row][col] = self.current_turn
        for fr, fc in flips:
            self.board[fr][fc] = self.current_turn
        self.last_move = (row, col)
        self.move_count += 1

        next_turn = -self.current_turn
        passed_opponent = False
        if self.get_legal_moves(next_turn):
            self.current_turn = next_turn
        elif self.get_legal_moves(self.current_turn):
            # 相手が打てないので自動パス
            passed_opponent = True
        else:
            self.is_finished = True

        return MoveOutcome(
            row=row,
            col=col,
            flipped_count=len(flips),
            passed_opponent=passed_opponent,
            is_finished=self.is_finished,
        )

    def choose_cpu_move(
        self, difficulty: str = "normal", rng: random.Random | None = None
    ) -> tuple[int, int]:
        """難易度別の重み付け簡易アルゴリズムで次の1手を選ぶ。"""
        legal = self.get_legal_moves(self.current_turn)
        if not legal:
            raise ValueError("CPU に合法手がありません")
        r_gen = rng or random

        if difficulty == "easy":
            # かんたん: 重み付けを弱くし、ランダム要素を大きくする
            scored = [
                (POSITION_WEIGHTS[r][c] + len(flips) * 2 + r_gen.randint(-30, 30), (r, c))
                for (r, c), flips in legal.items()
            ]
            max_score = max(s for s, _ in scored)
            best = [m for s, m in scored if s == max_score]
            return r_gen.choice(best)

        if difficulty == "hard":
            # むずかしい: 1手先の相手最善手を減点する簡易先読み＋重み付け
            best_score = -10**9
            best_moves: list[tuple[int, int]] = []
            opponent = -self.current_turn
            for (r, c), flips in legal.items():
                self.board[r][c] = self.current_turn
                for fr, fc in flips:
                    self.board[fr][fc] = self.current_turn

                opp_legal = self.get_legal_moves(opponent)
                opp_best = (
                    max(POSITION_WEIGHTS[orow][ocol] for orow, ocol in opp_legal)
                    if opp_legal
                    else -50
                )

                self.board[r][c] = EMPTY
                for fr, fc in flips:
                    self.board[fr][fc] = opponent

                score = POSITION_WEIGHTS[r][c] * 10 + len(flips) - opp_best * 6
                if score > best_score:
                    best_score = score
                    best_moves = [(r, c)]
                elif score == best_score:
                    best_moves.append((r, c))
            return r_gen.choice(best_moves)

        # ふつう: 位置重み付け＋返し枚数
        best_score = -10**9
        best_moves = []
        for (r, c), flips in legal.items():
            score = POSITION_WEIGHTS[r][c] * 10 + len(flips)
            if score > best_score:
                best_score = score
                best_moves = [(r, c)]
            elif score == best_score:
                best_moves.append((r, c))
        return r_gen.choice(best_moves)


def render_board_png(game: OthelloGame) -> bytes:
    """現在のオセロ盤面を PNG バイト列として描画する。"""
    cell_size = 56
    margin = 36
    board_px = cell_size * BOARD_SIZE
    width = margin + board_px + 14
    height = margin + board_px + 14

    img = Image.new("RGB", (width, height), (30, 33, 36))
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.load_default(size=16)
        small_font = ImageFont.load_default(size=13)
    except TypeError:
        font = ImageFont.load_default()
        small_font = font

    # 盤面背景（緑）
    draw.rectangle(
        [margin, margin, margin + board_px, margin + board_px],
        fill=(24, 122, 64),
        outline=(15, 60, 32),
        width=2,
    )

    # 座標ヘッダー（A-H, 1-8）
    for i in range(BOARD_SIZE):
        cx = margin + i * cell_size + cell_size // 2
        cy = margin + i * cell_size + cell_size // 2
        draw.text((cx, margin // 2), COL_LABELS[i], fill=(225, 228, 232), font=font, anchor="mm")
        draw.text((margin // 2, cy), str(i + 1), fill=(225, 228, 232), font=font, anchor="mm")

    # グリッド線
    for i in range(1, BOARD_SIZE):
        x = margin + i * cell_size
        y = margin + i * cell_size
        draw.line([(x, margin), (x, margin + board_px)], fill=(14, 72, 38), width=2)
        draw.line([(margin, y), (margin + board_px, y)], fill=(14, 72, 38), width=2)

    # 星（4点）
    for sr in (2, 6):
        for sc in (2, 6):
            sx = margin + sc * cell_size
            sy = margin + sr * cell_size
            draw.ellipse([sx - 4, sy - 4, sx + 4, sy + 4], fill=(14, 72, 38))

    legal_moves = (
        {} if (game.is_finished or game.is_aborted) else game.get_legal_moves()
    )

    # 石と合法手マーカーの描画
    for r in range(BOARD_SIZE):
        for c in range(BOARD_SIZE):
            x0 = margin + c * cell_size
            y0 = margin + r * cell_size
            cx = x0 + cell_size // 2
            cy = y0 + cell_size // 2
            cell = game.board[r][c]

            if cell == BLACK:
                pad = 6
                draw.ellipse(
                    [x0 + pad, y0 + pad, x0 + cell_size - pad, y0 + cell_size - pad],
                    fill=(24, 24, 28),
                    outline=(70, 70, 78),
                    width=2,
                )
            elif cell == WHITE:
                pad = 6
                draw.ellipse(
                    [x0 + pad, y0 + pad, x0 + cell_size - pad, y0 + cell_size - pad],
                    fill=(242, 244, 248),
                    outline=(180, 185, 195),
                    width=2,
                )
            elif (r, c) in legal_moves:
                ring_pad = 12
                ring_color = (255, 215, 64) if game.current_turn == BLACK else (120, 220, 255)
                draw.ellipse(
                    [
                        x0 + ring_pad,
                        y0 + ring_pad,
                        x0 + cell_size - ring_pad,
                        y0 + cell_size - ring_pad,
                    ],
                    outline=ring_color,
                    width=2,
                )
                draw.text(
                    (cx, cy),
                    coord_label(r, c),
                    fill=ring_color,
                    font=small_font,
                    anchor="mm",
                )

            if game.last_move == (r, c):
                draw.ellipse(
                    [cx - 4, cy - 4, cx + 4, cy + 4],
                    fill=(235, 75, 75),
                )

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
