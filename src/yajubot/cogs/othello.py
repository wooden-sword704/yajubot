"""画像盤面＋合法手ドロップダウン操作の 8×8 オセロ Cog。"""

from __future__ import annotations

import asyncio
import io
import logging
import random

import discord
from discord import app_commands
from discord.ext import commands

from yajubot.bot import YajuBot
from yajubot.othello_engine import (
    BLACK,
    WHITE,
    OthelloGame,
    coord_label,
    parse_coord_label,
    render_board_png,
)

logger = logging.getLogger(__name__)

CPU_USER_ID = -1
MAX_SELECT_OPTIONS = 25
AI_DIFFICULTY_LABELS: dict[str, str] = {
    "easy": "かんたん",
    "normal": "ふつう",
    "hard": "むずかしい",
}


def _format_player(user_id: int) -> str:
    return "**AI**" if user_id == CPU_USER_ID else f"<@{user_id}>"


class OthelloMoveSelect(discord.ui.Select):
    """合法手を選択するドロップダウン。"""

    def __init__(
        self,
        board_view: OthelloBoardView,
        options: list[discord.SelectOption],
        placeholder: str,
        row: int,
    ) -> None:
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            row=row,
        )
        self.board_view = board_view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.board_view.handle_move_select(interaction, self.values[0])


