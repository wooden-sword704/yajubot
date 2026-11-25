"""サーバーのカスタム絵文字を使った神経衰弱ゲームを提供する Cog。"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from yajubot.bot import YajuBot
from yajubot.storage import ProxyStorage


@dataclass(frozen=True, slots=True)
class ShinkeiRewardSpec:
    """難易度ごとの手数別タドコイン報酬設定。"""

    base_amount: int
    fast_moves: int
    normal_moves: int
    slow_moves: int


SHINKEI_REWARD_SPECS: dict[str, ShinkeiRewardSpec] = {
    "easy": ShinkeiRewardSpec(base_amount=100, fast_moves=14, normal_moves=19, slow_moves=23),
    "normal": ShinkeiRewardSpec(base_amount=160, fast_moves=18, normal_moves=23, slow_moves=27),
    "hard": ShinkeiRewardSpec(base_amount=280, fast_moves=25, normal_moves=29, slow_moves=35),
    "ikisugi": ShinkeiRewardSpec(base_amount=810, fast_moves=40, normal_moves=55, slow_moves=65),
}


PERFECT_CLEAR_REWARDS: dict[str, tuple[int, int]] = {
    # difficulty_key: (required_pairs, bonus_tadocoin)
    "easy": (6, 30000),
    "normal": (8, 81000),
    "hard": (12, 93150),
    "ikisugi": (12, 1919810),
}


def calculate_shinkei_reward(difficulty_key: str, moves: int) -> tuple[int, str]:
    """クリア手数に応じたタドコイン獲得量と倍率ラベルを返す。"""
    spec = SHINKEI_REWARD_SPECS.get(difficulty_key)
    if spec is None:
        return 0, "対象外"
    perfect = PERFECT_CLEAR_REWARDS.get(difficulty_key)
    if perfect is not None and 0 < moves <= perfect[0]:
        return perfect[1], "ノーミス最短クリア！！！！！！！！！！！！！！！！！！！！！！！！！！ハギャぎ！？ハギャぎ！？ハギャぎ！？ハギャぎ！？ハギャぎ！？ハギャぎ！？ハギャぎ！？ハギャぎ！？ハギャぎ！？ハギャぎ！？ハギャぎ！？ハギャぎ！？"
    if moves <= spec.fast_moves:
        return (spec.base_amount * 3) // 2, "ハヤスギィ！1.5倍ボーナス"
    if moves <= spec.normal_moves:
        return spec.base_amount, "1.0倍"
    if moves <= spec.slow_moves:
        return spec.base_amount // 2, "0.5倍"
    return 0, "手数オーバー"


def apply_forced_shinkei_moves(game: ShinkeiGame, forced_result: str) -> None:
    """デバッグ用: 次回クリア時の手数を指定された結果ティアの値へすり替える。"""
    diff_key = game.difficulty.key
    spec = SHINKEI_REWARD_SPECS.get(diff_key)
    if forced_result == "perfect":
        game.moves = game.difficulty.required_pairs
    elif spec is not None:
        if forced_result == "fast":
            game.moves = spec.fast_moves
        elif forced_result == "normal":
            game.moves = spec.normal_moves
        elif forced_result == "slow":
            game.moves = spec.slow_moves
        elif forced_result == "over":
            game.moves = spec.slow_moves + 5


def _apply_shinkei_clear_rewards(
    storage: ProxyStorage,
    game: ShinkeiGame,
    elapsed_sec: float,
) -> str:
    """クリア時のタドコイン付与と自己ベスト記録を行い、表示用テキストを返す。"""
    reward, rate_label = calculate_shinkei_reward(game.difficulty.key, game.moves)
    lines: list[str] = []

    if not game.is_multiplayer:
        uid = game.players[0]
        is_best = storage.record_shinkei_clear(
            game.guild_id, game.difficulty.key, uid, elapsed_sec, game.moves
        )
        if reward > 0:
            new_bal = storage.add_tadocoin(game.guild_id, uid, reward)
            lines.append(
                f"<:TADOCOIN_TEMP:1574811641042698320> **+{reward:,}** タドコイン獲得！（{rate_label} / 所持: **{new_bal:,}**）"
            )
        if is_best:
            lines.append("🏆 **自己ベストタイム更新！**（`/leaderboard` に記録されたゾ）")
    else:
        max_score = max(game.scores.values(), default=0)
        winners = [uid for uid, s in game.scores.items() if s == max_score and s > 0]
        if reward > 0 and winners:
            mentions = []
            for uid in winners:
                storage.add_tadocoin(game.guild_id, uid, reward)
                mentions.append(f"<@{uid}>")
            lines.append(
                f"<:TADOCOIN_TEMP:1574811641042698320> 勝者 {' '.join(mentions)} に **+{reward:,}** タドコイン！（{rate_label}）"
            )
            losers = [uid for uid in game.players if uid not in winners]
            loser_reward = reward // 3
            if loser_reward > 0 and losers:
                loser_mentions = []
                for uid in losers:
                    storage.add_tadocoin(game.guild_id, uid, loser_reward)
                    loser_mentions.append(f"<@{uid}>")
                lines.append(
                    f"<:TADOCOIN_TEMP:1574811641042698320> 敗者 {' '.join(loser_mentions)} に **+{loser_reward:,}** タドコイン！"
                )
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class DifficultySpec:
    """神経衰弱の難易度定義。"""

    key: str
    label: str
    rows: int
    cols: int
    reveal_seconds: float
    shuffle_chance: float = 0.0

    @property
    def total_tiles(self) -> int:
        return self.rows * self.cols

    @property
    def has_center_empty(self) -> bool:
        return self.total_tiles % 2 == 1

    @property
    def center_index(self) -> int | None:
        return self.total_tiles // 2 if self.has_center_empty else None

    @property
    def required_pairs(self) -> int:
        return (self.total_tiles - (1 if self.has_center_empty else 0)) // 2


DIFFICULTIES: dict[str, DifficultySpec] = {
    "easy": DifficultySpec(
        key="easy",
        label="かんたん",
        rows=3,
        cols=4,
        reveal_seconds=2.5,
    ),
    "normal": DifficultySpec(
        key="normal",
        label="ふつう",
        rows=4,
        cols=4,
        reveal_seconds=2.0,
    ),
    "hard": DifficultySpec(
        key="hard",
        label="むずかしい",
        rows=5,
        cols=5,
        reveal_seconds=2.0,
    ),
    "ikisugi": DifficultySpec(
        key="ikisugi",
        label="ｲｷｽｷﾞｨ!ｲｸｩｲｸｲｸ...ｱｯﾊｯ､ﾝｱｯｰ!!!",
        rows=5,
        cols=5,
        reveal_seconds=0.8,
        shuffle_chance=0.1919,
    ),
}


@dataclass(slots=True)
class CardState:
    """盤面上の1マスの状態。"""

    index: int
    emoji: discord.Emoji | None  # 中央空きマスは None
    matched_by: int | None = None  # 揃えたユーザーの ID
    is_center: bool = False


@dataclass(slots=True)
class ShinkeiGame:
    """神経衰弱1ゲーム分の状態管理。"""

    guild_id: int
    host_id: int
    difficulty: DifficultySpec
    players: list[int]
    cards: list[CardState]
    turn_index: int = 0
    first_pick: int | None = None
    second_pick: int | None = None
    is_busy: bool = False
    is_aborted: bool = False
    moves: int = 0
    scores: dict[int, int] = field(default_factory=dict)
    started_at: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def current_player_id(self) -> int:
        return self.players[self.turn_index]

    @property
    def is_multiplayer(self) -> bool:
        return len(self.players) > 1

    @property
    def matched_pairs(self) -> int:
        return sum(1 for c in self.cards if c.matched_by is not None) // 2

    @property
    def is_finished(self) -> bool:
        return self.matched_pairs >= self.difficulty.required_pairs

    def advance_turn(self) -> None:
        self.turn_index = (self.turn_index + 1) % len(self.players)

    def forfeit_player(self, user_id: int) -> str:
        """プレイヤーを途中終了/離脱させる。戻り値: 'not_in_game' | 'aborted' | 'forfeited'"""
        if user_id not in self.players:
            return "not_in_game"
        if len(self.players) <= 1:
            self.is_aborted = True
            return "aborted"
        idx = self.players.index(user_id)
        self.players.pop(idx)
        if idx < self.turn_index:
            self.turn_index -= 1
        elif idx == self.turn_index:
            self.first_pick = None
            self.second_pick = None
            self.turn_index %= len(self.players)
        return "forfeited"

    def shuffle_unmatched(self) -> bool:
        """まだ揃っていないカードの絵文字位置をシャッフルする。"""
        unmatched = [
            c for c in self.cards if not c.is_center and c.matched_by is None
        ]
        if len(unmatched) < 4:
            return False
        emojis = [c.emoji for c in unmatched]
        random.shuffle(emojis)
        for c, em in zip(unmatched, emojis):
            c.emoji = em
        return True


def create_shinkei_game(
    guild: discord.Guild, difficulty: DifficultySpec, players: list[int]
) -> ShinkeiGame:
    """サーバー絵文字から必要数を選び、指定難易度の盤面を生成する。"""
    usable_emojis = [e for e in guild.emojis if e.available]
    needed = difficulty.required_pairs
    if len(usable_emojis) < needed:
        raise ValueError(
            f"サーバーのカスタム絵文字が {needed} 種類未満のため「{difficulty.label}」を開始できないゾ（現在 {len(usable_emojis)} 種類）。"
        )

    chosen = random.sample(usable_emojis, needed)
    deck: list[discord.Emoji] = chosen * 2
    random.shuffle(deck)

    cards: list[CardState] = []
    deck_iter = iter(deck)
    center_idx = difficulty.center_index
    for idx in range(difficulty.total_tiles):
        if center_idx is not None and idx == center_idx:
            cards.append(CardState(index=idx, emoji=None, is_center=True))
        else:
            cards.append(CardState(index=idx, emoji=next(deck_iter)))

    scores = {uid: 0 for uid in players}
    return ShinkeiGame(
        guild_id=guild.id,
        host_id=players[0],
        difficulty=difficulty,
        players=list(players),
        cards=cards,
        scores=scores,
    )


class ShinkeiBoardView(discord.ui.View):
    """神経衰弱盤面 View。"""

    def __init__(self, game: ShinkeiGame) -> None:
        super().__init__(timeout=900)
        self.game = game
        self._card_buttons: dict[int, discord.ui.Button] = {}
        self._quit_button: discord.ui.Button | None = None
        self._init_buttons()
        self._rebuild_buttons()

    def _init_buttons(self) -> None:
        cols = self.game.difficulty.cols
        for card in self.game.cards:
            row = card.index // cols
            btn = discord.ui.Button(
                label="⠀❓⠀",
                style=discord.ButtonStyle.secondary,
                custom_id=f"shinkei:card:{card.index}",
                row=row,
            )
            if card.is_center:
                btn.callback = self._handle_quit_click
            else:
                btn.callback = self._make_card_callback(card.index)
            self._card_buttons[card.index] = btn
            self.add_item(btn)

        if self.game.difficulty.rows < 5:
            quit_btn = discord.ui.Button(
                label="終了",
                emoji="🏳️",
                style=discord.ButtonStyle.danger,
                custom_id="shinkei:quit",
                row=self.game.difficulty.rows,
            )
            quit_btn.callback = self._handle_quit_click
            self._quit_button = quit_btn
            self.add_item(quit_btn)

    def _rebuild_buttons(self) -> None:
        ended = self.game.is_finished or self.game.is_aborted
        for card in self.game.cards:
            btn = self._card_buttons[card.index]
            if card.is_center:
                if ended:
                    btn.emoji = None
                    btn.label = "⠀・⠀"
                    btn.style = discord.ButtonStyle.secondary
                    btn.disabled = True
                else:
                    btn.emoji = "🏳️"
                    btn.label = "⠀"
                    btn.style = discord.ButtonStyle.danger
                    btn.disabled = False
            elif card.matched_by is not None:
                btn.emoji = card.emoji
                btn.label = "⠀"
                btn.style = discord.ButtonStyle.success
                btn.disabled = False
            elif card.index in (self.game.first_pick, self.game.second_pick):
                btn.emoji = card.emoji
                btn.label = "⠀"
                btn.style = discord.ButtonStyle.primary
                btn.disabled = False
            else:
                btn.emoji = None
                btn.label = "⠀❓⠀"
                btn.style = discord.ButtonStyle.secondary
                btn.disabled = self.game.is_busy or ended

        if self._quit_button is not None:
            self._quit_button.disabled = ended

    def _make_card_callback(self, card_index: int):
        async def callback(interaction: discord.Interaction) -> None:
            await self._handle_card_click(interaction, card_index)

        return callback

    def build_embed(
        self, status_note: str | None = None, *, reward_note: str | None = None
    ) -> discord.Embed:
        diff = self.game.difficulty
        if self.game.is_aborted:
            return discord.Embed(
                title=f"神経衰弱 途中終了（{diff.label}）",
                description=f"ゲームが途中終了されました。（獲得ペア: **{self.game.matched_pairs} / {diff.required_pairs}** / 手数: **{self.game.moves}手**）",
                color=discord.Color.dark_grey(),
            )
        if self.game.is_finished:
            elapsed = time.monotonic() - self.game.started_at
            embed = discord.Embed(
                title=f"やったぜ。（{diff.label}）",
                description=f"全 {diff.required_pairs} ペアを揃えた！（総手数: **{self.game.moves}手** / タイム: **{elapsed:.1f}秒**）",
                color=discord.Color.gold(),
            )
            if self.game.is_multiplayer:
                ranking = sorted(
                    self.game.scores.items(), key=lambda x: x[1], reverse=True
                )
                rank_lines = [
                    f"{i}. <@{uid}>: **{score}ペア**"
                    for i, (uid, score) in enumerate(ranking, start=1)
                ]
                embed.add_field(
                    name="最終スコア", value="\n".join(rank_lines), inline=False
                )
            if reward_note:
                embed.add_field(
                    name="報酬・記録", value=reward_note, inline=False
                )
            return embed

        embed = discord.Embed(
            title=f"🃏 神経衰弱（{diff.rows}×{diff.cols} / {diff.label}）",
            description=status_note or "カードを2枚めくって同じ絵文字を揃えなきゃ（使命感）",
            color=discord.Color.red() if diff.key == "ikisugi" else discord.Color.blurple(),
        )
        embed.add_field(
            name="進捗",
            value=f"{self.game.matched_pairs} / {diff.required_pairs} ペア（{self.game.moves}手）",
            inline=True,
        )
        if self.game.is_multiplayer:
            embed.add_field(
                name="現在の手番",
                value=f"<@{self.game.current_player_id}>",
                inline=True,
            )
            score_text = " / ".join(
                f"<@{uid}>: **{self.game.scores.get(uid, 0)}**"
                for uid in self.game.players
            )
            embed.add_field(name="獲得ペア数", value=score_text, inline=False)
        else:
            embed.add_field(
                name="プレイヤー",
                value=f"<@{self.game.current_player_id}>",
                inline=True,
            )
        return embed

    async def _handle_quit_click(self, interaction: discord.Interaction) -> None:
        if self.game.is_finished or self.game.is_aborted:
            await interaction.response.defer()
            return
        if interaction.user.id not in self.game.players:
            await interaction.response.send_message(
                "このゲームに参加していないゾ。", ephemeral=True
            )
            return
        msg = (
            "このゲームを途中終了やっちゃうよ！？やっちゃうよ！？"
            if len(self.game.players) <= 1
            else "ゲームから途中離脱やっちゃうよ！？やっちゃうよ！？"
        )
        confirm_view = ShinkeiQuitConfirmView(self, interaction.message)
        await interaction.response.send_message(msg, view=confirm_view, ephemeral=True)

    async def _safe_defer(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.defer()
        except (discord.NotFound, discord.HTTPException, aiohttp.ClientError):
            pass

    async def _handle_card_click(
        self, interaction: discord.Interaction, card_index: int
    ) -> None:
        async with self.game.lock:
            if self.game.is_finished or self.game.is_aborted or self.game.is_busy:
                await self._safe_defer(interaction)
                return

            if interaction.user.id != self.game.current_player_id:
                try:
                    await interaction.response.send_message(
                        f"今は <@{self.game.current_player_id}> の手番だゾ。",
                        ephemeral=True,
                    )
                except (discord.NotFound, discord.HTTPException, aiohttp.ClientError):
                    pass
                return

            card = self.game.cards[card_index]
            if card.is_center or card.matched_by is not None:
                await self._safe_defer(interaction)
                return

            if self.game.first_pick is None:
                self.game.first_pick = card_index
                self._rebuild_buttons()
                try:
                    await interaction.response.edit_message(
                        embed=self.build_embed("1枚目をめくったゾ。2枚目を選んでくれよな～頼むよ～"),
                        view=self,
                    )
                except (discord.NotFound, discord.HTTPException, aiohttp.ClientError):
                    self.game.first_pick = None
                    self._rebuild_buttons()
                return

            if card_index == self.game.first_pick:
                await self._safe_defer(interaction)
                return

            # 2枚目
            self.game.second_pick = card_index
            self.game.moves += 1
            first_card = self.game.cards[self.game.first_pick]
            second_card = card

            if (
                first_card.emoji is not None
                and second_card.emoji is not None
                and first_card.emoji.id == second_card.emoji.id
            ):
                # 一致！
                first_card.matched_by = interaction.user.id
                second_card.matched_by = interaction.user.id
                self.game.scores[interaction.user.id] = (
                    self.game.scores.get(interaction.user.id, 0) + 1
                )
                self.game.first_pick = None
                self.game.second_pick = None
                self._rebuild_buttons()
                try:
                    if self.game.is_finished:
                        elapsed = time.monotonic() - self.game.started_at
                        forced = getattr(
                            interaction.client, "debug_next_shinkei_result", None
                        )
                        if forced is not None:
                            interaction.client.debug_next_shinkei_result = None  # type: ignore[attr-defined]
                            apply_forced_shinkei_moves(self.game, forced)
                        storage = getattr(interaction.client, "storage", None)
                        reward_note = (
                            _apply_shinkei_clear_rewards(storage, self.game, elapsed)
                            if isinstance(storage, ProxyStorage)
                            else None
                        )
                        await interaction.response.edit_message(
                            embed=self.build_embed(reward_note=reward_note), view=self
                        )
                    else:
                        note = f"✨ <@{interaction.user.id}> が {first_card.emoji} を揃えたゾ！"
                        if self.game.is_multiplayer:
                            note += " 連続手番！"
                        await interaction.response.edit_message(
                            embed=self.build_embed(note), view=self
                        )
                except (discord.NotFound, discord.HTTPException, aiohttp.ClientError):
                    pass
                return

            # 不一致：一定時間見せてから裏に戻す
            self.game.is_busy = True
            self._rebuild_buttons()
            try:
                await interaction.response.edit_message(
                    embed=self.build_embed("❌ ハズレ！まもなく裏に戻るゾ。みとけよみとけよ～"),
                    view=self,
                )
            except (discord.NotFound, discord.HTTPException, aiohttp.ClientError):
                self.game.is_busy = False
                self.game.first_pick = None
                self.game.second_pick = None
                self._rebuild_buttons()
                return

        await asyncio.sleep(self.game.difficulty.reveal_seconds)

        async with self.game.lock:
            if self.game.is_aborted or self.game.is_finished:
                self.game.is_busy = False
                return
            self.game.first_pick = None
            self.game.second_pick = None
            self.game.is_busy = False
            shuffled = False
            if (
                self.game.difficulty.shuffle_chance > 0.0
                and random.random() < self.game.difficulty.shuffle_chance
            ):
                shuffled = self.game.shuffle_unmatched()

            if self.game.is_multiplayer:
                self.game.advance_turn()
            self._rebuild_buttons()
            note = (
                f"次は <@{self.game.current_player_id}> の手番です！"
                if self.game.is_multiplayer
                else "次のカードを選んでくれよ〜（マジキチスマイル）"
            )
            if shuffled:
                note = "🌪️ **イキスギミック発動！残りのカードがシャッフルされたゾ！！（絶望）**\n" + note
            try:
                await interaction.edit_original_response(
                    embed=self.build_embed(note), view=self
                )
            except (discord.NotFound, discord.HTTPException, aiohttp.ClientError):
                pass


class ShinkeiQuitConfirmView(discord.ui.View):
    """途中終了・離脱の確認 View。"""

    def __init__(
        self, board_view: ShinkeiBoardView, board_message: discord.Message | None
    ) -> None:
        super().__init__(timeout=60)
        self.board_view = board_view
        self.board_message = board_message

    @discord.ui.button(label="終了/離脱", style=discord.ButtonStyle.danger)
    async def confirm_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        game = self.board_view.game
        async with game.lock:
            if game.is_finished or game.is_aborted:
                await interaction.response.edit_message(
                    content="ゲームはすでに終了してるゾ", view=None
                )
                return
            outcome = game.forfeit_player(interaction.user.id)
            if outcome == "not_in_game":
                await interaction.response.edit_message(
                    content="このゲームに参加してないゾ", view=None
                )
                return
            self.board_view._rebuild_buttons()
            if self.board_message is not None:
                try:
                    await self.board_message.edit(
                        embed=self.board_view.build_embed(), view=self.board_view
                    )
                except (discord.NotFound, discord.HTTPException):
                    pass

        if outcome == "aborted":
            await interaction.response.edit_message(
                content="ゲームを途中終了したゾ", view=None
            )
        else:
            await interaction.response.edit_message(
                content="ゲームから離脱したゾ。この戦いは負けた判定になったゾ", view=None
            )


MAX_MULTIPLAYER_PLAYERS = 4


class ShinkeiLobbyLeaveConfirmView(discord.ui.View):
    """マルチプレイ待機ロビーからの退出・途中終了確認 View。"""

    def __init__(
        self, lobby_view: ShinkeiLobbyView, lobby_message: discord.Message | None
    ) -> None:
        super().__init__(timeout=60)
        self.lobby_view = lobby_view
        self.lobby_message = lobby_message

    @discord.ui.button(label="はい（実行する）", style=discord.ButtonStyle.danger)
    async def confirm_leave(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        lv = self.lobby_view
        if lv.is_closed:
            await interaction.response.edit_message(
                content="このロビーはすでに終了・開始されているゾ。", view=None
            )
            return

        if interaction.user.id not in lv.players:
            await interaction.response.edit_message(
                content="すでにロビーから退出しているゾ。", view=None
            )
            return

        if interaction.user.id == lv.host_id:
            lv.is_closed = True
            lv.stop()
            closed_embed = discord.Embed(
                title=f"🃏 神経衰弱 募集終了（{lv.difficulty.label}）",
                description="ホストによりロビーが途中終了されました。",
                color=discord.Color.dark_grey(),
            )
            if self.lobby_message is not None:
                try:
                    await self.lobby_message.edit(embed=closed_embed, view=None)
                except (discord.NotFound, discord.HTTPException, aiohttp.ClientError):
                    pass
            await interaction.response.edit_message(
                content="ロビーを途中終了したゾ。", view=None
            )
            return

        lv.players.remove(interaction.user.id)
        if len(lv.players) < MAX_MULTIPLAYER_PLAYERS:
            lv.join_button.disabled = False
        if self.lobby_message is not None:
            try:
                await self.lobby_message.edit(embed=lv.build_embed(), view=lv)
            except (discord.NotFound, discord.HTTPException, aiohttp.ClientError):
                pass
        await interaction.response.edit_message(
            content="ロビーから退出したゾ。", view=None
        )

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel_leave(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(
            content="キャンセルしたゾ。", view=None
        )


class ShinkeiLobbyView(discord.ui.View):
    """マルチプレイ参加募集ロビー View。"""

    def __init__(self, host_id: int, difficulty: DifficultySpec) -> None:
        super().__init__(timeout=300)
        self.host_id = host_id
        self.difficulty = difficulty
        self.players: list[int] = [host_id]
        self.is_closed: bool = False

    def build_embed(self) -> discord.Embed:
        member_list = "\n".join(f"• <@{uid}>" for uid in self.players)
        embed = discord.Embed(
            title=f"🃏 神経衰弱（マルチプレイ募集 / {self.difficulty.label}）",
            description="参加したいホモは「参加する」ボタンを押そう（提案）\nホストが「ゲーム開始」を押すと始まるゾ",
            color=discord.Color.green(),
        )
        embed.add_field(
            name=f"参加メンバー（{len(self.players)} / {MAX_MULTIPLAYER_PLAYERS}人）",
            value=member_list,
            inline=False,
        )
        return embed

    @discord.ui.button(label="参加する", emoji="✋", style=discord.ButtonStyle.primary)
    async def join_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id in self.players:
            await interaction.response.send_message(
                "すでに参加してるゾ。", ephemeral=True
            )
            return
        if len(self.players) >= MAX_MULTIPLAYER_PLAYERS:
            await interaction.response.send_message(
                f"最大 {MAX_MULTIPLAYER_PLAYERS} 人までしか参加できないゾ。",
                ephemeral=True,
            )
            return
        self.players.append(interaction.user.id)
        if len(self.players) >= MAX_MULTIPLAYER_PLAYERS:
            button.disabled = True
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="ゲーム開始", emoji="▶️", style=discord.ButtonStyle.success)
    async def start_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.host_id:
            await interaction.response.send_message(
                "ゲーム開始はホストのみ押せるゾ。", ephemeral=True
            )
            return
        if interaction.guild is None:
            return
        game = None
        try:
            game = create_shinkei_game(interaction.guild, self.difficulty, self.players)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        board = ShinkeiBoardView(game)
        self.is_closed = True
        self.stop()
        await interaction.response.edit_message(content=None, embed=board.build_embed(), view=board)

    @discord.ui.button(label="退出 / 終了", emoji="🚪", style=discord.ButtonStyle.danger)
    async def leave_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if self.is_closed:
            await interaction.response.send_message(
                "このロビーはすでに終了・開始されているゾ。", ephemeral=True
            )
            return
        if interaction.user.id not in self.players:
            await interaction.response.send_message(
                "このロビーに参加していないゾ。", ephemeral=True
            )
            return
        msg = (
            "ホストが抜けるとロビーが途中終了になるゾ。本当に終了する？"
            if interaction.user.id == self.host_id
            else "ロビーから退出するゾ。よろしいですか？"
        )
        confirm_view = ShinkeiLobbyLeaveConfirmView(self, interaction.message)
        await interaction.response.send_message(msg, view=confirm_view, ephemeral=True)


class ShinkeiModeSelectView(discord.ui.View):
    """1人プレイ / マルチプレイ選択 View。"""

    def __init__(self, host_id: int, difficulty: DifficultySpec) -> None:
        super().__init__(timeout=120)
        self.host_id = host_id
        self.difficulty = difficulty

    @discord.ui.button(label="1人プレイ", emoji="👤", style=discord.ButtonStyle.primary)
    async def solo_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.host_id:
            await interaction.response.send_message(
                "コマンドを実行した本人のみ選択できるゾ。", ephemeral=True
            )
            return
        if interaction.guild is None:
            return
        try:
            game = create_shinkei_game(
                interaction.guild, self.difficulty, [self.host_id]
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        board = ShinkeiBoardView(game)
        self.stop()
        await interaction.response.edit_message(content=None, embed=board.build_embed(), view=board)

    @discord.ui.button(
        label="マルチプレイ（募集）", emoji="👥", style=discord.ButtonStyle.success
    )
    async def multi_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.host_id:
            await interaction.response.send_message(
                "コマンドを実行した本人のみ選択できるゾ。", ephemeral=True
            )
            return
        lobby = ShinkeiLobbyView(self.host_id, self.difficulty)
        self.stop()
        await interaction.response.edit_message(content=None, embed=lobby.build_embed(), view=lobby)


IKISUGI_GIMMICK_NOTE = (
    "\n\n **イキスギミックを説明して差し上げろ**\n"
    "・まずうちさぁ…外した時の猶予時間、**0.8秒**しかないんだけど…（脳みそ）焼いてかない？\n"
    "・外すと **15%の確率** で残りのカードが勝手に **シャッフル** されるゾ。何度もリピートして見てくれ（絶望）\n"
    "・おっ、大丈夫か大丈夫か？諦めたい時は真ん中の「🏳️」から逃げよう。 馬鹿野郎お前俺は勝つぞお前！"
)


class ShinkeiDifficultySelectView(discord.ui.View):
    """難易度選択 View。"""

    def __init__(self, host_id: int) -> None:
        super().__init__(timeout=120)
        self.host_id = host_id

    async def _choose(
        self, interaction: discord.Interaction, diff_key: str
    ) -> None:
        if interaction.user.id != self.host_id:
            await interaction.response.send_message(
                "コマンドを実行した本人のみ選択できるゾ。", ephemeral=True
            )
            return
        if interaction.guild is None:
            return
        diff = DIFFICULTIES[diff_key]
        usable_count = sum(1 for e in interaction.guild.emojis if e.available)
        if usable_count < diff.required_pairs:
            await interaction.response.send_message(
                f"サーバーのカスタム絵文字が {diff.required_pairs} 種類未満のため「{diff.label}」を開始できないゾ（現在 {usable_count} 種類）。",
                ephemeral=True,
            )
            return
        self.stop()
        mode_view = ShinkeiModeSelectView(self.host_id, diff)
        note = IKISUGI_GIMMICK_NOTE if diff.key == "ikisugi" else ""
        await interaction.response.edit_message(
            content=f"🃏 **神経衰弱（{diff.rows}×{diff.cols} / {diff.label}）**\nプレイモードを選択して、六尺、サポーターになって、H、しよう（提案）{note}",
            view=mode_view,
        )

    @discord.ui.button(label="かんたん (3×4)", style=discord.ButtonStyle.success, row=0)
    async def easy_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._choose(interaction, "easy")

    @discord.ui.button(label="ふつう (4×4)", style=discord.ButtonStyle.primary, row=0)
    async def normal_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._choose(interaction, "normal")

    @discord.ui.button(label="むずかしい (5×5)", style=discord.ButtonStyle.secondary, row=1)
    async def hard_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._choose(interaction, "hard")

    @discord.ui.button(
        label="ｲｷｽｷﾞｨ!ｲｸｩｲｸｲｸ...ｱｯﾊｯ､ﾝｱｯｰ!!! (5×5、ｲｷｽｷﾞﾐｯｸ付き)",
        style=discord.ButtonStyle.danger,
        row=1,
    )
    async def ikisugi_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._choose(interaction, "ikisugi")


class ShinkeiCog(commands.Cog):
    """神経衰弱コマンドを提供する Cog。"""

    def __init__(self, bot: YajuBot) -> None:
        self.bot = bot

    @app_commands.command(
        name="shinkei",
        description="鯖の絵文字で視認性がカスすぎる神経衰弱をしよう（提案）",
    )
    @app_commands.guild_only()
    async def shinkei_cmd(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "サーバー内のチャンネルでのみ利用できるからね。そして天は泣き、大地は震えるだろうね。", ephemeral=True
            )
            return

        view = ShinkeiDifficultySelectView(interaction.user.id)
        await interaction.response.send_message(
            "🃏 **神経衰弱**\n難易度を選択してください、なんでもしますから（なんでもするとは言ってない）",
            view=view,
        )


async def setup(bot: YajuBot) -> None:
    await bot.add_cog(ShinkeiCog(bot))
