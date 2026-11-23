"""暗号化ログ・プロキシ履歴・サーバーロール設定を永続化する SQLite ストレージ。"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# サーバーロール設定に関わらず常に管理操作を許可する例外ユーザー ID (wooden_sword.704)
OWNER_USER_ID: int = 1108317459125121096

DEFAULT_DB_PATH = Path("data/yajubot.db")
DEFAULT_KEY_PATH = Path("data/.log_key")
DEFAULT_MAX_RECORDS = 50_000


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
        return self._fernet.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, token: bytes) -> str:
        try:
            return self._fernet.decrypt(token).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError):
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
    ) -> None:
        self._db_path_str = str(db_path)
        self._is_memory = self._db_path_str == ":memory:"
        if not self._is_memory:
            Path(self._db_path_str).parent.mkdir(parents=True, exist_ok=True)

        self._cipher = cipher or LogCipher(
            key_path=None if self._is_memory else DEFAULT_KEY_PATH
        )
        self._max_records = max_records
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path_str, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
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
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    proxy_role_ids TEXT NOT NULL DEFAULT '[]',
                    admin_role_ids TEXT NOT NULL DEFAULT '[]',
                    log_channel_id INTEGER,
                    binjou_channel_ids TEXT NOT NULL DEFAULT '[]',
                    binjou_probability REAL NOT NULL DEFAULT 5.0
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
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- ギルド設定 ---

    def get_guild_settings(self, guild_id: int) -> GuildSettings:
        with self._lock:
            row = self._conn.execute(
                "SELECT proxy_role_ids, admin_role_ids, log_channel_id, binjou_channel_ids, binjou_probability FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        if row is None:
            return GuildSettings(guild_id=guild_id)
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
        return GuildSettings(
            guild_id=guild_id,
            proxy_role_ids=proxy_ids,
            admin_role_ids=admin_ids,
            log_channel_id=log_channel_id,
            binjou_channel_ids=binjou_ids,
            binjou_probability=binjou_prob,
        )

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
        return self.get_guild_settings(guild_id)

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
