"""SQLite repository (aiosqlite). WAL + foreign keys + parameterized queries only."""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from typing import Any

import aiosqlite

SCHEMA_PATH = pathlib.Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 2


@dataclass(slots=True)
class FileRow:
    id: int
    user_id: int
    kind: str
    path: str
    tg_file_id: str | None
    mime: str | None
    sha256: str
    caption: str | None
    created_at: str
    analysis: str | None = None  # cached vision description (viewer phase)


@dataclass(slots=True)
class StepRow:
    id: int
    project_id: int
    parent_step_id: int | None
    brief_text: str
    plan_json: str | None
    base_file_id: int | None
    winner_file_id: int | None
    quality_note: int
    disputed: int
    created_at: str


class Database:
    def __init__(self, path: str | pathlib.Path) -> None:
        self.path = str(path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database is not connected")
        return self._conn

    async def execute_script(self, script: str) -> None:
        await self.conn.executescript(script)

    # --- migrations -------------------------------------------------------
    async def migrate(self) -> None:
        c = self.conn
        await c.execute("PRAGMA journal_mode=WAL")
        await c.execute("PRAGMA foreign_keys=ON")
        await c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        cur = await c.execute("SELECT value FROM meta WHERE key='schema_version'")
        row = await cur.fetchone()
        # raw cursor returns a tuple (no row_factory): first column is the value
        current = int(row[0]) if row else 0
        if current < SCHEMA_VERSION:
            if current < 2:
                # v2: cached vision analysis of uploaded files
                cur2 = await c.execute("PRAGMA table_info(files)")
                cols = [r[1] for r in await cur2.fetchall()]
                if "analysis" not in cols:
                    await c.execute("ALTER TABLE files ADD COLUMN analysis TEXT")
            # future migrations go here; v1 is created by schema.sql itself
            await c.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
        await c.commit()

    # --- helpers ----------------------------------------------------------
    async def execute(self, sql: str, params: tuple | list = ()) -> int:
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur.lastrowid or 0

    async def fetchone(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        cur = await self.conn.execute(sql, params)
        row = await cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row, strict=False))

    async def fetchall(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        cur = await self.conn.execute(sql, params)
        rows = await cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=False)) for r in rows]

    # --- users ------------------------------------------------------------
    async def ensure_user(self, tg_id: int) -> None:
        await self.execute("INSERT OR IGNORE INTO users(tg_id) VALUES (?)", (tg_id,))

    async def get_settings(self, tg_id: int) -> dict[str, Any]:
        row = await self.fetchone("SELECT settings_json FROM users WHERE tg_id=?", (tg_id,))
        return json.loads(row["settings_json"]) if row else {}

    async def save_settings(self, tg_id: int, settings: dict[str, Any]) -> None:
        await self.ensure_user(tg_id)
        await self.execute(
            "UPDATE users SET settings_json=? WHERE tg_id=?",
            (json.dumps(settings, ensure_ascii=False), tg_id),
        )

    # --- instructions -----------------------------------------------------
    async def add_instruction(self, user_id: int, text: str, source: str, file_id: str | None = None) -> int:
        return await self.execute(
            "INSERT INTO instructions(user_id, text, source, file_id) VALUES (?,?,?,?)",
            (user_id, text, source, file_id),
        )

    async def list_instructions(self, user_id: int) -> list[dict[str, Any]]:
        return await self.fetchall(
            "SELECT id, text, source, file_id, created_at FROM instructions "
            "WHERE user_id=? ORDER BY id",
            (user_id,),
        )

    async def delete_instruction(self, user_id: int, instruction_id: int) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM instructions WHERE id=? AND user_id=?", (instruction_id, user_id)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def count_instructions(self, user_id: int) -> int:
        row = await self.fetchone(
            "SELECT COUNT(*) AS n FROM instructions WHERE user_id=?", (user_id,)
        )
        return int(row["n"]) if row else 0

    # --- files ------------------------------------------------------------
    async def add_file(
        self,
        user_id: int,
        kind: str,
        path: str,
        sha256: str,
        tg_file_id: str | None = None,
        mime: str | None = None,
        caption: str | None = None,
    ) -> int:
        return await self.execute(
            "INSERT INTO files(user_id, kind, path, tg_file_id, mime, sha256, caption) "
            "VALUES (?,?,?,?,?,?,?)",
            (user_id, kind, path, tg_file_id, mime, sha256, caption),
        )

    async def get_file(self, user_id: int, file_id: int) -> FileRow | None:
        row = await self.fetchone(
            "SELECT * FROM files WHERE id=? AND user_id=?", (file_id, user_id)
        )
        return FileRow(**row) if row else None

    async def find_file_by_sha(self, user_id: int, sha256: str, kind: str | None = None) -> FileRow | None:
        sql = "SELECT * FROM files WHERE user_id=? AND sha256=?"
        params: list[Any] = [user_id, sha256]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        row = await self.fetchone(sql + " ORDER BY id LIMIT 1", params)
        return FileRow(**row) if row else None

    async def list_files(self, user_id: int, kind: str | None = None, limit: int = 200) -> list[FileRow]:
        sql = "SELECT * FROM files WHERE user_id=?"
        params: list[Any] = [user_id]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [FileRow(**r) for r in await self.fetchall(sql, params)]

    async def delete_file(self, user_id: int, file_id: int) -> FileRow | None:
        row = await self.get_file(user_id, file_id)
        if not row:
            return None
        await self.execute("DELETE FROM files WHERE id=? AND user_id=?", (file_id, user_id))
        return row

    async def set_file_analysis(self, user_id: int, file_id: int, analysis: str) -> None:
        await self.execute(
            "UPDATE files SET analysis=? WHERE id=? AND user_id=?",
            (analysis, file_id, user_id),
        )

    async def set_file_caption(self, user_id: int, file_id: int, caption: str) -> None:
        await self.execute(
            "UPDATE files SET caption=? WHERE id=? AND user_id=?", (caption, file_id, user_id)
        )

    # --- projects / steps -------------------------------------------------
    async def ensure_project(self, user_id: int) -> int:
        row = await self.fetchone(
            "SELECT id FROM projects WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)
        )
        if row:
            return int(row["id"])
        return await self.execute(
            "INSERT INTO projects(user_id, title) VALUES (?, 'Проект')", (user_id,)
        )

    async def create_project(self, user_id: int) -> int:
        return await self.execute(
            "INSERT INTO projects(user_id, title) VALUES (?, 'Проект')", (user_id,)
        )

    async def get_active_project(self, user_id: int) -> int:
        """Latest project of the user; creates one if none."""
        return await self.ensure_project(user_id)

    async def set_active_step(self, project_id: int, step_id: int | None) -> None:
        await self.execute(
            "UPDATE projects SET active_step_id=? WHERE id=?", (step_id, project_id)
        )

    async def get_project(self, project_id: int) -> dict[str, Any] | None:
        return await self.fetchone("SELECT * FROM projects WHERE id=?", (project_id,))

    async def add_step(
        self,
        project_id: int,
        brief_text: str,
        plan_json: str | None,
        base_file_id: int | None,
        parent_step_id: int | None,
    ) -> StepRow:
        step_id = await self.execute(
            "INSERT INTO steps(project_id, parent_step_id, brief_text, plan_json, base_file_id) "
            "VALUES (?,?,?,?,?)",
            (project_id, parent_step_id, brief_text, plan_json, base_file_id),
        )
        await self.set_active_step(project_id, step_id)
        return await self.get_step(step_id)  # type: ignore[return-value]

    async def get_step(self, step_id: int) -> StepRow | None:
        row = await self.fetchone("SELECT * FROM steps WHERE id=?", (step_id,))
        return StepRow(**row) if row else None

    async def list_steps(self, project_id: int) -> list[StepRow]:
        rows = await self.fetchall(
            "SELECT * FROM steps WHERE project_id=? ORDER BY id", (project_id,)
        )
        return [StepRow(**r) for r in rows]

    async def get_active_step(self, project_id: int) -> StepRow | None:
        row = await self.fetchone(
            "SELECT s.* FROM steps s JOIN projects p ON p.active_step_id=s.id WHERE p.id=?",
            (project_id,),
        )
        return StepRow(**row) if row else None

    async def finish_step(
        self,
        step_id: int,
        winner_file_id: int | None,
        quality_note: int = 0,
        disputed: int = 0,
    ) -> None:
        await self.execute(
            "UPDATE steps SET winner_file_id=?, quality_note=?, disputed=? WHERE id=?",
            (winner_file_id, quality_note, disputed, step_id),
        )

    # --- candidates -------------------------------------------------------
    async def add_candidate(
        self, step_id: int, file_id: int, score_json: str | None = None, is_winner: bool = False
    ) -> int:
        return await self.execute(
            "INSERT INTO candidates(step_id, file_id, score_json, is_winner) VALUES (?,?,?,?)",
            (step_id, file_id, score_json, int(is_winner)),
        )

    async def list_candidates(self, step_id: int) -> list[dict[str, Any]]:
        return await self.fetchall(
            "SELECT c.*, f.path, f.kind FROM candidates c JOIN files f ON f.id=c.file_id "
            "WHERE c.step_id=? ORDER BY c.id",
            (step_id,),
        )

    # --- usage ------------------------------------------------------------
    async def log_usage(
        self,
        user_id: int,
        kind: str,
        model: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        images: int = 0,
    ) -> None:
        await self.execute(
            "INSERT INTO usage(user_id, kind, model, tokens_in, tokens_out, images) "
            "VALUES (?,?,?,?,?,?)",
            (user_id, kind, model, tokens_in, tokens_out, images),
        )

    async def images_today(self, user_id: int) -> int:
        row = await self.fetchone(
            "SELECT COALESCE(SUM(images),0) AS n FROM usage "
            "WHERE user_id=? AND kind='image' AND created_at >= datetime('now', 'start of day')",
            (user_id,),
        )
        return int(row["n"]) if row else 0
