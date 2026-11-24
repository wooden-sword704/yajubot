"""ルーレット機能を提供する Cog。"""

from __future__ import annotations

import io
import logging
import random
import re
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from yajubot.bot import YajuBot
from yajubot.cogs.proxy import can_manage_bot
from yajubot.storage import RouletteCandidateRecord, RouletteRecord

logger = logging.getLogger(__name__)

_DURATION_RE = re.compile(
    r"^\s*(?:(\d+)\s*(?:w|週|週間))?\s*(?:(\d+)\s*(?:d|日))?\s*(?:(\d+)\s*(?:h|時間))?\s*(?:(\d+)\s*(?:m|分))?\s*$",
    re.IGNORECASE,
)


def parse_duration_minutes(raw: str) -> int | None:
    """時間制限文字列を分単位にパースする（1分〜1週間 = 10080分）。"""
    text = raw.strip()
    if not text:
        return None
    if text.isdigit():
        minutes = int(text)
    else:
        m = _DURATION_RE.match(text)
        if not m or not any(m.groups()):
            return None
        w = int(m.group(1) or 0)
        d = int(m.group(2) or 0)
        h = int(m.group(3) or 0)
        mins = int(m.group(4) or 0)
        minutes = w * 7 * 24 * 60 + d * 24 * 60 + h * 60 + mins
    if 1 <= minutes <= 10080:
        return minutes
    return None


def build_roulette_embed(
    roulette: RouletteRecord,
    candidates: list[RouletteCandidateRecord],
) -> discord.Embed:
    """ルーレット公開用の Embed を生成する。"""
    if roulette.status == "open":
        status_label = "募集中"
        color = discord.Color.green()
    elif roulette.status == "waiting_start":
        status_label = "募集終了（開始待ち）"
        color = discord.Color.gold()
    else:
        status_label = "募集終了"
        color = discord.Color.dark_grey()

    display_title = roulette.title or f"ルーレット #{roulette.id}"
    embed = discord.Embed(
        title=f"{display_title}（{status_label}）",
        description=roulette.description or "候補を追加してルーレットに参加しよう(提案)",
        color=color,
    )
    embed.add_field(name="オーナー", value=f"<@{roulette.owner_id}>", inline=True)
    embed.add_field(
        name="1人あたり上限", value=f"{roulette.max_per_user}個", inline=True
    )
    embed.add_field(
        name="文字数上限", value=f"{roulette.max_length}文字", inline=True
    )
    embed.add_field(
        name="締切",
        value=f"<t:{roulette.deadline_ts}:F> (<t:{roulette.deadline_ts}:R>)",
        inline=False,
    )
    if candidates:
        preview_lines = [
            f"`{i}.` {c.text} (<@{c.user_id}>)"
            for i, c in enumerate(candidates[:20], start=1)
        ]
        if len(candidates) > 20:
            preview_lines.append(f"...ほか {len(candidates) - 20} 件")
        cand_text = "\n".join(preview_lines)
        if len(cand_text) > 1000:
            cand_text = cand_text[:997] + "..."
    else:
        cand_text = "（まだ候補が）ないです"
    embed.add_field(
        name=f"現在の候補（{len(candidates)}件）",
        value=cand_text,
        inline=False,
    )
    return embed


def build_roulette_public_view(roulette_id: int, *, is_open: bool) -> discord.ui.View:
    """公開メッセージに付与する永続ボタン View（custom_id ベース）。"""
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            label="候補追加",
            emoji="➕",
            style=discord.ButtonStyle.primary,
            custom_id=f"roulette:add:{roulette_id}",
            disabled=not is_open,
        )
    )
    view.add_item(
        discord.ui.Button(
            label="管理",
            emoji="⚙️",
            style=discord.ButtonStyle.secondary,
            custom_id=f"roulette:manage:{roulette_id}",
        )
    )
    return view


