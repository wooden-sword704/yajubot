"""暗号化ログ・プロキシ履歴・サーバーロール設定を永続化する SQLite ストレージ。"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sqlite3
import threading
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# サーバーロール設定に関わらず常に管理操作を許可する例外ユーザー ID (wooden_sword.704)
OWNER_USER_ID: int = 1108317459125121096

DEFAULT_DB_PATH = Path("data/yajubot.db")
DEFAULT_KEY_PATH = Path("data/.log_key")
DEFAULT_MAX_RECORDS = 1_000_000
_ZLIB_MAGIC_HEADER = b"\x00ZLIB\x00"

DEFAULT_DISBOARD_BOT_ID: int = 302050872383242240
DEFAULT_DISSOKU_BOT_ID: int = 761562078095867916
DEFAULT_BUMP_DELAY_MINUTES: int = 120
DEFAULT_UP_DELAY_MINUTES: int = 60
_UNSET: object = object()


JST = timezone(timedelta(hours=9))


def _now_jst_str() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")


def _normalize_fernet_key(raw_key: str) -> bytes:
    """任意の文字列または Fernet キー文字列を 32-byte urlsafe base64 キーに正規化する。"""
    key_bytes = raw_key.strip().encode("utf-8")
    try:
        # すでに有効な Fernet キーであればそのまま利用
        Fernet(key_bytes)
        return key_bytes
    except Exception:
        digest = hashlib.sha256(key_bytes).digest()
        return base64.urlsafe_b64encode(digest)


class LogCipher:
    """メッセージ本文ログを暗号化・復号するためのラッパー（Fernet / AES-128-CBC + HMAC）。"""

    def __init__(
        self,
        key: str | bytes | None = None,
        *,
        key_path: Path | str | None = DEFAULT_KEY_PATH,
    ) -> None:
        if key is not None:
            raw = key.decode("utf-8") if isinstance(key, bytes) else key
            fernet_key = _normalize_fernet_key(raw)
        else:
            env_key = os.environ.get("LOG_ENCRYPTION_KEY", "").strip()
            if env_key:
                fernet_key = _normalize_fernet_key(env_key)
            elif key_path is not None:
                path = Path(key_path)
                if path.exists():
                    fernet_key = _normalize_fernet_key(path.read_text(encoding="utf-8"))
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    fernet_key = Fernet.generate_key()
                    path.write_bytes(fernet_key)
                    try:
                        os.chmod(path, 0o600)
                    except OSError:
                        pass
            else:
                fernet_key = Fernet.generate_key()

        self._fernet = Fernet(fernet_key)

    def encrypt(self, plaintext: str) -> bytes:
        compressed = _ZLIB_MAGIC_HEADER + zlib.compress(
            plaintext.encode("utf-8"), level=6
        )
        return self._fernet.encrypt(compressed)

    def decrypt(self, token: bytes) -> str:
        try:
            raw = self._fernet.decrypt(token)
            if raw.startswith(_ZLIB_MAGIC_HEADER):
                raw = zlib.decompress(raw[len(_ZLIB_MAGIC_HEADER) :])
            return raw.decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, zlib.error):
            return "<復号失敗>"


@dataclass(frozen=True, slots=True)
class GuildSettings:
    """サーバー単位のプロキシ利用ロール・管理ロール・ログチャンネル設定。"""

    guild_id: int
    proxy_role_ids: tuple[int, ...] = ()
    admin_role_ids: tuple[int, ...] = ()
    log_channel_id: int | None = None
    binjou_channel_ids: tuple[int, ...] = ()
    binjou_probability: float = 5.0
    stage_mod_role_ids: tuple[int, ...] = ()
    self_assignable_role_ids: tuple[int, ...] = ()
    tadocoin_excluded_channel_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class RouletteRecord:
    """ルーレット本体レコード。"""

    id: int
    guild_id: int
    channel_id: int
    message_id: int
    owner_id: int
    title: str
    max_per_user: int
    max_length: int
    deadline_ts: int
    description: str
    status: str  # "open", "waiting_start"


@dataclass(frozen=True, slots=True)
class RouletteCandidateRecord:
    """ルーレット候補レコード。"""

    id: int
    roulette_id: int
    user_id: int
    text: str


@dataclass(frozen=True, slots=True)
class BumpNotifySettings:
    """サーバー単位の Bump/Up 通知設定。"""

    guild_id: int
    channel_id: int | None = None
    bump_enabled: bool = True
    up_enabled: bool = True
    bump_delay_minutes: int = DEFAULT_BUMP_DELAY_MINUTES
    up_delay_minutes: int = DEFAULT_UP_DELAY_MINUTES
    bump_bot_id: int = DEFAULT_DISBOARD_BOT_ID
    up_bot_id: int = DEFAULT_DISSOKU_BOT_ID


@dataclass(frozen=True, slots=True)
class BumpReminderRecord:
    """永続化された Bump/Up リマインダーレコード。"""

    guild_id: int
    command_type: str
    channel_id: int
    user_id: int
    notify_at: float
    created_at: float


@dataclass(frozen=True, slots=True)
class ProxyEditRecord:
    """復号済みのプロキシメッセージ編集履歴レコード。"""

    proxy_msg_id: int
    old_content: str
    new_content: str
    edited_at: str


@dataclass(frozen=True, slots=True)
class ProxyMessageRecord:
    """復号済みのプロキシ送信履歴レコード。"""

    proxy_msg_id: int
    guild_id: int
    channel_id: int
    author_id: int
    reply_target_id: int | None
    content: str
    created_at: str
    edited_at: str | None
    is_deleted: bool
    deleted_at: str | None


class ProxyStorage:
    """1GB RAM 環境向けに最適化された SQLite ベースの永続化ストア。"""

    def __init__(
        self,
        db_path: Path | str = DEFAULT_DB_PATH,
        *,
        cipher: LogCipher | None = None,
        max_records: int = DEFAULT_MAX_RECORDS,
        role_backup_path: Path | str | None = None,
    ) -> None:
        self._db_path_str = str(db_path)
        self._is_memory = self._db_path_str == ":memory:"
        if not self._is_memory:
            Path(self._db_path_str).parent.mkdir(parents=True, exist_ok=True)

        if role_backup_path is not None:
            self._role_backup_path: Path | None = Path(role_backup_path)
        elif not self._is_memory:
            self._role_backup_path = (
                Path(self._db_path_str).parent / "role_toggle_backup.json"
            )
        else:
            self._role_backup_path = None

        self._cipher = cipher or LogCipher(
            key_path=None if self._is_memory else DEFAULT_KEY_PATH
        )
        self._max_records = max_records
        self._write_ops_since_trim = 0
        self._guild_settings_cache: dict[int, GuildSettings] = {}
        self._bump_settings_cache: dict[int, BumpNotifySettings] = {}
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path_str, check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        self._auto_restore_role_backup_if_empty()

    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("PRAGMA foreign_keys = ON;")
            cur.execute("PRAGMA busy_timeout = 5000;")
            if not self._is_memory:
                cur.execute("PRAGMA journal_mode=WAL;")
                cur.execute("PRAGMA synchronous=NORMAL;")
            # メモリキャッシュ上限を約 2MB に制限
            cur.execute("PRAGMA cache_size=-2000;")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS proxy_messages (
                    proxy_msg_id INTEGER PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    author_id INTEGER NOT NULL,
                    reply_target_id INTEGER,
                    content_enc BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    edited_at TEXT,
                    is_deleted INTEGER NOT NULL DEFAULT 0,
                    deleted_at TEXT
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_proxy_reply_target
                ON proxy_messages(reply_target_id)
                WHERE reply_target_id IS NOT NULL;
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_proxy_deleted
                ON proxy_messages(guild_id, is_deleted, deleted_at DESC);
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_proxy_created_at
                ON proxy_messages(created_at ASC, proxy_msg_id ASC);
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    proxy_role_ids TEXT NOT NULL DEFAULT '[]',
                    admin_role_ids TEXT NOT NULL DEFAULT '[]',
                    log_channel_id INTEGER,
                    binjou_channel_ids TEXT NOT NULL DEFAULT '[]',
                    binjou_probability REAL NOT NULL DEFAULT 5.0,
                    stage_mod_role_ids TEXT NOT NULL DEFAULT '[]',
                    self_assignable_role_ids TEXT NOT NULL DEFAULT '[]',
                    tadocoin_excluded_channel_ids TEXT NOT NULL DEFAULT '[]'
                );
                """
            )
            # 既存 DB 向けマイグレーション: カラムがなければ追加
            cols = {
                r["name"]
                for r in cur.execute("PRAGMA table_info(guild_settings)").fetchall()
            }
            if "log_channel_id" not in cols:
                cur.execute("ALTER TABLE guild_settings ADD COLUMN log_channel_id INTEGER;")
            if "binjou_channel_ids" not in cols:
                cur.execute(
                    "ALTER TABLE guild_settings ADD COLUMN binjou_channel_ids TEXT NOT NULL DEFAULT '[]';"
                )
            if "binjou_probability" not in cols:
                cur.execute(
                    "ALTER TABLE guild_settings ADD COLUMN binjou_probability REAL NOT NULL DEFAULT 5.0;"
                )
            if "stage_mod_role_ids" not in cols:
                cur.execute(
                    "ALTER TABLE guild_settings ADD COLUMN stage_mod_role_ids TEXT NOT NULL DEFAULT '[]';"
                )
            if "self_assignable_role_ids" not in cols:
                cur.execute(
                    "ALTER TABLE guild_settings ADD COLUMN self_assignable_role_ids TEXT NOT NULL DEFAULT '[]';"
                )
            if "tadocoin_excluded_channel_ids" not in cols:
                cur.execute(
                    "ALTER TABLE guild_settings ADD COLUMN tadocoin_excluded_channel_ids TEXT NOT NULL DEFAULT '[]';"
                )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_toggleable_roles (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    role_id INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, user_id, role_id)
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tadocoin_balances (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    balance INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tadocoin_leaderboard
                ON tadocoin_balances(guild_id, balance DESC);
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS shinkei_records (
                    guild_id INTEGER NOT NULL,
                    difficulty TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    best_time_sec REAL NOT NULL,
                    moves INTEGER NOT NULL,
                    achieved_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, difficulty, user_id)
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_shinkei_best_time
                ON shinkei_records(guild_id, difficulty, best_time_sec ASC);
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sub_role_plans (
                    guild_id INTEGER NOT NULL,
                    role_id INTEGER NOT NULL,
                    weekly_cost INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, role_id)
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_role_subscriptions (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    role_id INTEGER NOT NULL,
                    next_billing_ts INTEGER NOT NULL,
                    notify_muted INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id, role_id)
                );
                """
            )
            sub_cols = {
                r["name"]
                for r in cur.execute(
                    "PRAGMA table_info(user_role_subscriptions)"
                ).fetchall()
            }
            if "notify_muted" not in sub_cols:
                cur.execute(
                    "ALTER TABLE user_role_subscriptions ADD COLUMN notify_muted INTEGER NOT NULL DEFAULT 0;"
                )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sub_due
                ON user_role_subscriptions(next_billing_ts);
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS proxy_edit_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proxy_msg_id INTEGER NOT NULL,
                    guild_id INTEGER NOT NULL,
                    old_content_enc BLOB NOT NULL,
                    new_content_enc BLOB NOT NULL,
                    edited_at TEXT NOT NULL
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_edit_history_msg
                ON proxy_edit_history(proxy_msg_id, id DESC);
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS omikuji_history (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    drawn_date TEXT NOT NULL,
                    result_name TEXT NOT NULL,
                    drawn_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS roulettes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL DEFAULT 0,
                    owner_id INTEGER NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    max_per_user INTEGER NOT NULL,
                    max_length INTEGER NOT NULL,
                    deadline_ts INTEGER NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open'
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS roulette_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    roulette_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    FOREIGN KEY (roulette_id) REFERENCES roulettes(id) ON DELETE CASCADE
                );
                """
            )
            r_cols = {
                r["name"]
                for r in cur.execute("PRAGMA table_info(roulettes)").fetchall()
            }
            if "title" not in r_cols:
                cur.execute("ALTER TABLE roulettes ADD COLUMN title TEXT NOT NULL DEFAULT '';")
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS bump_notify_settings (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER,
                    bump_enabled INTEGER NOT NULL DEFAULT 1,
                    up_enabled INTEGER NOT NULL DEFAULT 1,
                    bump_delay_minutes INTEGER NOT NULL DEFAULT {DEFAULT_BUMP_DELAY_MINUTES},
                    up_delay_minutes INTEGER NOT NULL DEFAULT {DEFAULT_UP_DELAY_MINUTES},
                    bump_bot_id INTEGER NOT NULL DEFAULT {DEFAULT_DISBOARD_BOT_ID},
                    up_bot_id INTEGER NOT NULL DEFAULT {DEFAULT_DISSOKU_BOT_ID}
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS bump_reminders (
                    guild_id INTEGER NOT NULL,
                    command_type TEXT NOT NULL,
                    channel_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    notify_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (guild_id, command_type)
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_media_traffic (
                    date_jst TEXT PRIMARY KEY,
                    total_bytes INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS aicord_user_tokens (
                    user_id INTEGER PRIMARY KEY,
                    encrypted_token BLOB NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS aicord_channel_configs (
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    character_id TEXT NOT NULL,
                    character_name TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, channel_id)
                );
                """
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- 日次メディア通信量 ---

    def get_daily_media_traffic(self, date_jst: str | None = None) -> int:
        """指定日（JST YYYY-MM-DD、省略時は本日）の累計メディア通信量（バイト）を返す。"""
        target_date = date_jst or _now_jst_str()[:10]
        with self._lock:
            row = self._conn.execute(
                "SELECT total_bytes FROM daily_media_traffic WHERE date_jst = ?",
                (target_date,),
            ).fetchone()
            return int(row[0]) if row is not None else 0

    def add_daily_media_traffic(
        self, bytes_count: int, date_jst: str | None = None
    ) -> int:
        """指定日（JST YYYY-MM-DD、省略時は本日）の累計メディア通信量を加算し、更新後の値を返す。"""
        target_date = date_jst or _now_jst_str()[:10]
        added = max(0, int(bytes_count))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO daily_media_traffic (date_jst, total_bytes)
                VALUES (?, ?)
                ON CONFLICT(date_jst) DO UPDATE SET
                    total_bytes = daily_media_traffic.total_bytes + excluded.total_bytes
                """,
                (target_date, added),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT total_bytes FROM daily_media_traffic WHERE date_jst = ?",
                (target_date,),
            ).fetchone()
            return int(row[0]) if row is not None else added

    # --- AICord ユーザートークン & チャンネル設定 ---

    def set_aicord_user_token(self, user_id: int, access_token: str) -> None:
        """ユーザーの AICord アクセストークンを暗号化して保存する。"""
        encrypted = self._cipher.encrypt(access_token)
        now_str = _now_jst_str()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO aicord_user_tokens (user_id, encrypted_token, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    encrypted_token = excluded.encrypted_token,
                    updated_at = excluded.updated_at
                """,
                (user_id, encrypted, now_str),
            )
            self._conn.commit()

    def get_aicord_user_token(self, user_id: int) -> str | None:
        """保存されたユーザーの AICord アクセストークンを復号して返す。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT encrypted_token FROM aicord_user_tokens WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                return None
            return self._cipher.decrypt(bytes(row[0]))

    def delete_aicord_user_token(self, user_id: int) -> bool:
        """ユーザーの AICord アクセストークンを削除する。"""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM aicord_user_tokens WHERE user_id = ?",
                (user_id,),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def set_aicord_channel(
        self, guild_id: int, channel_id: int, character_id: str, character_name: str
    ) -> None:
        """指定チャンネルの AICord 対話キャラクターを設定する。"""
        now_str = _now_jst_str()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO aicord_channel_configs
                    (guild_id, channel_id, character_id, character_name, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, channel_id) DO UPDATE SET
                    character_id = excluded.character_id,
                    character_name = excluded.character_name,
                    updated_at = excluded.updated_at
                """,
                (guild_id, channel_id, character_id, character_name, now_str),
            )
            self._conn.commit()

    def remove_aicord_channel(self, guild_id: int, channel_id: int) -> bool:
        """指定チャンネルの AICord 対話設定を削除する。"""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM aicord_channel_configs WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def get_aicord_channel(
        self, guild_id: int, channel_id: int
    ) -> tuple[str, str] | None:
        """指定チャンネルに設定された (character_id, character_name) を返す。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT character_id, character_name FROM aicord_channel_configs "
                "WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            ).fetchone()
            if row is None:
                return None
            return str(row[0]), str(row[1])

    def list_aicord_channels(self, guild_id: int) -> list[tuple[int, str, str]]:
        """ギルド内の設定済みチャンネル一覧 (channel_id, character_id, character_name) を返す。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT channel_id, character_id, character_name FROM aicord_channel_configs "
                "WHERE guild_id = ? ORDER BY channel_id ASC",
                (guild_id,),
            ).fetchall()
            return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]

    # --- ギルド設定 ---

    def get_guild_settings(self, guild_id: int) -> GuildSettings:
        cached = self._guild_settings_cache.get(guild_id)
        if cached is not None:
            return cached
        with self._lock:
            row = self._conn.execute(
                "SELECT proxy_role_ids, admin_role_ids, log_channel_id, binjou_channel_ids, binjou_probability, stage_mod_role_ids, self_assignable_role_ids, tadocoin_excluded_channel_ids FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        if row is None:
            result = GuildSettings(guild_id=guild_id)
            if len(self._guild_settings_cache) >= 2000:
                self._guild_settings_cache.clear()
            self._guild_settings_cache[guild_id] = result
            return result
        try:
            proxy_ids = tuple(int(r) for r in json.loads(row["proxy_role_ids"]))
        except (ValueError, TypeError):
            proxy_ids = ()
        try:
            admin_ids = tuple(int(r) for r in json.loads(row["admin_role_ids"]))
        except (ValueError, TypeError):
            admin_ids = ()
        log_channel_id = int(row["log_channel_id"]) if row["log_channel_id"] is not None else None
        try:
            binjou_ids = tuple(int(c) for c in json.loads(row["binjou_channel_ids"]))
        except (ValueError, TypeError):
            binjou_ids = ()
        try:
            binjou_prob = float(row["binjou_probability"])
        except (ValueError, TypeError):
            binjou_prob = 5.0
        try:
            stage_mod_ids = tuple(int(r) for r in json.loads(row["stage_mod_role_ids"]))
        except (ValueError, TypeError):
            stage_mod_ids = ()
        try:
            self_assignable_ids = tuple(
                int(r) for r in json.loads(row["self_assignable_role_ids"])
            )
        except (ValueError, TypeError):
            self_assignable_ids = ()
        try:
            tadocoin_excluded_ids = tuple(
                int(c) for c in json.loads(row["tadocoin_excluded_channel_ids"])
            )
        except (ValueError, TypeError):
            tadocoin_excluded_ids = ()
        result = GuildSettings(
            guild_id=guild_id,
            proxy_role_ids=proxy_ids,
            admin_role_ids=admin_ids,
            log_channel_id=log_channel_id,
            binjou_channel_ids=binjou_ids,
            binjou_probability=binjou_prob,
            stage_mod_role_ids=stage_mod_ids,
            self_assignable_role_ids=self_assignable_ids,
            tadocoin_excluded_channel_ids=tadocoin_excluded_ids,
        )
        if len(self._guild_settings_cache) >= 2000:
            self._guild_settings_cache.clear()
        self._guild_settings_cache[guild_id] = result
        return result

    def _invalidate_guild_settings(self, guild_id: int) -> None:
        self._guild_settings_cache.pop(guild_id, None)

    def set_log_channel(
        self, guild_id: int, channel_id: int | None
    ) -> GuildSettings:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO guild_settings (guild_id, log_channel_id)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET log_channel_id = excluded.log_channel_id
                """,
                (guild_id, channel_id),
            )
            self._conn.commit()
        self._invalidate_guild_settings(guild_id)
        return self.get_guild_settings(guild_id)

    def set_proxy_roles(self, guild_id: int, role_ids: list[int]) -> GuildSettings:
        serialized = json.dumps(sorted(set(int(r) for r in role_ids)))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO guild_settings (guild_id, proxy_role_ids)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET proxy_role_ids = excluded.proxy_role_ids
                """,
                (guild_id, serialized),
            )
            self._conn.commit()
        self._invalidate_guild_settings(guild_id)
        return self.get_guild_settings(guild_id)

    def set_admin_roles(self, guild_id: int, role_ids: list[int]) -> GuildSettings:
        serialized = json.dumps(sorted(set(int(r) for r in role_ids)))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO guild_settings (guild_id, admin_role_ids)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET admin_role_ids = excluded.admin_role_ids
                """,
                (guild_id, serialized),
            )
            self._conn.commit()
        self._invalidate_guild_settings(guild_id)
        return self.get_guild_settings(guild_id)

    def set_binjou_channels(self, guild_id: int, channel_ids: list[int]) -> GuildSettings:
        serialized = json.dumps(sorted(set(int(c) for c in channel_ids)))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO guild_settings (guild_id, binjou_channel_ids)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET binjou_channel_ids = excluded.binjou_channel_ids
                """,
                (guild_id, serialized),
            )
            self._conn.commit()
        self._invalidate_guild_settings(guild_id)
        return self.get_guild_settings(guild_id)

    def set_binjou_probability(self, guild_id: int, probability: float) -> GuildSettings:
        clamped = max(0.0, min(100.0, float(probability)))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO guild_settings (guild_id, binjou_probability)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET binjou_probability = excluded.binjou_probability
                """,
                (guild_id, clamped),
            )
            self._conn.commit()
        self._invalidate_guild_settings(guild_id)
        return self.get_guild_settings(guild_id)

    def set_stage_mod_roles(self, guild_id: int, role_ids: list[int]) -> GuildSettings:
        serialized = json.dumps(sorted(set(int(r) for r in role_ids)))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO guild_settings (guild_id, stage_mod_role_ids)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET stage_mod_role_ids = excluded.stage_mod_role_ids
                """,
                (guild_id, serialized),
            )
            self._conn.commit()
        self._invalidate_guild_settings(guild_id)
        return self.get_guild_settings(guild_id)

    def set_self_assignable_roles(self, guild_id: int, role_ids: list[int]) -> GuildSettings:
        serialized = json.dumps(sorted(set(int(r) for r in role_ids)))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO guild_settings (guild_id, self_assignable_role_ids)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET self_assignable_role_ids = excluded.self_assignable_role_ids
                """,
                (guild_id, serialized),
            )
            self._conn.commit()
            self._sync_role_toggle_backup_locked(overwrite_guild_settings_id=guild_id)
        self._invalidate_guild_settings(guild_id)
        return self.get_guild_settings(guild_id)

    def get_user_toggleable_roles(self, guild_id: int, user_id: int) -> set[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role_id FROM user_toggleable_roles WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchall()
        return {int(row["role_id"]) for row in rows}

    def record_user_toggleable_roles(
        self, guild_id: int, user_id: int, role_ids: list[int] | set[int]
    ) -> None:
        if not role_ids:
            return
        with self._lock:
            cur = self._conn.executemany(
                """
                INSERT OR IGNORE INTO user_toggleable_roles (guild_id, user_id, role_id)
                VALUES (?, ?, ?)
                """,
                [(guild_id, user_id, int(rid)) for rid in role_ids],
            )
            inserted = cur.rowcount
            self._conn.commit()
            if inserted:
                self._sync_role_toggle_backup_locked()

    def _load_role_backup_raw(self) -> dict[str, dict]:
        if self._role_backup_path is None or not self._role_backup_path.exists():
            return {"guilds": {}}
        try:
            data = json.loads(self._role_backup_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("guilds"), dict):
                return data
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("非暗号化ロールバックアップの読み込みに失敗しました: %s", exc)
        return {"guilds": {}}

    def _sync_role_toggle_backup_locked(
        self, *, overwrite_guild_settings_id: int | None = None
    ) -> None:
        """ロール付け外し設定と対象ユーザー履歴を非暗号化 JSON に同期する。"""
        if self._role_backup_path is None:
            return
        backup = self._load_role_backup_raw()
        guilds_map: dict[str, dict] = backup.setdefault("guilds", {})

        for row in self._conn.execute(
            "SELECT guild_id, self_assignable_role_ids FROM guild_settings"
        ).fetchall():
            gid = int(row["guild_id"])
            gid_key = str(gid)
            entry = guilds_map.setdefault(gid_key, {})
            try:
                db_roles = sorted(
                    {int(r) for r in json.loads(row["self_assignable_role_ids"])}
                )
            except (ValueError, TypeError):
                db_roles = []
            if gid == overwrite_guild_settings_id or db_roles:
                entry["self_assignable_role_ids"] = db_roles
            else:
                entry.setdefault("self_assignable_role_ids", [])
            entry.setdefault("users", {})

        for row in self._conn.execute(
            "SELECT guild_id, user_id, role_id FROM user_toggleable_roles"
        ).fetchall():
            gid_key = str(int(row["guild_id"]))
            uid_key = str(int(row["user_id"]))
            rid = int(row["role_id"])
            entry = guilds_map.setdefault(
                gid_key, {"self_assignable_role_ids": [], "users": {}}
            )
            users_map = entry.setdefault("users", {})
            existing = set(int(x) for x in users_map.get(uid_key, []))
            existing.add(rid)
            users_map[uid_key] = sorted(existing)

        self._role_backup_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._role_backup_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(backup, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, self._role_backup_path)

    def _auto_restore_role_backup_if_empty(self) -> None:
        if self._role_backup_path is None or not self._role_backup_path.exists():
            return
        with self._lock:
            has_any = self._conn.execute(
                "SELECT 1 FROM user_toggleable_roles LIMIT 1"
            ).fetchone()
        if has_any is None:
            restored_guilds, restored_entries = self.restore_role_toggle_backup()
            if restored_guilds or restored_entries:
                logger.info(
                    "非暗号化バックアップからロール履歴を自動復元しました (guilds=%d, entries=%d)",
                    restored_guilds,
                    restored_entries,
                )

    def restore_role_toggle_backup(
        self, guild_id: int | None = None
    ) -> tuple[int, int]:
        """非暗号化バックアップからロール付け外し設定・ユーザー履歴を復元する。"""
        if self._role_backup_path is None or not self._role_backup_path.exists():
            return (0, 0)

        backup = self._load_role_backup_raw()
        guilds_map = backup.get("guilds", {})
        restored_guilds = 0
        restored_entries = 0

        with self._lock:
            for gid_key, gdata in guilds_map.items():
                try:
                    gid = int(gid_key)
                except (ValueError, TypeError):
                    continue
                if guild_id is not None and gid != guild_id:
                    continue
                if not isinstance(gdata, dict):
                    continue

                raw_self_roles = gdata.get("self_assignable_role_ids", [])
                if isinstance(raw_self_roles, list) and raw_self_roles:
                    row = self._conn.execute(
                        "SELECT self_assignable_role_ids FROM guild_settings WHERE guild_id = ?",
                        (gid,),
                    ).fetchone()
                    current_roles: list[int] = []
                    if row is not None:
                        try:
                            current_roles = json.loads(row["self_assignable_role_ids"])
                        except (ValueError, TypeError):
                            current_roles = []
                    if not current_roles:
                        serialized = json.dumps(
                            sorted({int(r) for r in raw_self_roles})
                        )
                        self._conn.execute(
                            """
                            INSERT INTO guild_settings (guild_id, self_assignable_role_ids)
                            VALUES (?, ?)
                            ON CONFLICT(guild_id) DO UPDATE SET self_assignable_role_ids = excluded.self_assignable_role_ids
                            """,
                            (gid, serialized),
                        )
                        restored_guilds += 1

                users_map = gdata.get("users", {})
                if isinstance(users_map, dict):
                    for uid_key, rids in users_map.items():
                        try:
                            uid = int(uid_key)
                        except (ValueError, TypeError):
                            continue
                        if not isinstance(rids, list):
                            continue
                        for rid in rids:
                            try:
                                rid_int = int(rid)
                            except (ValueError, TypeError):
                                continue
                            cur = self._conn.execute(
                                """
                                INSERT OR IGNORE INTO user_toggleable_roles (guild_id, user_id, role_id)
                                VALUES (?, ?, ?)
                                """,
                                (gid, uid, rid_int),
                            )
                            if cur.rowcount and cur.rowcount > 0:
                                restored_entries += 1
            self._conn.commit()
        self._guild_settings_cache.clear()
        return (restored_guilds, restored_entries)

    def set_tadocoin_excluded_channels(
        self, guild_id: int, channel_ids: list[int]
    ) -> GuildSettings:
        serialized = json.dumps(sorted(set(int(c) for c in channel_ids)))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO guild_settings (guild_id, tadocoin_excluded_channel_ids)
                VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET tadocoin_excluded_channel_ids = excluded.tadocoin_excluded_channel_ids
                """,
                (guild_id, serialized),
            )
            self._conn.commit()
        self._invalidate_guild_settings(guild_id)
        return self.get_guild_settings(guild_id)

    # --- タドコイン & リーダーボード ---

    def get_tadocoin_balance(self, guild_id: int, user_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT balance FROM tadocoin_balances WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
        return int(row["balance"]) if row is not None else 0

    def add_tadocoin(self, guild_id: int, user_id: int, delta: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT balance FROM tadocoin_balances WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            current = int(row["balance"]) if row is not None else 0
            new_balance = max(0, current + int(delta))
            self._conn.execute(
                """
                INSERT INTO tadocoin_balances (guild_id, user_id, balance)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET balance = excluded.balance
                """,
                (guild_id, user_id, new_balance),
            )
            self._conn.commit()
        return new_balance

    def set_tadocoin(self, guild_id: int, user_id: int, amount: int) -> int:
        new_balance = max(0, int(amount))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO tadocoin_balances (guild_id, user_id, balance)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET balance = excluded.balance
                """,
                (guild_id, user_id, new_balance),
            )
            self._conn.commit()
        return new_balance

    def try_spend_tadocoin(
        self, guild_id: int, user_id: int, amount: int
    ) -> tuple[bool, int]:
        cost = int(amount)
        if cost <= 0:
            return False, self.get_tadocoin_balance(guild_id, user_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT balance FROM tadocoin_balances WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            current = int(row["balance"]) if row is not None else 0
            if current < cost:
                return False, current
            new_balance = current - cost
            self._conn.execute(
                """
                INSERT INTO tadocoin_balances (guild_id, user_id, balance)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET balance = excluded.balance
                """,
                (guild_id, user_id, new_balance),
            )
            self._conn.commit()
        return True, new_balance

    def get_tadocoin_leaderboard(
        self, guild_id: int, *, limit: int = 10
    ) -> list[tuple[int, int]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT user_id, balance
                FROM tadocoin_balances
                WHERE guild_id = ? AND balance > 0
                ORDER BY balance DESC, user_id ASC
                LIMIT ?
                """,
                (guild_id, limit),
            ).fetchall()
        return [(int(r["user_id"]), int(r["balance"])) for r in rows]

    def record_shinkei_clear(
        self,
        guild_id: int,
        difficulty: str,
        user_id: int,
        elapsed_sec: float,
        moves: int,
    ) -> bool:
        """神経衰弱のクリアタイムを記録し、自己ベスト更新なら True を返す。"""
        now_str = _now_jst_str()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT best_time_sec FROM shinkei_records
                WHERE guild_id = ? AND difficulty = ? AND user_id = ?
                """,
                (guild_id, difficulty, user_id),
            ).fetchone()
            if row is not None and float(row["best_time_sec"]) <= elapsed_sec:
                return False
            self._conn.execute(
                """
                INSERT INTO shinkei_records (guild_id, difficulty, user_id, best_time_sec, moves, achieved_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, difficulty, user_id) DO UPDATE SET
                    best_time_sec = excluded.best_time_sec,
                    moves = excluded.moves,
                    achieved_at = excluded.achieved_at
                """,
                (guild_id, difficulty, user_id, float(elapsed_sec), int(moves), now_str),
            )
            self._conn.commit()
        return True

    def get_shinkei_leaderboard(
        self, guild_id: int, difficulty: str, *, limit: int = 10
    ) -> list[tuple[int, float, int]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT user_id, best_time_sec, moves
                FROM shinkei_records
                WHERE guild_id = ? AND difficulty = ?
                ORDER BY best_time_sec ASC, moves ASC
                LIMIT ?
                """,
                (guild_id, difficulty, limit),
            ).fetchall()
        return [
            (int(r["user_id"]), float(r["best_time_sec"]), int(r["moves"]))
            for r in rows
        ]

    # --- サブスクロール（週次課金） ---

    def set_sub_role_plan(
        self, guild_id: int, role_id: int, weekly_cost: int
    ) -> None:
        with self._lock:
            if weekly_cost <= 0:
                self._conn.execute(
                    "DELETE FROM sub_role_plans WHERE guild_id = ? AND role_id = ?",
                    (guild_id, role_id),
                )
            else:
                self._conn.execute(
                    """
                    INSERT INTO sub_role_plans (guild_id, role_id, weekly_cost)
                    VALUES (?, ?, ?)
                    ON CONFLICT(guild_id, role_id) DO UPDATE SET weekly_cost = excluded.weekly_cost
                    """,
                    (guild_id, role_id, int(weekly_cost)),
                )
            self._conn.commit()

    def get_sub_role_plans(self, guild_id: int) -> dict[int, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role_id, weekly_cost FROM sub_role_plans WHERE guild_id = ? ORDER BY weekly_cost ASC, role_id ASC",
                (guild_id,),
            ).fetchall()
        return {int(r["role_id"]): int(r["weekly_cost"]) for r in rows}

    def subscribe_user_role(
        self, guild_id: int, user_id: int, role_id: int, next_billing_ts: int
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO user_role_subscriptions (guild_id, user_id, role_id, next_billing_ts)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id, role_id) DO UPDATE SET next_billing_ts = excluded.next_billing_ts
                """,
                (guild_id, user_id, role_id, int(next_billing_ts)),
            )
            self._conn.commit()

    def unsubscribe_user_role(
        self, guild_id: int, user_id: int, role_id: int
    ) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM user_role_subscriptions WHERE guild_id = ? AND user_id = ? AND role_id = ?",
                (guild_id, user_id, role_id),
            )
            self._conn.commit()

    def get_user_subscriptions(self, guild_id: int, user_id: int) -> dict[int, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role_id, next_billing_ts FROM user_role_subscriptions WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchall()
        return {int(r["role_id"]): int(r["next_billing_ts"]) for r in rows}

    def get_due_subscriptions(
        self, now_ts: int, *, guild_id: int | None = None
    ) -> list[tuple[int, int, int, int]]:
        with self._lock:
            if guild_id is None:
                rows = self._conn.execute(
                    "SELECT guild_id, user_id, role_id, next_billing_ts FROM user_role_subscriptions WHERE next_billing_ts <= ?",
                    (int(now_ts),),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT guild_id, user_id, role_id, next_billing_ts FROM user_role_subscriptions WHERE guild_id = ? AND next_billing_ts <= ?",
                    (guild_id, int(now_ts)),
                ).fetchall()
        return [
            (
                int(r["guild_id"]),
                int(r["user_id"]),
                int(r["role_id"]),
                int(r["next_billing_ts"]),
            )
            for r in rows
        ]

    def get_all_subscriptions(
        self, *, guild_id: int | None = None
    ) -> list[tuple[int, int, int, int]]:
        with self._lock:
            if guild_id is None:
                rows = self._conn.execute(
                    "SELECT guild_id, user_id, role_id, next_billing_ts FROM user_role_subscriptions"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT guild_id, user_id, role_id, next_billing_ts FROM user_role_subscriptions WHERE guild_id = ?",
                    (guild_id,),
                ).fetchall()
        return [
            (
                int(r["guild_id"]),
                int(r["user_id"]),
                int(r["role_id"]),
                int(r["next_billing_ts"]),
            )
            for r in rows
        ]

    def update_subscription_next_billing(
        self, guild_id: int, user_id: int, role_id: int, next_billing_ts: int
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE user_role_subscriptions
                SET next_billing_ts = ?
                WHERE guild_id = ? AND user_id = ? AND role_id = ?
                """,
                (int(next_billing_ts), guild_id, user_id, role_id),
            )
            self._conn.commit()

    def is_sub_notify_muted(
        self, guild_id: int, user_id: int, *, role_id: int | None = None
    ) -> bool:
        with self._lock:
            if role_id is None:
                row = self._conn.execute(
                    "SELECT 1 FROM user_role_subscriptions WHERE guild_id = ? AND user_id = ? AND notify_muted = 1 LIMIT 1",
                    (guild_id, user_id),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT 1 FROM user_role_subscriptions WHERE guild_id = ? AND user_id = ? AND role_id = ? AND notify_muted = 1 LIMIT 1",
                    (guild_id, user_id, role_id),
                ).fetchone()
        return row is not None

    def set_sub_notify_muted(
        self,
        guild_id: int,
        user_id: int,
        muted: bool,
        *,
        role_id: int | None = None,
    ) -> None:
        val = 1 if muted else 0
        with self._lock:
            if role_id is None:
                self._conn.execute(
                    "UPDATE user_role_subscriptions SET notify_muted = ? WHERE guild_id = ? AND user_id = ?",
                    (val, guild_id, user_id),
                )
            else:
                self._conn.execute(
                    "UPDATE user_role_subscriptions SET notify_muted = ? WHERE guild_id = ? AND user_id = ? AND role_id = ?",
                    (val, guild_id, user_id, role_id),
                )
            self._conn.commit()

    def unmute_user_sub_notifications(self, user_id: int) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE user_role_subscriptions SET notify_muted = 0 WHERE user_id = ? AND notify_muted = 1",
                (user_id,),
            )
            self._conn.commit()
            return cur.rowcount or 0

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM bot_meta WHERE key = ?",
                (key,),
            ).fetchone()
        return str(row["value"]) if row is not None else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO bot_meta (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            self._conn.commit()

    def reset_all_data(self) -> None:
        """DB 内の全テーブルおよび非暗号化ロールバックアップを完全に初期化する。"""
        with self._lock:
            tables = [
                r["name"]
                for r in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            for table_name in tables:
                self._conn.execute(f'DELETE FROM "{table_name}";')
            self._conn.commit()
            self._guild_settings_cache.clear()
            self._bump_settings_cache.clear()
            if self._role_backup_path is not None and self._role_backup_path.exists():
                try:
                    self._role_backup_path.unlink()
                except OSError:
                    pass

    # --- プロキシメッセージ履歴＆暗号化ログ ---

    def record_proxy_message(
        self,
        *,
        proxy_msg_id: int,
        guild_id: int,
        channel_id: int,
        author_id: int,
        content: str,
        reply_target_id: int | None = None,
    ) -> None:
        encrypted = self._cipher.encrypt(content)
        now = _now_jst_str()
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO proxy_messages (
                    proxy_msg_id, guild_id, channel_id, author_id,
                    reply_target_id, content_enc, created_at, edited_at,
                    is_deleted, deleted_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL)
                """,
                (
                    proxy_msg_id,
                    guild_id,
                    channel_id,
                    author_id,
                    reply_target_id,
                    encrypted,
                    now,
                ),
            )
            self._enforce_record_limit_locked()
            self._conn.commit()

    def _enforce_record_limit_locked(self) -> None:
        if self._max_records <= 0:
            return
        self._write_ops_since_trim += 1
        # 大容量本番設定時は 500 回書き込みごとにまとめて間引き、毎回の COUNT(*) を回避する
        if self._max_records > 1000 and self._write_ops_since_trim < 500:
            return
        self._write_ops_since_trim = 0
        row = self._conn.execute("SELECT COUNT(*) AS cnt FROM proxy_messages").fetchone()
        if row is not None and row["cnt"] > self._max_records:
            excess = row["cnt"] - self._max_records
            self._conn.execute(
                """
                DELETE FROM proxy_messages
                WHERE proxy_msg_id IN (
                    SELECT proxy_msg_id FROM proxy_messages
                    ORDER BY created_at ASC, proxy_msg_id ASC
                    LIMIT ?
                )
                """,
                (excess,),
            )

        hist_row = self._conn.execute("SELECT COUNT(*) AS cnt FROM proxy_edit_history").fetchone()
        if hist_row is not None and hist_row["cnt"] > self._max_records:
            hist_excess = hist_row["cnt"] - self._max_records
            self._conn.execute(
                """
                DELETE FROM proxy_edit_history
                WHERE id IN (
                    SELECT id FROM proxy_edit_history
                    ORDER BY id ASC
                    LIMIT ?
                )
                """,
                (hist_excess,),
            )

    def update_proxy_message_content(
        self, proxy_msg_id: int, new_content: str
    ) -> tuple[str | None, str]:
        """プロキシメッセージの本文を更新し、編集履歴（編集前・後・日時）を記録する。

        戻り値: (編集前本文 または None, 編集日時 JST)
        """
        new_encrypted = self._cipher.encrypt(new_content)
        now = _now_jst_str()
        old_content: str | None = None
        with self._lock:
            row = self._conn.execute(
                "SELECT guild_id, content_enc FROM proxy_messages WHERE proxy_msg_id = ?",
                (proxy_msg_id,),
            ).fetchone()
            if row is not None:
                old_encrypted = bytes(row["content_enc"])
                old_content = self._cipher.decrypt(old_encrypted)
                self._conn.execute(
                    """
                    INSERT INTO proxy_edit_history (
                        proxy_msg_id, guild_id, old_content_enc, new_content_enc, edited_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (proxy_msg_id, int(row["guild_id"]), old_encrypted, new_encrypted, now),
                )
                self._conn.execute(
                    """
                    UPDATE proxy_messages
                    SET content_enc = ?, edited_at = ?
                    WHERE proxy_msg_id = ?
                    """,
                    (new_encrypted, now, proxy_msg_id),
                )
                self._enforce_record_limit_locked()
                self._conn.commit()
        return old_content, now

    def get_edit_history(
        self, proxy_msg_id: int, *, limit: int = 10
    ) -> list[ProxyEditRecord]:
        """指定メッセージの編集履歴を新しい順で返す。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT proxy_msg_id, old_content_enc, new_content_enc, edited_at
                FROM proxy_edit_history
                WHERE proxy_msg_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (proxy_msg_id, limit),
            ).fetchall()
        return [
            ProxyEditRecord(
                proxy_msg_id=int(r["proxy_msg_id"]),
                old_content=self._cipher.decrypt(bytes(r["old_content_enc"])),
                new_content=self._cipher.decrypt(bytes(r["new_content_enc"])),
                edited_at=str(r["edited_at"]),
            )
            for r in rows
        ]

    def mark_proxy_message_deleted(self, proxy_msg_id: int) -> bool:
        now = _now_jst_str()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE proxy_messages
                SET is_deleted = 1, deleted_at = COALESCE(deleted_at, ?)
                WHERE proxy_msg_id = ? AND is_deleted = 0
                """,
                (now, proxy_msg_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def get_proxy_author_id(self, proxy_msg_id: int) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT author_id FROM proxy_messages WHERE proxy_msg_id = ?",
                (proxy_msg_id,),
            ).fetchone()
        return int(row["author_id"]) if row is not None else None

    def get_proxy_message(self, proxy_msg_id: int) -> ProxyMessageRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM proxy_messages WHERE proxy_msg_id = ?",
                (proxy_msg_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def get_replies_to_target(self, reply_target_id: int) -> list[tuple[int, int]]:
        """指定した返信先メッセージ ID を参照している未削除プロキシメッセージの (channel_id, proxy_msg_id) を返す。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT channel_id, proxy_msg_id
                FROM proxy_messages
                WHERE reply_target_id = ? AND is_deleted = 0
                """,
                (reply_target_id,),
            ).fetchall()
        return [(int(r["channel_id"]), int(r["proxy_msg_id"])) for r in rows]

    def get_deleted_logs(
        self, guild_id: int, *, limit: int = 10, offset: int = 0
    ) -> list[ProxyMessageRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM proxy_messages
                WHERE guild_id = ? AND is_deleted = 1
                ORDER BY deleted_at DESC, proxy_msg_id DESC
                LIMIT ? OFFSET ?
                """,
                (guild_id, limit, offset),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def count_deleted_logs(self, guild_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM proxy_messages WHERE guild_id = ? AND is_deleted = 1",
                (guild_id,),
            ).fetchone()
        return int(row["cnt"]) if row is not None else 0

    def _row_to_record(self, row: sqlite3.Row) -> ProxyMessageRecord:
        return ProxyMessageRecord(
            proxy_msg_id=int(row["proxy_msg_id"]),
            guild_id=int(row["guild_id"]),
            channel_id=int(row["channel_id"]),
            author_id=int(row["author_id"]),
            reply_target_id=(
                int(row["reply_target_id"]) if row["reply_target_id"] is not None else None
            ),
            content=self._cipher.decrypt(bytes(row["content_enc"])),
            created_at=str(row["created_at"]),
            edited_at=str(row["edited_at"]) if row["edited_at"] is not None else None,
            is_deleted=bool(row["is_deleted"]),
            deleted_at=str(row["deleted_at"]) if row["deleted_at"] is not None else None,
        )

    # --- おみくじ履歴 ---

    def get_omikuji_today(
        self, guild_id: int, user_id: int, today_jst: str
    ) -> tuple[str, str] | None:
        """本日（JST）すでにおみくじを引いていれば (結果名, 引いた日時) を返す。"""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT drawn_date, result_name, drawn_at
                FROM omikuji_history
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()
        if row is not None and str(row["drawn_date"]) == today_jst:
            return str(row["result_name"]), str(row["drawn_at"])
        return None

    def record_omikuji_draw(
        self, guild_id: int, user_id: int, today_jst: str, result_name: str
    ) -> str:
        """おみくじ結果を保存し、引いた日時（JST）を返す。"""
        now = _now_jst_str()
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO omikuji_history (
                    guild_id, user_id, drawn_date, result_name, drawn_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (guild_id, user_id, today_jst, result_name, now),
            )
            self._conn.commit()
        return now

    def reset_omikuji_cooldown(self, guild_id: int, user_id: int) -> bool:
        """デバッグ用：指定ユーザーのおみくじクールダウン（本日の記録）をリセットする。"""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM omikuji_history WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # --- ルーレット機能 ---

    def create_roulette(
        self,
        guild_id: int,
        channel_id: int,
        owner_id: int,
        title: str,
        max_per_user: int,
        max_length: int,
        deadline_ts: int,
        description: str = "",
    ) -> RouletteRecord:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO roulettes (
                    guild_id, channel_id, owner_id, title, max_per_user, max_length, deadline_ts, description, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')
                """,
                (guild_id, channel_id, owner_id, title, max_per_user, max_length, deadline_ts, description),
            )
            self._conn.commit()
            rid = int(cur.lastrowid)
        return RouletteRecord(
            id=rid,
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=0,
            owner_id=owner_id,
            title=title,
            max_per_user=max_per_user,
            max_length=max_length,
            deadline_ts=deadline_ts,
            description=description,
            status="open",
        )

    def set_roulette_message_id(self, roulette_id: int, message_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE roulettes SET message_id = ? WHERE id = ?",
                (message_id, roulette_id),
            )
            self._conn.commit()

    def set_roulette_status(self, roulette_id: int, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE roulettes SET status = ? WHERE id = ?",
                (status, roulette_id),
            )
            self._conn.commit()

    def get_roulette(self, roulette_id: int) -> RouletteRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM roulettes WHERE id = ?",
                (roulette_id,),
            ).fetchone()
        if row is None:
            return None
        return RouletteRecord(
            id=int(row["id"]),
            guild_id=int(row["guild_id"]),
            channel_id=int(row["channel_id"]),
            message_id=int(row["message_id"]),
            owner_id=int(row["owner_id"]),
            title=str(row["title"]) if "title" in row.keys() else "",
            max_per_user=int(row["max_per_user"]),
            max_length=int(row["max_length"]),
            deadline_ts=int(row["deadline_ts"]),
            description=str(row["description"]),
            status=str(row["status"]),
        )

    def list_roulettes_by_guild(
        self, guild_id: int, *, status: str | None = None
    ) -> list[RouletteRecord]:
        with self._lock:
            if status is not None:
                rows = self._conn.execute(
                    "SELECT * FROM roulettes WHERE guild_id = ? AND status = ? ORDER BY id DESC",
                    (guild_id, status),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM roulettes WHERE guild_id = ? ORDER BY id DESC",
                    (guild_id,),
                ).fetchall()
        return [
            RouletteRecord(
                id=int(r["id"]),
                guild_id=int(r["guild_id"]),
                channel_id=int(r["channel_id"]),
                message_id=int(r["message_id"]),
                owner_id=int(r["owner_id"]),
                title=str(r["title"]) if "title" in r.keys() else "",
                max_per_user=int(r["max_per_user"]),
                max_length=int(r["max_length"]),
                deadline_ts=int(r["deadline_ts"]),
                description=str(r["description"]),
                status=str(r["status"]),
            )
            for r in rows
        ]

    def list_expired_open_roulettes(self, now_ts: int) -> list[RouletteRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM roulettes WHERE status = 'open' AND deadline_ts <= ?",
                (now_ts,),
            ).fetchall()
        return [
            RouletteRecord(
                id=int(r["id"]),
                guild_id=int(r["guild_id"]),
                channel_id=int(r["channel_id"]),
                message_id=int(r["message_id"]),
                owner_id=int(r["owner_id"]),
                title=str(r["title"]) if "title" in r.keys() else "",
                max_per_user=int(r["max_per_user"]),
                max_length=int(r["max_length"]),
                deadline_ts=int(r["deadline_ts"]),
                description=str(r["description"]),
                status=str(r["status"]),
            )
            for r in rows
        ]

    def delete_roulette(self, roulette_id: int) -> bool:
        with self._lock:
            self._conn.execute(
                "DELETE FROM roulette_candidates WHERE roulette_id = ?",
                (roulette_id,),
            )
            cur = self._conn.execute(
                "DELETE FROM roulettes WHERE id = ?",
                (roulette_id,),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def add_roulette_candidate(
        self, roulette_id: int, user_id: int, text: str
    ) -> RouletteCandidateRecord:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO roulette_candidates (roulette_id, user_id, text) VALUES (?, ?, ?)",
                (roulette_id, user_id, text),
            )
            self._conn.commit()
            cid = int(cur.lastrowid)
        return RouletteCandidateRecord(
            id=cid, roulette_id=roulette_id, user_id=user_id, text=text
        )

    def list_roulette_candidates(
        self, roulette_id: int, *, user_id: int | None = None
    ) -> list[RouletteCandidateRecord]:
        with self._lock:
            if user_id is not None:
                rows = self._conn.execute(
                    "SELECT * FROM roulette_candidates WHERE roulette_id = ? AND user_id = ? ORDER BY id ASC",
                    (roulette_id, user_id),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM roulette_candidates WHERE roulette_id = ? ORDER BY id ASC",
                    (roulette_id,),
                ).fetchall()
        return [
            RouletteCandidateRecord(
                id=int(r["id"]),
                roulette_id=int(r["roulette_id"]),
                user_id=int(r["user_id"]),
                text=str(r["text"]),
            )
            for r in rows
        ]

    def delete_roulette_candidate(self, candidate_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM roulette_candidates WHERE id = ?",
                (candidate_id,),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # --- Bump / Up 通知設定・リマインダー ---

    def get_bump_notify_settings(self, guild_id: int) -> BumpNotifySettings:
        cached = self._bump_settings_cache.get(guild_id)
        if cached is not None:
            return cached
        with self._lock:
            row = self._conn.execute(
                """
                SELECT guild_id, channel_id, bump_enabled, up_enabled,
                       bump_delay_minutes, up_delay_minutes, bump_bot_id, up_bot_id
                FROM bump_notify_settings
                WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()
        if row is None:
            result = BumpNotifySettings(guild_id=guild_id)
        else:
            result = BumpNotifySettings(
                guild_id=int(row["guild_id"]),
                channel_id=int(row["channel_id"]) if row["channel_id"] is not None else None,
                bump_enabled=bool(row["bump_enabled"]),
                up_enabled=bool(row["up_enabled"]),
                bump_delay_minutes=max(1, int(row["bump_delay_minutes"])),
                up_delay_minutes=max(1, int(row["up_delay_minutes"])),
                bump_bot_id=int(row["bump_bot_id"]),
                up_bot_id=int(row["up_bot_id"]),
            )
        if len(self._bump_settings_cache) >= 2000:
            self._bump_settings_cache.clear()
        self._bump_settings_cache[guild_id] = result
        return result

    def update_bump_notify_settings(
        self,
        guild_id: int,
        *,
        channel_id: int | None | object = _UNSET,
        bump_enabled: bool | None = None,
        up_enabled: bool | None = None,
        bump_delay_minutes: int | None = None,
        up_delay_minutes: int | None = None,
        bump_bot_id: int | None = None,
        up_bot_id: int | None = None,
    ) -> BumpNotifySettings:
        current = self.get_bump_notify_settings(guild_id)
        next_channel = (
            current.channel_id
            if channel_id is _UNSET
            else (int(channel_id) if channel_id is not None else None)
        )
        next_bump_enabled = (
            current.bump_enabled if bump_enabled is None else bool(bump_enabled)
        )
        next_up_enabled = (
            current.up_enabled if up_enabled is None else bool(up_enabled)
        )
        next_bump_delay = (
            current.bump_delay_minutes
            if bump_delay_minutes is None
            else max(1, int(bump_delay_minutes))
        )
        next_up_delay = (
            current.up_delay_minutes
            if up_delay_minutes is None
            else max(1, int(up_delay_minutes))
        )
        next_bump_bot = (
            current.bump_bot_id if bump_bot_id is None else int(bump_bot_id)
        )
        next_up_bot = current.up_bot_id if up_bot_id is None else int(up_bot_id)

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO bump_notify_settings (
                    guild_id, channel_id, bump_enabled, up_enabled,
                    bump_delay_minutes, up_delay_minutes, bump_bot_id, up_bot_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    bump_enabled = excluded.bump_enabled,
                    up_enabled = excluded.up_enabled,
                    bump_delay_minutes = excluded.bump_delay_minutes,
                    up_delay_minutes = excluded.up_delay_minutes,
                    bump_bot_id = excluded.bump_bot_id,
                    up_bot_id = excluded.up_bot_id
                """,
                (
                    guild_id,
                    next_channel,
                    int(next_bump_enabled),
                    int(next_up_enabled),
                    next_bump_delay,
                    next_up_delay,
                    next_bump_bot,
                    next_up_bot,
                ),
            )
            self._conn.commit()
        self._bump_settings_cache.pop(guild_id, None)
        return self.get_bump_notify_settings(guild_id)

    def upsert_bump_reminder(
        self,
        guild_id: int,
        command_type: str,
        channel_id: int,
        user_id: int,
        notify_at: float,
        created_at: float | None = None,
    ) -> BumpReminderRecord:
        now_ts = float(created_at if created_at is not None else datetime.now(timezone.utc).timestamp())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO bump_reminders (
                    guild_id, command_type, channel_id, user_id, notify_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, command_type) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    user_id = excluded.user_id,
                    notify_at = excluded.notify_at,
                    created_at = excluded.created_at
                """,
                (guild_id, command_type, channel_id, user_id, float(notify_at), now_ts),
            )
            self._conn.commit()
        return BumpReminderRecord(
            guild_id=guild_id,
            command_type=command_type,
            channel_id=channel_id,
            user_id=user_id,
            notify_at=float(notify_at),
            created_at=now_ts,
        )

    def get_bump_reminder(
        self, guild_id: int, command_type: str
    ) -> BumpReminderRecord | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT guild_id, command_type, channel_id, user_id, notify_at, created_at
                FROM bump_reminders
                WHERE guild_id = ? AND command_type = ?
                """,
                (guild_id, command_type),
            ).fetchone()
        if row is None:
            return None
        return BumpReminderRecord(
            guild_id=int(row["guild_id"]),
            command_type=str(row["command_type"]),
            channel_id=int(row["channel_id"]),
            user_id=int(row["user_id"]),
            notify_at=float(row["notify_at"]),
            created_at=float(row["created_at"]),
        )

    def list_bump_reminders(
        self, guild_id: int | None = None
    ) -> list[BumpReminderRecord]:
        with self._lock:
            if guild_id is not None:
                rows = self._conn.execute(
                    """
                    SELECT guild_id, command_type, channel_id, user_id, notify_at, created_at
                    FROM bump_reminders
                    WHERE guild_id = ?
                    ORDER BY notify_at ASC
                    """,
                    (guild_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT guild_id, command_type, channel_id, user_id, notify_at, created_at
                    FROM bump_reminders
                    ORDER BY notify_at ASC
                    """
                ).fetchall()
        return [
            BumpReminderRecord(
                guild_id=int(r["guild_id"]),
                command_type=str(r["command_type"]),
                channel_id=int(r["channel_id"]),
                user_id=int(r["user_id"]),
                notify_at=float(r["notify_at"]),
                created_at=float(r["created_at"]),
            )
            for r in rows
        ]

    def delete_bump_reminder(self, guild_id: int, command_type: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM bump_reminders WHERE guild_id = ? AND command_type = ?",
                (guild_id, command_type),
            )
            self._conn.commit()
            return cur.rowcount > 0
