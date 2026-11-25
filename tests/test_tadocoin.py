"""タドコイン通貨・スロット・おみくじ/神経衰弱報酬・リーダーボード・管理設定の単体テスト。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from yajubot.bot import YajuBot
from yajubot.cogs.admin import (
    AdminPanelView,
    TadocoinExcludedChannelView,
    TadocoinOverrideModal,
    TadocoinOverrideView,
)
from yajubot.cogs.omikuji import get_omikuji_tadocoin_delta
from yajubot.cogs.shinkei import (
    DIFFICULTIES,
    PERFECT_CLEAR_REWARDS,
    ShinkeiGame,
    _apply_shinkei_clear_rewards,
    calculate_shinkei_reward,
)
from yajubot.cogs.tadocoin import (
    CHAT_REWARD_AMOUNT,
    SLOT_MIN_BET,
    LeaderboardView,
    TadocoinCog,
    _build_slot_frame,
    build_leaderboard_embed,
    evaluate_slot_spin,
    select_slot_symbols,
)
from yajubot.config import BotConfig
from yajubot.storage import OWNER_USER_ID, ProxyStorage


class TadocoinSystemTest(unittest.IsolatedAsyncioTestCase):
    def test_storage_balance_and_shinkei_records(self) -> None:
        storage = ProxyStorage(":memory:")
        try:
            self.assertEqual(storage.get_tadocoin_balance(1, 10), 0)
            self.assertEqual(storage.add_tadocoin(1, 10, 100), 100)
            self.assertEqual(storage.add_tadocoin(1, 10, -250), 0)  # 0未満にならない

            storage.set_tadocoin(1, 10, 500)
            ok, bal = storage.try_spend_tadocoin(1, 10, 200)
            self.assertTrue(ok)
            self.assertEqual(bal, 300)

            ok2, bal2 = storage.try_spend_tadocoin(1, 10, 400)
            self.assertFalse(ok2)
            self.assertEqual(bal2, 300)

            storage.set_tadocoin(1, 20, 800)
            lb = storage.get_tadocoin_leaderboard(1)
            self.assertEqual(lb, [(20, 800), (10, 300)])

            self.assertTrue(storage.record_shinkei_clear(1, "normal", 10, 62.5, 16))
            self.assertFalse(storage.record_shinkei_clear(1, "normal", 10, 70.0, 20))
            self.assertTrue(storage.record_shinkei_clear(1, "normal", 10, 55.0, 14))
            self.assertTrue(storage.record_shinkei_clear(1, "normal", 20, 58.0, 15))
            slb = storage.get_shinkei_leaderboard(1, "normal")
            self.assertEqual(slb[0][0], 10)
            self.assertAlmostEqual(slb[0][1], 55.0)
        finally:
            storage.close()

    def test_omikuji_and_shinkei_rewards(self) -> None:
        self.assertGreater(get_omikuji_tadocoin_delta("# お　お　あ　た　り"), 1000)
        self.assertLess(get_omikuji_tadocoin_delta("お　お　は　ず　れ"), 0)
        self.assertGreater(get_omikuji_tadocoin_delta("野獣先輩級"), 0)

        r_perfect, label_perfect = calculate_shinkei_reward("normal", 8)
        self.assertEqual(r_perfect, PERFECT_CLEAR_REWARDS["normal"][1])
        self.assertTrue(label_perfect)

        r_fast, _ = calculate_shinkei_reward("normal", 18)
        r_norm, _ = calculate_shinkei_reward("normal", 23)
        r_slow, _ = calculate_shinkei_reward("normal", 27)
        r_over, _ = calculate_shinkei_reward("normal", 28)
        self.assertEqual(r_fast, 240)
        self.assertEqual(r_norm, 160)
        self.assertEqual(r_slow, 80)
        self.assertEqual(r_over, 0)

        storage = ProxyStorage(":memory:")
        try:
            game = ShinkeiGame(
                guild_id=1,
                host_id=42,
                difficulty=DIFFICULTIES["normal"],
                players=[42],
                cards=[],
                moves=18,
            )
            note = _apply_shinkei_clear_rewards(storage, game, 60.0)
            self.assertIn("+240", note)
            self.assertEqual(storage.get_tadocoin_balance(1, 42), 240)
            self.assertEqual(len(storage.get_shinkei_leaderboard(1, "normal")), 1)

            # コインがもらえない手数では獲得表記が出ない
            game_over = ShinkeiGame(
                guild_id=1,
                host_id=42,
                difficulty=DIFFICULTIES["normal"],
                players=[42],
                cards=[],
                moves=28,
            )
            note_over = _apply_shinkei_clear_rewards(storage, game_over, 120.0)
            self.assertEqual(note_over, "")

            # オンラインプレイ時、敗者にも勝者の1/3が配布される
            mp_game = ShinkeiGame(
                guild_id=1,
                host_id=101,
                difficulty=DIFFICULTIES["normal"],
                players=[101, 102],
                cards=[],
                moves=18,
                scores={101: 6, 102: 2},
            )
            mp_note = _apply_shinkei_clear_rewards(storage, mp_game, 60.0)
            self.assertIn("+240", mp_note)
            self.assertIn("+80", mp_note)
            self.assertEqual(storage.get_tadocoin_balance(1, 101), 240)
            self.assertEqual(storage.get_tadocoin_balance(1, 102), 80)
        finally:
            storage.close()

    def test_slot_evaluation_and_symbols(self) -> None:
        guild = MagicMock(spec=discord.Guild)
        guild.id = 12345
        emojis = []
        for i in range(6):
            e = MagicMock(spec=discord.Emoji)
            e.id = i + 1
            e.available = True
            e.__str__ = lambda self, idx=i: f"<:e{idx}:{idx}>"
            emojis.append(e)
        guild.emojis = emojis

        symbols = select_slot_symbols(guild, day_key="2026-11-25")
        self.assertEqual(len(symbols), 5)
        # 同日中は全ユーザー固定（同じ結果になること）
        self.assertEqual(symbols, select_slot_symbols(guild, day_key="2026-11-25"))
        # 別の日付では日替わりになること・30日間で2日連続同じ特賞シンボルにならないこと
        symbols_next = select_slot_symbols(guild, day_key="2026-11-26")
        self.assertNotEqual(symbols[0], symbols_next[0])
        prev_special = None
        for day in range(1, 31):
            daily = select_slot_symbols(guild, day_key=f"2026-11-{day:02d}")
            self.assertNotEqual(daily[0], prev_special)
            prev_special = daily[0]

        frame = _build_slot_frame("🍒", "🍋", "🍇")
        self.assertEqual(frame, "🍒  │  🍋  │  🍇")
        self.assertNotIn("┏", frame)
        self.assertNotIn("┗", frame)

        jackpot = evaluate_slot_spin(symbols, 100, reels=(symbols[0], symbols[0], symbols[0]))
        self.assertTrue(jackpot.is_jackpot)
        self.assertEqual(jackpot.payout, 10000)

        big = evaluate_slot_spin(symbols, 100, reels=(symbols[1], symbols[1], symbols[1]))
        self.assertEqual(big.payout, 300)

        small = evaluate_slot_spin(symbols, 100, reels=(symbols[1], symbols[1], symbols[2]))
        self.assertEqual(small.payout, 110)

        loss = evaluate_slot_spin(symbols, 100, reels=(symbols[0], symbols[1], symbols[2]))
        self.assertEqual(loss.payout, 0)

        # 設定確率 (1% / 9% / 40% / 50%) 付近に収束することを確認
        spins = [evaluate_slot_spin(symbols, 100).payout for _ in range(5000)]
        loss_rate = sum(1 for p in spins if p == 0) / 5000
        small_rate = sum(1 for p in spins if p == 110) / 5000
        self.assertGreater(loss_rate, 0.45)
        self.assertLess(loss_rate, 0.55)
        self.assertGreater(small_rate, 0.35)
        self.assertLess(small_rate, 0.45)

    async def test_chat_reward_slot_command_and_leaderboard(self) -> None:
        bot = YajuBot(BotConfig(token="dummy"), storage=ProxyStorage(":memory:"))
        try:
            cog = TadocoinCog(bot)
            guild = MagicMock(spec=discord.Guild)
            guild.id = 1
            guild.emojis = []

            bot.storage.set_tadocoin_excluded_channels(1, [999])

            # 例外チャンネルでは増えない
            excluded_msg = MagicMock(spec=discord.Message)
            excluded_msg.author = SimpleNamespace(id=10, bot=False)
            excluded_msg.guild = guild
            excluded_msg.channel = SimpleNamespace(id=999, parent_id=None)
            await cog.on_message(excluded_msg)
            self.assertEqual(bot.storage.get_tadocoin_balance(1, 10), 0)

            # 通常チャンネルでは増える（クールダウン中は増えない）
            normal_msg = MagicMock(spec=discord.Message)
            normal_msg.author = SimpleNamespace(id=10, bot=False)
            normal_msg.guild = guild
            normal_msg.channel = SimpleNamespace(id=123, parent_id=None)
            await cog.on_message(normal_msg)
            await cog.on_message(normal_msg)
            self.assertEqual(bot.storage.get_tadocoin_balance(1, 10), CHAT_REWARD_AMOUNT)

            # スロット実行（所持数不足）
            inter_poor = MagicMock(spec=discord.Interaction)
            inter_poor.guild = guild
            inter_poor.user = SimpleNamespace(id=10, display_name="User10")
            inter_poor.response = MagicMock()
            inter_poor.response.send_message = AsyncMock()
            await cog.slot.callback(cog, inter_poor, 500)
            inter_poor.response.send_message.assert_awaited_once()
            self.assertIn("足りない", inter_poor.response.send_message.call_args.args[0])

            # 最低ベット額未満は拒否される
            inter_min = MagicMock(spec=discord.Interaction)
            inter_min.guild = guild
            inter_min.user = SimpleNamespace(id=10, display_name="User10")
            inter_min.response = MagicMock()
            inter_min.response.send_message = AsyncMock()
            await cog.slot.callback(cog, inter_min, 100)
            inter_min.response.send_message.assert_awaited_once()
            self.assertIn("最低ベット額", inter_min.response.send_message.call_args.args[0])

            # スロット実行（成功＋演出）
            bot.storage.set_tadocoin(1, 10, SLOT_MIN_BET * 2)
            inter_spin = MagicMock(spec=discord.Interaction)
            inter_spin.guild = guild
            inter_spin.user = SimpleNamespace(id=10, display_name="User10")
            inter_spin.response = MagicMock()
            inter_spin.response.send_message = AsyncMock()
            inter_spin.edit_original_response = AsyncMock()

            with patch("yajubot.cogs.tadocoin.asyncio.sleep", new=AsyncMock()):
                await cog.slot.callback(cog, inter_spin, SLOT_MIN_BET)
            inter_spin.response.send_message.assert_awaited_once()
            self.assertEqual(inter_spin.edit_original_response.await_count, 3)

            # 10秒以内の連続実行はクールダウンで拒否される
            inter_cd = MagicMock(spec=discord.Interaction)
            inter_cd.guild = guild
            inter_cd.user = SimpleNamespace(id=10, display_name="User10")
            inter_cd.response = MagicMock()
            inter_cd.response.send_message = AsyncMock()
            await cog.slot.callback(cog, inter_cd, SLOT_MIN_BET)
            inter_cd.response.send_message.assert_awaited_once()
            self.assertIn("クールダウン", inter_cd.response.send_message.call_args.args[0])
            self.assertTrue(inter_cd.response.send_message.call_args.kwargs.get("ephemeral"))

            # リーダーボード確認
            bot.storage.record_shinkei_clear(1, "normal", 10, 50.0, 12)
            lb_embed = build_leaderboard_embed(bot, guild, "tadocoin")
            self.assertIn("<@10>", lb_embed.description)
            sh_embed = build_leaderboard_embed(bot, guild, "shinkei_normal")
            self.assertIn("50.00秒", sh_embed.description)

            lb_view = LeaderboardView(bot, guild)
            self.assertEqual(len(lb_view.children), 1)
        finally:
            await bot.close()

    async def test_admin_tadocoin_settings(self) -> None:
        bot = YajuBot(BotConfig(token="dummy"), storage=ProxyStorage(":memory:"))
        try:
            owner = SimpleNamespace(
                id=OWNER_USER_ID, roles=[], guild_permissions=discord.Permissions.none()
            )
            panel = AdminPanelView(bot, 1)

            # 例外チャンネル設定へ遷移
            inter = MagicMock(spec=discord.Interaction)
            inter.user = owner
            inter.response = MagicMock()
            inter.response.edit_message = AsyncMock()
            sel = MagicMock(spec=discord.ui.Select)
            sel.values = ["tadocoin_excluded"]
            await AdminPanelView.menu_select(panel, inter, sel)
            excl_view = inter.response.edit_message.call_args.kwargs["view"]
            self.assertIsInstance(excl_view, TadocoinExcludedChannelView)

            # チャンネル選択反映
            ch_sel = MagicMock(spec=discord.ui.ChannelSelect)
            ch_sel.values = [SimpleNamespace(id=777)]
            inter_ch = MagicMock(spec=discord.Interaction)
            inter_ch.user = owner
            inter_ch.response = MagicMock()
            inter_ch.response.edit_message = AsyncMock()
            await TadocoinExcludedChannelView.excluded_channel_select(
                excl_view, inter_ch, ch_sel
            )
            self.assertEqual(
                bot.storage.get_guild_settings(1).tadocoin_excluded_channel_ids,
                (777,),
            )

            # 上書き設定画面へ遷移 & モーダル送信
            inter_ov = MagicMock(spec=discord.Interaction)
            inter_ov.user = owner
            inter_ov.response = MagicMock()
            inter_ov.response.edit_message = AsyncMock()
            sel.values = ["tadocoin_override"]
            await AdminPanelView.menu_select(panel, inter_ov, sel)
            ov_view = inter_ov.response.edit_message.call_args.kwargs["view"]
            self.assertIsInstance(ov_view, TadocoinOverrideView)

            modal = TadocoinOverrideModal(
                bot, 1, SimpleNamespace(id=555), ov_view
            )
            modal.amount_input._value = "12345"
            inter_modal = MagicMock(spec=discord.Interaction)
            inter_modal.user = owner
            inter_modal.response = MagicMock()
            inter_modal.response.edit_message = AsyncMock()
            await modal.on_submit(inter_modal)
            self.assertEqual(bot.storage.get_tadocoin_balance(1, 555), 12345)
        finally:
            await bot.close()


if __name__ == "__main__":
    unittest.main()
