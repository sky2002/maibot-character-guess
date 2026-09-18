from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import json
import sqlite3
import time

from .models import Evidence, Game


class Store:
    """游戏、跨群配额和缓存共享单个事务存储，进程重启不重置配额。"""

    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(data_dir / "games.sqlite3", timeout=5)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS games (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, expires REAL NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS usage (day TEXT PRIMARY KEY, requests INTEGER NOT NULL, tokens INTEGER NOT NULL);
        """)
        self.db.commit()

    def get(self, stream_id: str) -> Optional[Game]:
        row = self.db.execute("SELECT data FROM games WHERE id=?", (stream_id,)).fetchone()
        return Game.model_validate_json(row[0]) if row else None

    def save(self, game: Game, touch: bool = True) -> None:
        if touch:
            game.updated_at = time.time()
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO games VALUES (?, ?)", (game.stream_id, game.model_dump_json()))

    def quota_until(self) -> float:
        row = self.db.execute("SELECT value FROM settings WHERE key='codex_quota_until'").fetchone()
        return float(row[0]) if row else 0

    def set_quota_until(self, timestamp: float) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('codex_quota_until', ?)", (str(timestamp),))

    def reserve_deepseek(self, request_limit: int, token_limit: int, tokens: int) -> bool:
        """请求前预留最坏输出量；失败或超时也不退回，避免未知账单被重复花费。"""
        day = datetime.now(timezone.utc).date().isoformat()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO usage VALUES (?, 0, 0)", (day,))
            result = self.db.execute(
                "UPDATE usage SET requests=requests+1, tokens=tokens+? WHERE day=? AND requests < ? AND tokens+? <= ?",
                (tokens, day, request_limit, tokens, token_limit),
            )
        return result.rowcount == 1

    def cached(self, key: str) -> Optional[Evidence]:
        row = self.db.execute("SELECT data FROM cache WHERE key=? AND expires>?", (key, time.time())).fetchone()
        return Evidence.model_validate_json(row[0]) if row else None

    def cache(self, key: str, evidence: Evidence, hours: int) -> None:
        if hours <= 0:
            return
        with self.db:
            self.db.execute("DELETE FROM cache WHERE expires<=?", (time.time(),))
            self.db.execute(
                "INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
                (key, time.time() + hours * 3600, evidence.model_dump_json()),
            )

    def stats(self) -> str:
        day = datetime.now(timezone.utc).date().isoformat()
        row = self.db.execute("SELECT requests, tokens FROM usage WHERE day=?", (day,)).fetchone()
        return json.dumps(
            {"utc_day": day, "deepseek_requests": row[0] if row else 0, "reserved_output_tokens": row[1] if row else 0},
            ensure_ascii=False,
        )

    def close(self) -> None:
        self.db.close()
