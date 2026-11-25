"""「MURはんがきめる」（/mur）の単体テスト。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from yajubot.bot import YajuBot
from yajubot.cogs.mur import (
    MurCog,
    build_mur_message,
    judge_mur_topic,
)
from yajubot.cogs.proxy import REPLY1_EMOJI, REPLY2_EMOJI
from yajubot.config import BotConfig
from yajubot.storage import ProxyStorage


class MurJudgeTest(unittest.IsolatedAsyncioTestCase):
    def test_deterministic_judgment_and_simhash_clustering(self) -> None:
        d1 = judge_mur_topic("今日の晩飯ラーメンにする？", guild_id=1, day_key="2026-11-25")
        d2 = judge_mur_topic("今日の晩飯ラーメンにする？", guild_id=1, day_key="2026-11-25")
        self.assertEqual(d1, d2)
        self.assertIn(d1.level, (1, 2, 3, 4, 5))

        # 似た議題は評価の分散が抑えられる（最大差 1 以内）
        v1 = judge_mur_topic("野獣邸にサウナを作るべきか?", guild_id=1, day_key="2026-11-25")
        v2 = judge_mur_topic("野獣邸にサウナを作るべきですか？", guild_id=1, day_key="2026-11-25")
        v3 = judge_mur_topic("野獣邸にサウナを作るのってどうなの？", guild_id=1, day_key="2026-11-25")
        self.assertEqual(v1.level, v2.level)
        self.assertLessEqual(max(v1.level, v2.level, v3.level) - min(v1.level, v2.level, v3.level), 1)

    async def test_mur_command_sends_webhook_with_reply_header(self) -> None:
        bot = YajuBot(BotConfig(token="dummy"), storage=ProxyStorage(":memory:"))
        try:
            cog = MurCog(bot)
            ch = MagicMock(spec=discord.TextChannel)
            wh = MagicMock(spec=discord.Webhook)
            wh.send = AsyncMock()

            guild = MagicMock(spec=discord.Guild)
            guild.id = 1
            inter = MagicMock(spec=discord.Interaction)
            inter.guild = guild
            inter.channel = ch
            inter.user = SimpleNamespace(
                id=42, mention="<@42>", display_name="User42"
            )
            inter.response = MagicMock()
            inter.response.defer = AsyncMock()
            inter.delete_original_response = AsyncMock()
            inter.followup = MagicMock()
            inter.followup.send = AsyncMock()

            with (
                patch.object(type(bot), "user", new=SimpleNamespace(id=999)),
                patch(
                    "yajubot.cogs.mur._get_or_create_mur_webhook",
                    AsyncMock(return_value=wh),
                ),
            ):
                await cog.mur_decide.callback(cog, inter, "明日の会議なくす？")

            wh.send.assert_awaited_once()
            sent_kwargs = wh.send.call_args.kwargs
            self.assertTrue(
                sent_kwargs["content"].startswith(f"{REPLY1_EMOJI}{REPLY2_EMOJI}<@42> :明日の会議なくす？")
            )
            self.assertFalse(sent_kwargs["allowed_mentions"].users)
            inter.delete_original_response.assert_awaited_once()
        finally:
            await bot.close()


if __name__ == "__main__":
    unittest.main()