def build_export_file(
    roulette: RouletteRecord,
    all_candidates: list[RouletteCandidateRecord],
    winner: RouletteCandidateRecord,
    mode_name: str,
    elimination_log: list[str] | None = None,
) -> discord.File:
    """候補一覧と選抜結果をテキストファイルとしてエクスポートする。"""
    lines = [
        f"=== ルーレット #{roulette.id} 結果 ===",
        f"方式: {mode_name}",
        f"オーナーID: {roulette.owner_id}",
        f"当選候補: {winner.text} (追加者ID: {winner.user_id})",
        "",
        f"--- 全候補一覧 ({len(all_candidates)}件) ---",
    ]
    for idx, c in enumerate(all_candidates, start=1):
        lines.append(f"{idx}. {c.text} (追加者ID: {c.user_id})")
    if elimination_log:
        lines.append("")
        lines.append("--- 脱落ログ ---")
        lines.extend(elimination_log)
    data = "\n".join(lines).encode("utf-8")
    return discord.File(io.BytesIO(data), filename=f"roulette_{roulette.id}_result.txt")


class RouletteCreateModal(discord.ui.Modal, title="ルーレット開設設定"):
    """ルーレットの設定を入力するモーダル。"""

    title_input: discord.ui.TextInput = discord.ui.TextInput(
        label="ルーレット名（必須）",
        placeholder="○○のサブ垢名前決めルーレット",
        required=True,
        max_length=50,
    )
    max_per_user_input: discord.ui.TextInput = discord.ui.TextInput(
        label="一人当たりの追加可能数（1〜10個・必須）",
        placeholder="2とか5とか",
        required=True,
        max_length=2,
    )
    max_length_input: discord.ui.TextInput = discord.ui.TextInput(
        label="文字数制限（1〜50文字・必須）",
        placeholder="20とか30とか",
        required=True,
        max_length=2,
    )
    duration_input: discord.ui.TextInput = discord.ui.TextInput(
        label="時間制限（1分〜1週間・必須）",
        placeholder="10m, 1h, 2d, 1w, または分数(60)",
        required=True,
        max_length=20,
    )
    description_input: discord.ui.TextInput = discord.ui.TextInput(
        label="メッセージ（任意）",
        placeholder="候補いっぱいいっぱい裕次郎",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
    )

    def __init__(self, cog: RouletteCog) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            max_per_user = int(self.max_per_user_input.value.strip())
            max_length = int(self.max_length_input.value.strip())
        except ValueError:
            await interaction.response.send_message(
                "追加可能数と文字数制限は整数で入力してホスィ...", ephemeral=True
            )
            return

        if not (1 <= max_per_user <= 10):
            await interaction.response.send_message(
                "一人当たりの追加可能数は 1〜10 の範囲で指定してくれよな～頼むよ～", ephemeral=True
            )
            return
        if not (1 <= max_length <= 50):
            await interaction.response.send_message(
                "文字数制限は 1〜50 の範囲で指定してホスィ...", ephemeral=True
            )
            return

        minutes = parse_duration_minutes(self.duration_input.value)
        if minutes is None:
            await interaction.response.send_message(
                "時間制限は 1分〜1週間 の範囲で指定してホスィ...（例: `10m`, `1h`, `2d`, `1w`, `60`）。",
                ephemeral=True,
            )
            return

        title = self.title_input.value.strip()
        if not title:
            await interaction.response.send_message(
                "ルーレット名を入力してくれよな～頼むよ～", ephemeral=True
            )
            return

        description = self.description_input.value.strip()
        view = RoulettePublishConfirmView(
            self.cog,
            owner_id=interaction.user.id,
            title=title,
            max_per_user=max_per_user,
            max_length=max_length,
            minutes=minutes,
            description=description,
        )
        embed = discord.Embed(
            title=f"🎰 {title}（プレビュー）",
            description=description or "（メッセージなし）",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="1人あたり上限", value=f"{max_per_user}個", inline=True)
        embed.add_field(name="文字数上限", value=f"{max_length}文字", inline=True)
        embed.add_field(name="時間制限", value=f"{minutes}分", inline=True)
        await interaction.response.send_message(
            content="内容を確認して「完了（公開）」ボタンを押すとチャンネルに投稿されるゾ！グッドラック！",
            embed=embed,
            view=view,
            ephemeral=True,
        )


