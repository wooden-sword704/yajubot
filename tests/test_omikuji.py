"""おみくじ機能（/omikuji, /omikuji_reset）と確率・1日1回制限の単体テスト。"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from yajubot.bot import YajuBot
from yajubot.cogs.omikuji import (
    OmikujiCog,
    draw_omikuji,
    get_today_jst_str,
    normalize_tier_probabilities,
)
from yajubot.config import BotConfig
from yajubot.omikuji_data import (
    OMIKUJI_CATEGORIES,
    OMIKUJI_RANDOM_CATEGORIES,
    OMIKUJI_TIERS,
    OmikujiRandomCategory,
    OmikujiTier,
)
from yajubot.storage import OWNER_USER_ID, ProxyStorage


class OmikujiTest(unittest.IsolatedAsyncioTestCase):
    def test_default_tiers_and_probability_scaling(self) -> None:
        self.assertEqual(len(OMIKUJI_TIERS), 6)
        self.assertAlmostEqual(sum(t.probability for t in OMIKUJI_TIERS), 100.0, places=2)
        self.assertEqual(OMIKUJI_CATEGORIES, ("今日の淫夢ファミリー",))
        self.assertEqual(
            [rc.name for rc in OMIKUJI_RANDOM_CATEGORIES],
            ["今日の語録", "今日の拓也県旅行"],
        )

        # デフォルト抽選ではティア連動1項目＋ティア非依存ランダム項目がすべて含まれる
        default_res = draw_omikuji()
        default_items = dict(default_res.items)
        self.assertEqual(
            list(default_items.keys()),
            ["今日の淫夢ファミリー", "今日の語録", "今日の拓也県旅行"],
        )
        self.assertIn(
            default_items["今日の淫夢ファミリー"],
            default_res.tier.categories["今日の淫夢ファミリー"],
        )

        # 合計が100でない場合は自動で合計100へスケーリングされる
        unscaled = (
            OmikujiTier(name="A", color=0x111111, probability=10.0, categories={"願望": ("a",)}),
            OmikujiTier(name="B", color=0x222222, probability=40.0, categories={"願望": ("b",)}),
        )
        scaled = normalize_tier_probabilities(unscaled)
        self.assertAlmostEqual(sum(scaled), 100.0)
        self.assertAlmostEqual(scaled[0], 20.0)
        self.assertAlmostEqual(scaled[1], 80.0)

        custom_tiers = (
            OmikujiTier(
                name="超吉",
                color=0xFFFFFF,
                probability=100.0,
                categories={"今日の淫夢ファミリー": ("レア1", "レア2")},
            ),
            OmikujiTier(
                name="はずれ",
                color=0x000000,
                probability=0.0,
                categories={"今日の淫夢ファミリー": ("なし",)},
            ),
            OmikujiTier(
                name="空ティア",
                color=0x111111,
                probability=999.0,
                categories={},
            ),
        )
        custom_random = (
            OmikujiRandomCategory(name="今日の語録", items=("語録1", "語録2")),
            OmikujiRandomCategory(name="空項目", items=()),
        )
        for _ in range(10):
            res = draw_omikuji(
                custom_tiers,
                categories=("今日の淫夢ファミリー",),
                random_categories=custom_random,
            )
            self.assertEqual(res.tier.name, "超吉")
            items_dict = dict(res.items)
            self.assertIn(items_dict["今日の淫夢ファミリー"], {"レア1", "レア2"})
            self.assertIn(items_dict["今日の語録"], {"語録1", "語録2"})
            self.assertNotIn("空項目", items_dict)

    def test_jst_date_conversion(self) -> None:
        # UTC 2026-11-17 15:30:00 -> JST 2026-11-18 00:30:00
        utc_dt = datetime(2026, 11, 17, 15, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(get_today_jst_str(utc_dt), "2026-11-18")

    async def test_omikuji_daily_limit_and_reset(self) -> None:
        bot = YajuBot(BotConfig(token="dummy-token"), storage=ProxyStorage(":memory:"))
        try:
            cog = OmikujiCog(bot)
            guild = MagicMock(spec=discord.Guild)
            guild.id = 1

            normal_user = SimpleNamespace(
                id=42,
                display_name="一般ユーザー",
                display_avatar=SimpleNamespace(url="https://example.com/u.png"),
                roles=[],
                guild_permissions=discord.Permissions.none(),
            )

            interaction = MagicMock(spec=discord.Interaction)
            interaction.guild = guild
            interaction.user = normal_user
            interaction.response = MagicMock()
            interaction.response.send_message = AsyncMock()

            # 1回目: おみくじを引ける
            with patch.dict(
                OmikujiCog.omikuji.callback.__globals__,
                {"get_today_jst_str": lambda: "2026-11-17"},
            ):
                await cog.omikuji.callback(cog, interaction)
                interaction.response.send_message.assert_awaited_once()
                embed = interaction.response.send_message.call_args.kwargs["embed"]
                self.assertEqual(embed.title, "一般ユーザーのおみくじ")
                self.assertEqual(
                    [f.name for f in embed.fields],
                    ["運勢", "今日の淫夢ファミリー", "今日の語録", "今日の拓也県旅行"],
                )

                # 2回目（同日）: 制限に引っかかる
                interaction.response.send_message.reset_mock()
                await cog.omikuji.callback(cog, interaction)
                interaction.response.send_message.assert_awaited_once()
                msg_text = interaction.response.send_message.call_args.args[0]
                self.assertIn("今日はもうおみくじを引いたゾ", msg_text)
                self.assertTrue(interaction.response.send_message.call_args.kwargs["ephemeral"])

            # 翌日（2026-11-18）になると再度引ける
            interaction.response.send_message.reset_mock()
            with patch.dict(
                OmikujiCog.omikuji.callback.__globals__,
                {"get_today_jst_str": lambda: "2026-11-18"},
            ):
                await cog.omikuji.callback(cog, interaction)
                interaction.response.send_message.assert_awaited_once()
                self.assertIn("embed", interaction.response.send_message.call_args.kwargs)

            # 一般ユーザーによるリセットは拒否される
            interaction.response.send_message.reset_mock()
            await cog.omikuji_reset.callback(cog, interaction, None)
            interaction.response.send_message.assert_awaited_once()
            self.assertIn("管理者または許可された管理ロールのみ", interaction.response.send_message.call_args.args[0])

            # 例外アカウントによるリセットは成功し、同日でも再び引けるようになる
            owner_interaction = MagicMock(spec=discord.Interaction)
            owner_interaction.guild = guild
            owner_interaction.user = SimpleNamespace(
                id=OWNER_USER_ID, roles=[], guild_permissions=discord.Permissions.none()
            )
            owner_interaction.response = MagicMock()
            owner_interaction.response.send_message = AsyncMock()

            await cog.omikuji_reset.callback(cog, owner_interaction, normal_user)
            owner_interaction.response.send_message.assert_awaited_once()
            self.assertIn("リセットしたゾ", owner_interaction.response.send_message.call_args.args[0])

            interaction.response.send_message.reset_mock()
            with patch.dict(
                OmikujiCog.omikuji.callback.__globals__,
                {"get_today_jst_str": lambda: "2026-11-18"},
            ):
                await cog.omikuji.callback(cog, interaction)
                interaction.response.send_message.assert_awaited_once()
                self.assertIn("embed", interaction.response.send_message.call_args.kwargs)
        finally:
            await bot.close()


if __name__ == "__main__":
    unittest.main()
