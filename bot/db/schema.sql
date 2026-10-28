PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    settings_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS instructions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('text','voice')),
    file_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('mold','texture','reference','voice','result')),
    path TEXT NOT NULL,
    tg_file_id TEXT,
    mime TEXT,
    sha256 TEXT NOT NULL,
    caption TEXT,
    analysis TEXT,  -- cached vision description (viewer phase), v2
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_files_user_kind ON files(user_id, kind);
CREATE INDEX IF NOT EXISTS idx_files_sha ON files(sha256);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(tg_id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT 'Проект',
    active_step_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    parent_step_id INTEGER REFERENCES steps(id),
    brief_text TEXT NOT NULL,
    plan_json TEXT,
    base_file_id INTEGER,
    winner_file_id INTEGER,
    quality_note INTEGER NOT NULL DEFAULT 0, -- 1 = winner below quality threshold
    disputed INTEGER NOT NULL DEFAULT 0,     -- 1 = close scores between top candidates
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_steps_project ON steps(project_id);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    step_id INTEGER NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES files(id),
    score_json TEXT,
    is_winner INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_candidates_step ON candidates(step_id);

CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,          -- chat | image | stt
    model TEXT NOT NULL,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    images INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_usage_user_day ON usage(user_id, created_at);
