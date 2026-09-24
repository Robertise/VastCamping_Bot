"""Lưu thiết lập riêng của từng người dùng Telegram trong SQLite."""
import json
import sqlite3
from dataclasses import dataclass, field


@dataclass
class User:
    chat_id: int
    gpus: list[str] = field(default_factory=list)
    paused: bool = False
    max_price: float | None = None
    last_seen: set[str] = field(default_factory=set)


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                chat_id   INTEGER PRIMARY KEY,
                gpus      TEXT NOT NULL DEFAULT '[]',
                paused    INTEGER NOT NULL DEFAULT 0,
                max_price REAL,
                last_seen TEXT NOT NULL DEFAULT '[]'
            )"""
        )
        self.conn.commit()

    def _row_to_user(self, row) -> User:
        return User(
            chat_id=row[0],
            gpus=json.loads(row[1]),
            paused=bool(row[2]),
            max_price=row[3],
            last_seen=set(json.loads(row[4])),
        )

    def get(self, chat_id: int) -> User:
        row = self.conn.execute(
            "SELECT chat_id, gpus, paused, max_price, last_seen FROM users WHERE chat_id=?",
            (chat_id,),
        ).fetchone()
        if row is None:
            self.conn.execute("INSERT INTO users (chat_id) VALUES (?)", (chat_id,))
            self.conn.commit()
            return User(chat_id=chat_id)
        return self._row_to_user(row)

    def all(self) -> list[User]:
        rows = self.conn.execute(
            "SELECT chat_id, gpus, paused, max_price, last_seen FROM users"
        ).fetchall()
        return [self._row_to_user(r) for r in rows]

    def save(self, u: User):
        self.conn.execute(
            # last_seen chỉ ghi qua set_last_seen() để lệnh người dùng không
            # ghi đè kết quả của vòng kiểm tra đang chạy song song.
            """INSERT INTO users (chat_id, gpus, paused, max_price)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET
                 gpus=excluded.gpus, paused=excluded.paused,
                 max_price=excluded.max_price""",
            (u.chat_id, json.dumps(u.gpus), int(u.paused), u.max_price),
        )
        self.conn.commit()

    def set_last_seen(self, chat_id: int, keys: set[str]):
        """Chỉ cập nhật last_seen, không ghi đè thiết lập người dùng vừa đổi."""
        self.conn.execute("UPDATE users SET last_seen=? WHERE chat_id=?",
                          (json.dumps(sorted(keys)), chat_id))
        self.conn.commit()
