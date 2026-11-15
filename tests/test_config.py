"""config モジュールの単体テスト。"""

from __future__ import annotations

import unittest

from yajubot.config import BotConfig, ConfigError, load_config


class LoadConfigTest(unittest.TestCase):
    def test_valid_minimal_config(self) -> None:
        cfg = load_config({"DISCORD_TOKEN": "dummy.token.value"})
        self.assertEqual(cfg, BotConfig(token="dummy.token.value", guild_id=None, log_level="INFO"))

    def test_valid_full_config_with_whitespace(self) -> None:
        cfg = load_config(
            {
                "DISCORD_TOKEN": "  secret.token  ",
                "DISCORD_GUILD_ID": " 123456789012345678 ",
                "LOG_LEVEL": "debug",
            }
        )
        self.assertEqual(cfg.token, "secret.token")
        self.assertEqual(cfg.guild_id, 123456789012345678)
        self.assertEqual(cfg.log_level, "DEBUG")

    def test_repr_hides_token(self) -> None:
        cfg = BotConfig(token="super-secret-token", guild_id=42, log_level="INFO")
        self.assertNotIn("super-secret-token", repr(cfg))

    def test_missing_or_placeholder_token_raises(self) -> None:
        for env in [
            {},
            {"DISCORD_TOKEN": ""},
            {"DISCORD_TOKEN": "   "},
            {"DISCORD_TOKEN": "your_discord_bot_token_here"},
        ]:
            with self.subTest(env=env):
                with self.assertRaises(ConfigError):
                    load_config(env)

    def test_blank_guild_id_becomes_none(self) -> None:
        cfg = load_config({"DISCORD_TOKEN": "abc", "DISCORD_GUILD_ID": "   "})
        self.assertIsNone(cfg.guild_id)

    def test_invalid_guild_id_raises(self) -> None:
        for bad_id in ["abc", "0", "-10"]:
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(ConfigError):
                    load_config({"DISCORD_TOKEN": "abc", "DISCORD_GUILD_ID": bad_id})

    def test_invalid_log_level_raises(self) -> None:
        with self.assertRaises(ConfigError):
            load_config({"DISCORD_TOKEN": "abc", "LOG_LEVEL": "VERBOSE"})


if __name__ == "__main__":
    unittest.main()