class OthelloBoardView(discord.ui.View):
    """進行中のオセロ盤面 View（画像＋ドロップダウン操作）。"""

    def __init__(
        self,
        bot: YajuBot,
        guild_id: int,
        game: OthelloGame,
        *,
        bet: int = 0,
        is_practice: bool = False,
        ai_difficulty: str = "normal",
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id
        self.game = game
        self.bet = bet
        self.is_practice = is_practice
        self.ai_difficulty = ai_difficulty
        self.lock = asyncio.Lock()
        self.last_note: str = "置きたいマスを下のドロップダウンから選んでくれよな〜"
        self._payout_done = False

        self._run_cpu_turns()
        self._rebuild_controls()

    def _run_cpu_turns(self) -> None:
        if not self.is_practice or self.game.is_finished or self.game.is_aborted:
            return
        for _ in range(64):
            if (
                self.game.is_finished
                or self.game.is_aborted
                or self.game.current_player_id != CPU_USER_ID
            ):
                break
            r, c = self.game.choose_cpu_move(self.ai_difficulty)
            outcome = self.game.place_disc(r, c)
            self.last_note = f"AI が **{coord_label(r, c)}** に置きました（{outcome.flipped_count}枚返し）"
            if outcome.passed_opponent and not outcome.is_finished:
                self.last_note += "\nあなたに置ける場所がないため、AI の連続手番です。"

    def _settle_bets_if_needed(self) -> str:
        if self._payout_done or self.bet <= 0 or self.is_practice:
            return ""
        self._payout_done = True
        total_pot = self.bet * 2

        if self.game.is_aborted and self.game.aborted_by is not None:
            winner_id = (
                self.game.white_user_id
                if self.game.aborted_by == self.game.black_user_id
                else self.game.black_user_id
            )
            new_bal = self.bot.storage.add_tadocoin(self.guild_id, winner_id, total_pot)
            return f"相手が投了したため <@{winner_id}> に **+{total_pot:,}** タドコイン（所持: {new_bal:,}）"

        if self.game.is_aborted:
            self.bot.storage.add_tadocoin(self.guild_id, self.game.black_user_id, self.bet)
            self.bot.storage.add_tadocoin(self.guild_id, self.game.white_user_id, self.bet)
            return "タイムアウトのため双方に賭け金を返金しました。"

        black_cnt, white_cnt = self.game.count_discs()
        if black_cnt > white_cnt:
            winner_id = self.game.black_user_id
            new_bal = self.bot.storage.add_tadocoin(self.guild_id, winner_id, total_pot)
            return f"勝者 <@{winner_id}> に **+{total_pot:,}** タドコイン（所持: {new_bal:,}）"
        if white_cnt > black_cnt:
            winner_id = self.game.white_user_id
            new_bal = self.bot.storage.add_tadocoin(self.guild_id, winner_id, total_pot)
            return f"勝者 <@{winner_id}> に **+{total_pot:,}** タドコイン（所持: {new_bal:,}）"

        self.bot.storage.add_tadocoin(self.guild_id, self.game.black_user_id, self.bet)
        self.bot.storage.add_tadocoin(self.guild_id, self.game.white_user_id, self.bet)
        return f"引き分けのため双方に **{self.bet:,}** タドコインを返金しました。"

    def _rebuild_controls(self) -> None:
        self.clear_items()
        if self.game.is_finished or self.game.is_aborted:
            return

        legal = self.game.get_legal_moves()
        sorted_moves = sorted(legal.items(), key=lambda item: (item[0][0], item[0][1]))
        options = [
            discord.SelectOption(
                label=f"{coord_label(r, c)}（{len(flips)}枚返し）",
                value=coord_label(r, c),
            )
            for (r, c), flips in sorted_moves
        ]

        # 25個を超える場合は複数のドロップダウンに分割する（座標テキスト入力は使わない）
        chunks = [
            options[i : i + MAX_SELECT_OPTIONS]
            for i in range(0, len(options), MAX_SELECT_OPTIONS)
        ]
        for idx, chunk in enumerate(chunks[:2]):
            first_lbl = chunk[0].value
            last_lbl = chunk[-1].value
            placeholder = (
                f"石を置く場所を選択（{first_lbl}〜{last_lbl}）"
                if len(chunks) > 1
                else "石を置く場所を選択（黄・水色の丸）"
            )
            self.add_item(OthelloMoveSelect(self, chunk, placeholder, row=idx))

        forfeit_row = min(4, len(chunks[:2]))
        forfeit_btn = discord.ui.Button(
            label="投了する",
            style=discord.ButtonStyle.danger,
            row=forfeit_row,
        )
        forfeit_btn.callback = self._on_forfeit
        self.add_item(forfeit_btn)

    def build_embed_and_file(self) -> tuple[discord.Embed, discord.File]:
        black_cnt, white_cnt = self.game.count_discs()
        png_bytes = render_board_png(self.game)
        file = discord.File(io.BytesIO(png_bytes), filename="othello.png")

        mode_tag = (
            f"AI対戦 / {AI_DIFFICULTY_LABELS.get(self.ai_difficulty, self.ai_difficulty)}"
            if self.is_practice
            else None
        )
        if self.game.is_aborted:
            title = f"オセロ 途中終了（{mode_tag}）" if mode_tag else "オセロ 途中終了"
            color = discord.Color.dark_grey()
        elif self.game.is_finished:
            if black_cnt > white_cnt:
                winner_text = f"黒 {_format_player(self.game.black_user_id)} の勝ち"
            elif white_cnt > black_cnt:
                winner_text = f"白 {_format_player(self.game.white_user_id)} の勝ち"
            else:
                winner_text = "引き分け"
            title = (
                f"オセロ終局（{mode_tag}） — {black_cnt} 対 {white_cnt}"
                if mode_tag
                else f"オセロ終局 — {black_cnt} 対 {white_cnt}"
            )
            color = discord.Color.gold()
            self.last_note = winner_text
        else:
            turn_stone = "黒" if self.game.current_turn == BLACK else "白"
            title = (
                f"オセロ（{mode_tag} / {turn_stone}の手番）"
                if mode_tag
                else f"オセロ（{turn_stone}の手番）"
            )
            color = discord.Color.green()

        embed = discord.Embed(title=title, description=self.last_note, color=color)
        embed.add_field(
            name="黒",
            value=f"{_format_player(self.game.black_user_id)}\n**{black_cnt}** 枚",
            inline=True,
        )
        embed.add_field(
            name="白",
            value=f"{_format_player(self.game.white_user_id)}\n**{white_cnt}** 枚",
            inline=True,
        )
        if not self.game.is_finished and not self.game.is_aborted:
            embed.add_field(
                name="現在の手番",
                value=_format_player(self.game.current_player_id),
                inline=True,
            )

        if self.game.is_finished or self.game.is_aborted:
            reward_text = self._settle_bets_if_needed()
            if reward_text:
                embed.add_field(name="精算", value=reward_text, inline=False)

        embed.set_image(url="attachment://othello.png")
        return embed, file

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        logger.exception("OthelloBoardView でエラーが発生しました: %s", error)
        msg = "オセロの処理中にエラーが発生したゾ。"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except (discord.NotFound, discord.HTTPException):
            pass

    async def handle_move_select(
        self, interaction: discord.Interaction, coord_str: str
    ) -> None:
        async with self.lock:
            if self.game.is_finished or self.game.is_aborted:
                await interaction.response.send_message(
                    "この対局はすでに終了しているゾ。", ephemeral=True
                )
                return
            if interaction.user.id != self.game.current_player_id:
                await interaction.response.send_message(
                    f"今は {_format_player(self.game.current_player_id)} の手番だゾ。",
                    ephemeral=True,
                )
                return

            try:
                r, c = parse_coord_label(coord_str)
                outcome = self.game.place_disc(r, c)
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return

            self.last_note = f"<@{interaction.user.id}> が **{coord_str}** に置きました（{outcome.flipped_count}枚返し）"
            if outcome.passed_opponent and not outcome.is_finished:
                self.last_note += "\n相手に置ける場所がないためパスされました。"

            self._run_cpu_turns()
            self._rebuild_controls()
            if self.game.is_finished or self.game.is_aborted:
                self.stop()

            embed, file = self.build_embed_and_file()
            await interaction.response.edit_message(
                embed=embed, attachments=[file], view=self
            )

    async def _on_forfeit(self, interaction: discord.Interaction) -> None:
        async with self.lock:
            if self.game.is_finished or self.game.is_aborted:
                await interaction.response.send_message(
                    "この対局はすでに終了しているゾ。", ephemeral=True
                )
                return
            if self.game.color_of(interaction.user.id) is None:
                await interaction.response.send_message(
                    "この対局のプレイヤーのみ投了できるゾ。", ephemeral=True
                )
                return
            self.game.is_aborted = True
            self.game.aborted_by = interaction.user.id
            self.last_note = f"<@{interaction.user.id}> が投了しました。"
            self._rebuild_controls()
            self.stop()
            embed, file = self.build_embed_and_file()
            await interaction.response.edit_message(
                embed=embed, attachments=[file], view=self
            )


class OthelloLobbyView(discord.ui.View):
    """オセロ対人戦の募集ロビー View。"""

    def __init__(
        self, bot: YajuBot, guild_id: int, host_id: int, bet: int
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id
        self.host_id = host_id
        self.bet = bet
        self.players: list[int] = [host_id]
        self.lock = asyncio.Lock()
        self.is_closed = False

    def build_embed(self) -> discord.Embed:
        bet_text = f"**{self.bet:,}** タドコイン" if self.bet > 0 else "無料（賭けなし）"
        embed = discord.Embed(
            title="オセロ対戦相手募集中",
            description="「参加する」ボタンで2人目が揃ったら、ホストが「対局開始」を押してください。",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="賭け金", value=bet_text, inline=True)
        member_lines = [
            f"{i}. <@{uid}>" + ("（ホスト）" if uid == self.host_id else "")
            for i, uid in enumerate(self.players, start=1)
        ]
        embed.add_field(
            name=f"参加者 ({len(self.players)} / 2人)",
            value="\n".join(member_lines),
            inline=False,
        )
        return embed

    @discord.ui.button(label="参加する", style=discord.ButtonStyle.success)
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
            if len(self.players) >= 2:
                await interaction.response.send_message(
                    "すでに満員（2人）だゾ。", ephemeral=True
                )
                return
            if self.bet > 0:
                ok, bal = self.bot.storage.try_spend_tadocoin(
                    self.guild_id, interaction.user.id, self.bet
                )
                if not ok:
                    await interaction.response.send_message(
                        f"タドコインが足りないゾ！（必要: **{self.bet:,}** / 所持: **{bal:,}**）",
                        ephemeral=True,
                    )
                    return
            self.players.append(interaction.user.id)
            await interaction.response.edit_message(
                embed=self.build_embed(), view=self
            )

    @discord.ui.button(label="抜ける / 解散", style=discord.ButtonStyle.secondary)
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
                if self.bet > 0:
                    for uid in self.players:
                        self.bot.storage.add_tadocoin(self.guild_id, uid, self.bet)
                self.stop()
                embed = discord.Embed(
                    title="オセロ募集解散",
                    description="ホストが解散しました。",
                    color=discord.Color.dark_grey(),
                )
                await interaction.response.edit_message(embed=embed, view=None)
                return

            self.players.remove(interaction.user.id)
            if self.bet > 0:
                self.bot.storage.add_tadocoin(
                    self.guild_id, interaction.user.id, self.bet
                )
            await interaction.response.edit_message(
                embed=self.build_embed(), view=self
            )

    @discord.ui.button(label="対局開始", style=discord.ButtonStyle.primary)
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
                    "対局を開始できるのはホストだけだゾ。", ephemeral=True
                )
                return
            if len(self.players) < 2:
                await interaction.response.send_message(
                    "対戦相手がまだ参加していないゾ！", ephemeral=True
                )
                return

            p_list = list(self.players)
            random.shuffle(p_list)
            game = OthelloGame(black_user_id=p_list[0], white_user_id=p_list[1])
            self.is_closed = True
            self.stop()

            board_view = OthelloBoardView(
                self.bot, self.guild_id, game, bet=self.bet, is_practice=False
            )
            embed, file = board_view.build_embed_and_file()
            await interaction.response.edit_message(
                embed=embed, attachments=[file], view=board_view
            )


class OthelloAiDifficultyView(discord.ui.View):
    """オセロ AI対戦の難易度選択 View。"""

    def __init__(self, bot: YajuBot, guild_id: int, host_id: int) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.guild_id = guild_id
        self.host_id = host_id

    async def _start_ai(
        self, interaction: discord.Interaction, difficulty: str
    ) -> None:
        if interaction.user.id != self.host_id:
            await interaction.response.send_message(
                "コマンドを実行した本人のみ選択できるゾ。", ephemeral=True
            )
            return
        self.stop()
        game = OthelloGame(black_user_id=self.host_id, white_user_id=CPU_USER_ID)
        board_view = OthelloBoardView(
            self.bot,
            self.guild_id,
            game,
            bet=0,
            is_practice=True,
            ai_difficulty=difficulty,
        )
        embed, file = board_view.build_embed_and_file()
        await interaction.response.edit_message(
            content=None, embed=embed, attachments=[file], view=board_view
        )

    @discord.ui.button(label="かんたん", style=discord.ButtonStyle.success)
    async def easy_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._start_ai(interaction, "easy")

    @discord.ui.button(label="ふつう", style=discord.ButtonStyle.primary)
    async def normal_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._start_ai(interaction, "normal")

    @discord.ui.button(label="むずかしい", style=discord.ButtonStyle.danger)
    async def hard_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._start_ai(interaction, "hard")


class OthelloCog(commands.Cog):
    """8×8 オセロコマンドを提供する Cog。"""

    def __init__(self, bot: YajuBot) -> None:
        self.bot = bot

    @app_commands.command(
        name="othello",
        description="画像盤面＋ドロップダウン操作の8×8オセロ（対人戦）を始めるゾ",
    )
    @app_commands.describe(bet="賭けるタドコイン（0 で無料対戦）")
    @app_commands.guild_only()
    async def othello_cmd(
        self,
        interaction: discord.Interaction,
        bet: app_commands.Range[int, 0, 1_000_000] = 0,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "サーバー内のチャンネルでのみ利用できるゾ。", ephemeral=True
            )
            return

        if bet > 0:
            ok, bal = self.bot.storage.try_spend_tadocoin(
                guild.id, interaction.user.id, bet
            )
            if not ok:
                await interaction.response.send_message(
                    f"タドコインが足りないゾ！（必要: **{bet:,}** / 所持: **{bal:,}**）",
                    ephemeral=True,
                )
                return

        lobby = OthelloLobbyView(self.bot, guild.id, interaction.user.id, bet=bet)
        await interaction.response.send_message(embed=lobby.build_embed(), view=lobby)

    @app_commands.command(
        name="othello_ai",
        description="AI対戦の8×8オセロを始めるゾ（3段階の難易度）",
    )
    @app_commands.guild_only()
    async def othello_ai_cmd(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "サーバー内のチャンネルでのみ利用できるゾ。", ephemeral=True
            )
            return

        view = OthelloAiDifficultyView(self.bot, guild.id, interaction.user.id)
        embed = discord.Embed(
            title="オセロ（AI対戦）",
            description=(
                "難易度を選択してください。\n"
                "※ AI対戦ではタドコインの消費・付与はありません。"
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, view=view)


async def setup(bot: YajuBot) -> None:
    await bot.add_cog(OthelloCog(bot))