class RoulettePublishConfirmView(discord.ui.View):
    """設定完了・公開ボタン View。"""

    def __init__(
        self,
        cog: RouletteCog,
        *,
        owner_id: int,
        title: str,
        max_per_user: int,
        max_length: int,
        minutes: int,
        description: str,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.owner_id = owner_id
        self.title = title
        self.max_per_user = max_per_user
        self.max_length = max_length
        self.minutes = minutes
        self.description = description

    @discord.ui.button(label="完了（公開）", emoji="✅", style=discord.ButtonStyle.success)
    async def publish_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message(
                "サーバー内のチャンネルでだけ利用できるからね。そして天は鳴き、大地は震えるだろうね", ephemeral=True
            )
            return

        deadline_ts = int(time.time()) + self.minutes * 60
        roulette = self.cog.bot.storage.create_roulette(
            guild_id=interaction.guild.id,
            channel_id=interaction.channel.id,
            owner_id=self.owner_id,
            title=self.title,
            max_per_user=self.max_per_user,
            max_length=self.max_length,
            deadline_ts=deadline_ts,
            description=self.description,
        )
        embed = build_roulette_embed(roulette, [])
        view = build_roulette_public_view(roulette.id, is_open=True)
        msg = await interaction.channel.send(embed=embed, view=view)
        self.cog.bot.storage.set_roulette_message_id(roulette.id, msg.id)

        await interaction.response.edit_message(
            content=f"ルーレット #{roulette.id} を公開しました！",
            embed=None,
            view=None,
        )


class RouletteAddCandidateModal(discord.ui.Modal, title="ルーレット候補を追加"):
    """ルーレット候補を入力するモーダル。"""

    candidate_input: discord.ui.TextInput = discord.ui.TextInput(
        label="候補テキスト",
        placeholder="追加したい候補を入力",
        required=True,
        max_length=50,
    )

    def __init__(self, cog: RouletteCog, roulette_id: int) -> None:
        super().__init__()
        self.cog = cog
        self.roulette_id = roulette_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        roulette = self.cog.bot.storage.get_roulette(self.roulette_id)
        if roulette is None or roulette.status != "open":
            await interaction.response.send_message(
                "このルーレットはすでに終了または閉鎖されているゾ。間に合わなかっ...たぁ！", ephemeral=True
            )
            return

        text = self.candidate_input.value.strip()
        if not text:
            await interaction.response.send_message(
                "空の候補は追加できないゾ", ephemeral=True
            )
            return
        if len(text) > roulette.max_length:
            await interaction.response.send_message(
                f"文字数上限（{roulette.max_length}文字）を超えてるゾ（現在 {len(text)}文字）",
                ephemeral=True,
            )
            return

        user_cands = self.cog.bot.storage.list_roulette_candidates(
            roulette.id, user_id=interaction.user.id
        )
        if len(user_cands) >= roulette.max_per_user:
            await interaction.response.send_message(
                f"1人あたりの追加上限（{roulette.max_per_user}個）に達しちゃっ...たぁ！",
                ephemeral=True,
            )
            return

        self.cog.bot.storage.add_roulette_candidate(
            roulette.id, interaction.user.id, text
        )
        await self.cog.refresh_roulette_message(roulette.id)
        await interaction.response.send_message(
            f"候補「**{text}**」を追加したゾ", ephemeral=True
        )


class RouletteSelectToAddView(discord.ui.View):
    """コマンド経由で候補を追加するルーレットを選ぶ View。"""

    def __init__(self, cog: RouletteCog, roulettes: list[RouletteRecord]) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        options = [
            discord.SelectOption(
                label=(r.title or f"ルーレット #{r.id}")[:80],
                description=f"上限:{r.max_per_user}個 / {r.max_length}文字",
                value=str(r.id),
            )
            for r in roulettes[:25]
        ]
        select = discord.ui.Select(
            placeholder="候補を追加するルーレットを選択",
            options=options,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        roulette_id = int(interaction.data["values"][0])  # type: ignore[index]
        await interaction.response.send_modal(
            RouletteAddCandidateModal(self.cog, roulette_id)
        )


class RouletteEntryView(discord.ui.View):
    """/roulette コマンド実行時の最初の2択メニュー。"""

    def __init__(self, cog: RouletteCog, guild_id: int) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id

    @discord.ui.button(label="ルーレット開設", emoji="🎰", style=discord.ButtonStyle.primary)
    async def open_roulette(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(RouletteCreateModal(self.cog))

    @discord.ui.button(label="候補追加", emoji="➕", style=discord.ButtonStyle.secondary)
    async def add_candidate(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        roulettes = self.cog.bot.storage.list_roulettes_by_guild(
            self.guild_id, status="open"
        )
        if not roulettes:
            await interaction.response.send_message(
                "現在募集中のルーレットがないゾ", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "候補を追加するルーレット入れてホ",
            view=RouletteSelectToAddView(self.cog, roulettes),
            ephemeral=True,
        )


class RouletteManageView(discord.ui.View):
    """ルーレット管理パネル（オーナー/管理者は全操作、非オーナーは自分の候補削除のみ）。"""

    def __init__(
        self,
        cog: RouletteCog,
        roulette: RouletteRecord,
        candidates: list[RouletteCandidateRecord],
        *,
        is_privileged: bool,
    ) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.roulette_id = roulette.id
        self.is_privileged = is_privileged

        if candidates:
            options = [
                discord.SelectOption(
                    label=f"#{c.id}: {c.text[:70]}",
                    description=f"追加者ID: {c.user_id}",
                    value=str(c.id),
                )
                for c in candidates[:25]
            ]
            select = discord.ui.Select(
                placeholder="削除する候補を選択",
                options=options,
                row=0,
            )
            select.callback = self._on_delete_candidate
            self.add_item(select)

        if is_privileged:
            start_btn = discord.ui.Button(
                label="ルーレット開始",
                emoji="🎲",
                style=discord.ButtonStyle.success,
                row=1,
            )
            start_btn.callback = self._on_start
            self.add_item(start_btn)

            close_btn = discord.ui.Button(
                label="ルーレット閉鎖・削除",
                emoji="🗑️",
                style=discord.ButtonStyle.danger,
                row=1,
            )
            close_btn.callback = self._on_close
            self.add_item(close_btn)

    async def _on_delete_candidate(self, interaction: discord.Interaction) -> None:
        cand_id = int(interaction.data["values"][0])  # type: ignore[index]
        self.cog.bot.storage.delete_roulette_candidate(cand_id)
        await self.cog.refresh_roulette_message(self.roulette_id)
        await interaction.response.edit_message(
            content=f"候補 #{cand_id} を削除したゾ",
            view=None,
        )

    async def _on_start(self, interaction: discord.Interaction) -> None:
        await self.cog.prompt_start_mode(interaction, self.roulette_id)

    async def _on_close(self, interaction: discord.Interaction) -> None:
        await self.cog.close_and_delete_roulette(self.roulette_id)
        await interaction.response.edit_message(
            content=f"ルーレット #{self.roulette_id} を閉鎖・削除したゾ",
            view=None,
        )


class RouletteExportView(discord.ui.View):
    """結果 Embed に付与し、ボタン押下でテキストエクスポートを送信する View。"""

    def __init__(
        self,
        roulette: RouletteRecord,
        all_candidates: list[RouletteCandidateRecord],
        winner: RouletteCandidateRecord,
        mode_name: str,
        elimination_log: list[str] | None = None,
    ) -> None:
        super().__init__(timeout=600)
        self.roulette = roulette
        self.all_candidates = all_candidates
        self.winner = winner
        self.mode_name = mode_name
        self.elimination_log = elimination_log

    @discord.ui.button(
        label="結果をエクスポート", emoji="📄", style=discord.ButtonStyle.secondary
    )
    async def export_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        export_file = build_export_file(
            self.roulette,
            self.all_candidates,
            self.winner,
            self.mode_name,
            self.elimination_log,
        )
        await interaction.response.send_message(file=export_file, ephemeral=True)


class RouletteModeSelectView(discord.ui.View):
    """「一発選抜」か「脱落選抜」かを選ぶ View。"""

    def __init__(self, cog: RouletteCog, roulette_id: int) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.roulette_id = roulette_id

    @discord.ui.button(label="一発選抜", emoji="🎯", style=discord.ButtonStyle.primary)
    async def one_shot(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        roulette = self.cog.bot.storage.get_roulette(self.roulette_id)
        if roulette is None:
            await interaction.response.edit_message(
                content="ルーレットが見つからないゾ。ジャッ！キー！チェン！ジャッ！キー！チェン！", view=None
            )
            return
        candidates = self.cog.bot.storage.list_roulette_candidates(self.roulette_id)
        if not candidates:
            await interaction.response.edit_message(
                content="候補が0件のため選抜できません。 う、羽毛…", view=None
            )
            return

        winner = random.choice(candidates)
        embed = discord.Embed(
            title=f"🎉 ルーレット #{roulette.id} 結果",
            description=f"選ばれたのは… **{winner.text}** (<@{winner.user_id}>) です！",
            color=discord.Color.gold(),
        )
        export_view = RouletteExportView(roulette, candidates, winner, "一発選抜")
        channel = self.cog.bot.get_channel(roulette.channel_id)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            await channel.send(embed=embed, view=export_view)
        await self.cog.close_and_delete_roulette(roulette.id)
        await interaction.response.edit_message(
            content="ルーレット終わり！閉廷！", view=None
        )

    @discord.ui.button(label="脱落選抜", emoji="🔥", style=discord.ButtonStyle.danger)
    async def elimination(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(
            content="1回の続行で脱落させる数（最大5個）を選択してください。オッスお願いしまーす",
            view=RouletteEliminationCountView(self.cog, self.roulette_id),
        )


class RouletteEliminationCountView(discord.ui.View):
    """脱落選抜の1回あたり脱落数（1〜5個）を選択する View。"""

    def __init__(self, cog: RouletteCog, roulette_id: int) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.roulette_id = roulette_id
        options = [
            discord.SelectOption(label=f"{i}個ずつ脱落", value=str(i))
            for i in range(1, 6)
        ]
        select = discord.ui.Select(placeholder="脱落数を選択（1〜5個）", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        step_count = int(interaction.data["values"][0])  # type: ignore[index]
        roulette = self.cog.bot.storage.get_roulette(self.roulette_id)
        if roulette is None:
            await interaction.response.edit_message(
                content="ルーレットが見つからないゾ。ジャッ！キー！チェン！ジャッ！キー！チェン！", view=None
            )
            return
        candidates = self.cog.bot.storage.list_roulette_candidates(self.roulette_id)
        if len(candidates) < 2:
            await interaction.response.edit_message(
                content="脱落選抜には最低2件の候補が必要です。お兄さん許して～", view=None
            )
            return

        view = RouletteEliminationStepView(
            self.cog, roulette, candidates, step_count=step_count
        )
        channel = self.cog.bot.get_channel(roulette.channel_id)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            await channel.send(embed=view.build_status_embed(), view=view)
        await interaction.response.edit_message(
            content=f"脱落選抜（{step_count}個ずつ）を開始しました！チャンネルの「続行」ボタンで進めてください。",
            view=None,
        )


class RouletteEliminationStepView(discord.ui.View):
    """脱落選抜を「続行」ボタンで進める公開 View。"""

    def __init__(
        self,
        cog: RouletteCog,
        roulette: RouletteRecord,
        candidates: list[RouletteCandidateRecord],
        *,
        step_count: int,
    ) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.roulette = roulette
        self.all_candidates = list(candidates)
        self.remaining = list(candidates)
        self.step_count = step_count
        self.elimination_log: list[str] = []

    def build_status_embed(self, latest_eliminated: list[RouletteCandidateRecord] | None = None) -> discord.Embed:
        embed = discord.Embed(
            title=f"🔥 ルーレット #{self.roulette.id} 脱落選抜中",
            description=f"残り候補: **{len(self.remaining)}件**（1回あたり最大 {self.step_count}個 脱落）",
            color=discord.Color.orange(),
        )
        if latest_eliminated:
            elim_text = "\n".join(
                f"💀 {c.text} (<@{c.user_id}>)" for c in latest_eliminated
            )
            if len(elim_text) > 1000:
                elim_text = elim_text[:997] + "..."
            embed.add_field(name="今回の脱落者", value=elim_text, inline=False)

        rem_lines = [
            f"`{i}.` {c.text} (<@{c.user_id}>)"
            for i, c in enumerate(self.remaining, start=1)
        ]
        rem_text = "\n".join(rem_lines)
        if len(self.remaining) > 20 or len(rem_text) > 950:
            short_lines = rem_lines[:15]
            short_lines.append(f"候補に溺れる！溺れる！他にあと {len(self.remaining) - 15} 件")
            rem_text = "\n".join(short_lines)
            if len(rem_text) > 1000:
                rem_text = rem_text[:997] + "..."
        embed.add_field(name="残り候補", value=rem_text or "なし", inline=False)
        return embed

    @discord.ui.button(label="残り候補一覧", emoji="📋", style=discord.ButtonStyle.secondary)
    async def show_remaining(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        lines = [
            f"{i}. {c.text} (<@{c.user_id}>)"
            for i, c in enumerate(self.remaining, start=1)
        ]
        text = "\n".join(lines)
        if len(text) <= 1900:
            await interaction.response.send_message(
                f"**残り候補一覧（{len(self.remaining)}件）**\n{text}",
                ephemeral=True,
            )
        else:
            file = discord.File(
                io.BytesIO(text.encode("utf-8")),
                filename=f"roulette_{self.roulette.id}_remaining.txt",
            )
            await interaction.response.send_message(
                content=f"残り候補一覧（{len(self.remaining)}件）をファイルで送信します。",
                file=file,
                ephemeral=True,
            )

    @discord.ui.button(label="続行", emoji="▶️", style=discord.ButtonStyle.danger)
    async def next_step(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        is_admin = (
            interaction.guild is not None
            and can_manage_bot(interaction.user, self.cog.bot.storage, interaction.guild.id)
        )
        if interaction.user.id != self.roulette.owner_id and not is_admin:
            await interaction.response.send_message(
                "続行ボタンはルーレットオーナーまたは管理者のみ押せます。大会近いからね、しょうがないね(意味不明)",
                ephemeral=True,
            )
            return

        if len(self.remaining) <= 1:
            return

        num_to_elim = min(self.step_count, len(self.remaining) - 1)
        eliminated = random.sample(self.remaining, num_to_elim)
        elim_ids = {c.id for c in eliminated}
        self.remaining = [c for c in self.remaining if c.id not in elim_ids]
        for c in eliminated:
            self.elimination_log.append(f"脱落: {c.text} (追加者ID: {c.user_id})")

        # 旧メッセージのボタンを無効化・削除し、新規メッセージとして送信
        await interaction.response.edit_message(view=None)

        if len(self.remaining) == 1:
            winner = self.remaining[0]
            embed = discord.Embed(
                title=f"ルーレット #{self.roulette.id} 結果",
                description=f"**{winner.text}** (<@{winner.user_id}>)に決まったゾ。たまげたなあ",
                color=discord.Color.gold(),
            )
            export_view = RouletteExportView(
                self.roulette,
                self.all_candidates,
                winner,
                f"脱落選抜（{self.step_count}個ずつ）",
                self.elimination_log,
            )
            await self.cog.close_and_delete_roulette(self.roulette.id)
            await interaction.followup.send(embed=embed, view=export_view)
            self.stop()
        else:
            await interaction.followup.send(
                embed=self.build_status_embed(eliminated), view=self
            )


class RouletteAdminSelectView(discord.ui.View):
    """管理者用：全ルーレットから管理対象を選択する View。"""

    def __init__(self, cog: RouletteCog, roulettes: list[RouletteRecord]) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        options = [
            discord.SelectOption(
                label=(r.title or f"ルーレット #{r.id}")[:80],
                description=f"状態: {r.status} / オーナーID: {r.owner_id}",
                value=str(r.id),
            )
            for r in roulettes[:25]
        ]
        select = discord.ui.Select(placeholder="管理するルーレットを選択", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        roulette_id = int(interaction.data["values"][0])  # type: ignore[index]
        roulette = self.cog.bot.storage.get_roulette(roulette_id)
        if roulette is None:
            await interaction.response.edit_message(
                content="ルーレットが見つかりません。", view=None
            )
            return
        candidates = self.cog.bot.storage.list_roulette_candidates(roulette_id)
        view = RouletteManageView(self.cog, roulette, candidates, is_privileged=True)
        await interaction.response.edit_message(
            content=f"【管理者モード】ルーレット #{roulette.id} の管理パネル",
            view=view,
        )


class RouletteCog(commands.Cog):
    """ルーレット機能を提供する Cog。"""

    def __init__(self, bot: YajuBot) -> None:
        self.bot = bot
        self._deadline_watcher.start()

    def cog_unload(self) -> None:
        self._deadline_watcher.cancel()

    async def refresh_roulette_message(self, roulette_id: int) -> None:
        roulette = self.bot.storage.get_roulette(roulette_id)
        if roulette is None or roulette.message_id == 0:
            return
        channel = self.bot.get_channel(roulette.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        candidates = self.bot.storage.list_roulette_candidates(roulette_id)
        try:
            msg = await channel.fetch_message(roulette.message_id)
            await msg.edit(
                embed=build_roulette_embed(roulette, candidates),
                view=build_roulette_public_view(
                    roulette.id, is_open=(roulette.status == "open")
                ),
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    async def close_and_delete_roulette(self, roulette_id: int) -> None:
        roulette = self.bot.storage.get_roulette(roulette_id)
        if roulette is not None and roulette.message_id != 0:
            channel = self.bot.get_channel(roulette.channel_id)
            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                candidates = self.bot.storage.list_roulette_candidates(roulette_id)
                closed_roulette = RouletteRecord(
                    id=roulette.id,
                    guild_id=roulette.guild_id,
                    channel_id=roulette.channel_id,
                    message_id=roulette.message_id,
                    owner_id=roulette.owner_id,
                    title=roulette.title,
                    max_per_user=roulette.max_per_user,
                    max_length=roulette.max_length,
                    deadline_ts=roulette.deadline_ts,
                    description=roulette.description,
                    status="closed",
                )
                try:
                    msg = await channel.fetch_message(roulette.message_id)
                    await msg.edit(
                        embed=build_roulette_embed(closed_roulette, candidates),
                        view=None,
                    )
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
        self.bot.storage.delete_roulette(roulette_id)

    async def prompt_start_mode(
        self, interaction: discord.Interaction, roulette_id: int
    ) -> None:
        candidates = self.bot.storage.list_roulette_candidates(roulette_id)
        if not candidates:
            await interaction.response.send_message(
                "候補が0件のためルーレットを開始できません。", ephemeral=True
            )
            return
        view = RouletteModeSelectView(self, roulette_id)
        if interaction.response.is_done():
            await interaction.followup.send(
                "選抜方式を選択してください。", view=view, ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "選抜方式を選択してください。", view=view, ephemeral=True
            )

    @tasks.loop(seconds=15)
    async def _deadline_watcher(self) -> None:
        now_ts = int(time.time())
        expired = self.bot.storage.list_expired_open_roulettes(now_ts)
        for roulette in expired:
            self.bot.storage.set_roulette_status(roulette.id, "waiting_start")
            await self.refresh_roulette_message(roulette.id)
            channel = self.bot.get_channel(roulette.channel_id)
            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                view = discord.ui.View(timeout=None)
                view.add_item(
                    discord.ui.Button(
                        label="ルーレット開始",
                        emoji="🎲",
                        style=discord.ButtonStyle.success,
                        custom_id=f"roulette:start:{roulette.id}",
                    )
                )
                try:
                    await channel.send(
                        content=f"<@{roulette.owner_id}> ルーレット #{roulette.id} の募集時間が終了しました！開始ボタンを押してルーレットを回してください。",
                        view=view,
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass

    @_deadline_watcher.before_loop
    async def _before_deadline_watcher(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener("on_interaction")
    async def _handle_roulette_buttons(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        data = interaction.data or {}
        custom_id = str(data.get("custom_id", ""))
        if not custom_id.startswith("roulette:"):
            return

        parts = custom_id.split(":")
        if len(parts) != 3:
            return
        _, action, raw_id = parts
        try:
            roulette_id = int(raw_id)
        except ValueError:
            return

        roulette = self.bot.storage.get_roulette(roulette_id)
        if roulette is None:
            await interaction.response.send_message(
                "このルーレットはすでに終了または削除されています。", ephemeral=True
            )
            return

        if action == "add":
            if roulette.status != "open":
                await interaction.response.send_message(
                    "このルーレットの候補募集は終了しています。", ephemeral=True
                )
                return
            await interaction.response.send_modal(
                RouletteAddCandidateModal(self, roulette_id)
            )
        elif action == "manage":
            is_owner = interaction.user.id == roulette.owner_id
            if is_owner:
                candidates = self.bot.storage.list_roulette_candidates(roulette_id)
                view = RouletteManageView(
                    self, roulette, candidates, is_privileged=True
                )
                await interaction.response.send_message(
                    f"ルーレット #{roulette.id} の管理パネル（オーナー）",
                    view=view,
                    ephemeral=True,
                )
            else:
                own_candidates = self.bot.storage.list_roulette_candidates(
                    roulette_id, user_id=interaction.user.id
                )
                if not own_candidates:
                    await interaction.response.send_message(
                        "ホモの兄ちゃんが追加した候補はありません。", ephemeral=True
                    )
                    return
                view = RouletteManageView(
                    self, roulette, own_candidates, is_privileged=False
                )
                await interaction.response.send_message(
                    f"ルーレット #{roulette.id} のあなたの候補管理",
                    view=view,
                    ephemeral=True,
                )
        elif action == "start":
            if interaction.user.id != roulette.owner_id:
                await interaction.response.send_message(
                    "このボタンはルーレットオーナーのみ使用できます。", ephemeral=True
                )
                return
            await self.prompt_start_mode(interaction, roulette_id)

    @app_commands.command(
        name="roulette",
        description="ルーレットを開けて候補追加ペロペロ舐めち",
    )
    @app_commands.guild_only()
    async def roulette_cmd(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "サーバー内のチャンネルでのみ利用できます。", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "実行したい操作を選択してください。",
            view=RouletteEntryView(self, interaction.guild.id),
            ephemeral=True,
        )

    @app_commands.command(
        name="admin-roulette",
        description="サーバー内の全ルーレットを管理するゾ。多分管理者専用だと思うんですけど（名推理）",
    )
    @app_commands.guild_only()
    async def roulette_admin_cmd(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "サーバー内のチャンネルでのみ利用できます。", ephemeral=True
            )
            return
        if not can_manage_bot(interaction.user, self.bot.storage, interaction.guild.id):
            await interaction.response.send_message(
                "このコマンドは管理者または許可された管理ロールのみ実行できます。",
                ephemeral=True,
            )
            return
        roulettes = self.bot.storage.list_roulettes_by_guild(interaction.guild.id)
        if not roulettes:
            await interaction.response.send_message(
                "現在このサーバーにアクティブなルーレットはありません。",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "管理するルーレットを選択してください。",
            view=RouletteAdminSelectView(self, roulettes),
            ephemeral=True,
        )


async def setup(bot: YajuBot) -> None:
    await bot.add_cog(RouletteCog(bot))
