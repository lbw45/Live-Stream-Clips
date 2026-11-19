#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite 数据库层：连接管理 + 版本化 migration。

对应文档 §6.2 数据模型、§7.4 启动编排。

设计要点：
- WAL + busy_timeout 30s + 外键约束（对齐原版）；
- **版本化 migration**：``schema_migrations`` 表逐版本执行，只增不改；
- 所有表结构已按 §6.2「照抄原版列名」一次性建好，避免后续频繁改 schema；
- 连接不跨线程复用（sqlite3 默认限制），每次操作开新连接，成本可忽略。
"""
from __future__ import annotations

import contextlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.common.paths import ws

# ---------------------------------------------------------------- 时间

def utcnow() -> str:
    """统一的 ISO8601 UTC 时间戳（可排序、可直接入库）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------- migration

# 每个元素：(版本号, 说明, SQL 语句列表)。新增一律追加，禁止修改已发布版本。
MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (
        1,
        "核心表结构",
        [
            # --- 项目：一次素材导入/一次直播录制 = 一个项目 ---
            """
            CREATE TABLE IF NOT EXISTS projects (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                name              TEXT    NOT NULL,
                source_dir        TEXT    NOT NULL DEFAULT '',
                manifest_path     TEXT,
                status            TEXT    NOT NULL DEFAULT 'discovered',
                -- 就绪度：writing / ready / missing_subtitles / missing_scores / invalid
                readiness         TEXT    NOT NULL DEFAULT 'writing',
                candidate_count   INTEGER NOT NULL DEFAULT 0,
                eligible_count    INTEGER NOT NULL DEFAULT 0,
                top_score         REAL    NOT NULL DEFAULT 0,
                source_srt_path   TEXT,
                source_video_path TEXT,
                -- 来源：imported（手动导入）/ watched（目录监控发现）
                import_kind       TEXT    NOT NULL DEFAULT 'imported',
                source_signature  TEXT,           -- 文件指纹，用于去重
                discovered_at     TEXT    NOT NULL,
                updated_at        TEXT    NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status)",
            "CREATE INDEX IF NOT EXISTS idx_projects_signature ON projects(source_signature)",
            # --- 切片：LLM 切出来的一条候选 ---
            """
            CREATE TABLE IF NOT EXISTS clips (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id               INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                idx                      INTEGER NOT NULL DEFAULT 0,
                title                    TEXT    NOT NULL DEFAULT '',
                category                 TEXT    NOT NULL DEFAULT '',
                summary                  TEXT    NOT NULL DEFAULT '',
                keywords                 TEXT    NOT NULL DEFAULT '',   -- 逗号分隔
                start_sec                REAL    NOT NULL DEFAULT 0,
                duration_sec             REAL    NOT NULL DEFAULT 0,
                duration_ms              INTEGER NOT NULL DEFAULT 0,
                file_name                TEXT,
                source_path              TEXT,
                file_size_bytes          INTEGER NOT NULL DEFAULT 0,
                subtitle_path            TEXT,
                -- 四维评分
                overall_score            REAL    NOT NULL DEFAULT 0,
                value_score              REAL    NOT NULL DEFAULT 0,
                comedy_score             REAL    NOT NULL DEFAULT 0,
                completeness_score       REAL    NOT NULL DEFAULT 0,
                traffic_level            TEXT    NOT NULL DEFAULT '',
                suggested_title          TEXT,
                douyin_tags              TEXT    NOT NULL DEFAULT '',   -- JSON 数组
                hook_placement           TEXT,                          -- JSON
                best_duration            TEXT,
                editing_tips             TEXT,                          -- JSON 数组
                traffic_reason           TEXT,
                game_voiceover_suitable  INTEGER,                       -- 0/1，NULL=未评估
                game_voiceover_reason    TEXT,
                suggested_background_tag TEXT,
                core_risk                TEXT,
                next_action              TEXT,
                publish_priority         INTEGER NOT NULL DEFAULT 0,
                recommendation_reason    TEXT,
                created_at               TEXT    NOT NULL,
                updated_at               TEXT    NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_clips_project ON clips(project_id)",
            "CREATE INDEX IF NOT EXISTS idx_clips_score ON clips(overall_score DESC)",
            # --- 任务：一次剪辑任务 ---
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id         INTEGER REFERENCES projects(id) ON DELETE SET NULL,
                clip_id            INTEGER REFERENCES clips(id) ON DELETE SET NULL,
                strategy           TEXT    NOT NULL DEFAULT 'conservative',  -- conservative / aggressive
                status             TEXT    NOT NULL DEFAULT 'queued',
                stage              TEXT    NOT NULL DEFAULT 'queued',
                progress           REAL    NOT NULL DEFAULT 0,
                error_message      TEXT,
                max_outputs        INTEGER NOT NULL DEFAULT 3,
                score_threshold    REAL    NOT NULL DEFAULT 80,
                checkpoints        TEXT,                -- JSON
                input_fingerprints TEXT,                -- JSON：输入指纹，决定 checkpoint 是否失效
                retry_count        INTEGER NOT NULL DEFAULT 0,
                pause_requested    INTEGER NOT NULL DEFAULT 0,
                cancel_requested   INTEGER NOT NULL DEFAULT 0,
                resume_stage       TEXT,
                config_version     TEXT    NOT NULL DEFAULT '1',
                created_at         TEXT    NOT NULL,
                updated_at         TEXT    NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
            # --- 阶段检查点：断点续跑核心 ---
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id      INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                stage        TEXT    NOT NULL,
                fingerprint  TEXT    NOT NULL,
                payload_json TEXT,
                valid        INTEGER NOT NULL DEFAULT 1,
                created_at   TEXT    NOT NULL,
                UNIQUE(task_id, stage)
            )
            """,
            # --- 剪辑决策：keepRanges + hook 前置 ---
            """
            CREATE TABLE IF NOT EXISTS edit_decisions (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id             INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                clip_id             INTEGER REFERENCES clips(id) ON DELETE SET NULL,
                strategy            TEXT    NOT NULL DEFAULT 'conservative',
                hook_json           TEXT,
                keep_ranges_json    TEXT,
                subtitle_cues_json  TEXT,
                planner             TEXT    NOT NULL DEFAULT 'ai',   -- manual / ai / local_fallback
                planner_confidence  REAL    NOT NULL DEFAULT 0,
                visual_mode         TEXT    NOT NULL DEFAULT 'original',
                cover_title         TEXT,
                created_at          TEXT    NOT NULL,
                updated_at          TEXT    NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_edit_decisions_task ON edit_decisions(task_id)",
            # --- 产物 ---
            """
            CREATE TABLE IF NOT EXISTS artifacts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                kind       TEXT    NOT NULL,          -- video / cover / subtitle / audio
                local_path TEXT    NOT NULL,
                version    INTEGER NOT NULL DEFAULT 1,
                mime_type  TEXT,
                byte_size  INTEGER NOT NULL DEFAULT 0,
                created_at TEXT    NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id)",
            # --- 封面 ---
            """
            CREATE TABLE IF NOT EXISTS cover_concepts (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id           INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                clip_id           INTEGER REFERENCES clips(id) ON DELETE SET NULL,
                source_frame_ms   INTEGER NOT NULL DEFAULT 0,
                title             TEXT,
                prompt            TEXT,
                variant           INTEGER NOT NULL DEFAULT 0,
                generation_source TEXT    NOT NULL DEFAULT 'local',   -- local / ai
                generation_error  TEXT,
                created_at        TEXT    NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS cover_assets (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                concept_id   INTEGER NOT NULL REFERENCES cover_concepts(id) ON DELETE CASCADE,
                aspect_ratio TEXT    NOT NULL,          -- 3:4 / 4:3
                local_path   TEXT    NOT NULL,
                created_at   TEXT    NOT NULL
            )
            """,
            # --- 音乐素材 ---
            """
            CREATE TABLE IF NOT EXISTS music_tracks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                file_name   TEXT    NOT NULL,
                title       TEXT    NOT NULL DEFAULT '',
                duration_ms INTEGER NOT NULL DEFAULT 0,
                tags        TEXT    NOT NULL DEFAULT '',
                enabled     INTEGER NOT NULL DEFAULT 1,
                usage_count INTEGER NOT NULL DEFAULT 0,
                file_path   TEXT    NOT NULL,
                created_at  TEXT    NOT NULL,
                UNIQUE(file_path)
            )
            """,
            # --- 发布任务（D8/D15：止于上传+填表）---
            """
            CREATE TABLE IF NOT EXISTS publish_jobs (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                client_request_id      TEXT    NOT NULL UNIQUE,   -- 幂等键
                task_id                INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
                artifact_id            INTEGER REFERENCES artifacts(id) ON DELETE SET NULL,
                artifact_version       INTEGER NOT NULL DEFAULT 1,
                platform               TEXT    NOT NULL DEFAULT 'douyin',
                status                 TEXT    NOT NULL DEFAULT 'queued',
                phase                  TEXT    NOT NULL DEFAULT 'preflight',
                publish_mode           TEXT    NOT NULL DEFAULT 'manual',
                title                  TEXT    NOT NULL DEFAULT '',
                tags_json              TEXT    NOT NULL DEFAULT '[]',
                account_display_name   TEXT,
                file_name              TEXT,
                file_path              TEXT,
                file_size_bytes        INTEGER NOT NULL DEFAULT 0,
                attempt_count          INTEGER NOT NULL DEFAULT 0,
                retry_of_job_id        INTEGER REFERENCES publish_jobs(id) ON DELETE SET NULL,
                remote_item_id         TEXT,
                remote_url             TEXT,
                error_code             TEXT,
                error_message          TEXT,
                submit_started_at      TEXT,
                created_at             TEXT    NOT NULL,
                updated_at             TEXT    NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_publish_status ON publish_jobs(status)",
            # --- 偏好设置（键值对）---
            """
            CREATE TABLE IF NOT EXISTS preferences (
                key        TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        ],
    ),
    (
        4,
        "发布队列记录原始标题/标签（v3.56 重置/清除用）",
        [
            # 用户 2026-09-26：发布队列要能「重置回原本的标题标签」——
            # 入队时把自动生成的 title/tags 另存一份，用户改坏了能一键还原。
            "ALTER TABLE publish_jobs ADD COLUMN original_title TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE publish_jobs ADD COLUMN original_tags_json TEXT NOT NULL DEFAULT '[]'",
            # 迁移前已在队列里的老数据：原始值 = 现值（那会儿还没人改过，等价于"入队时"）。
            "UPDATE publish_jobs SET original_title = title WHERE original_title = ''",
            "UPDATE publish_jobs SET original_tags_json = tags_json WHERE original_tags_json = '[]'",
        ],
    ),
    (
        3,
        "发布队列补成片时长（v3.18）",
        [
            # 用户 2026-09-23：待发布队列要能看到成片时长。
            # 入队时从成片伴生 `.edit.json` 的 durationSec 带过来（渲染时写好的真实时长）。
            "ALTER TABLE publish_jobs ADD COLUMN duration_sec REAL NOT NULL DEFAULT 0",
        ],
    ),
    (
        2,
        "发布队列补充字段（M5-A）",
        [
            # 发布队列要「按评分排序」（A8），但 v1 的 publish_jobs 没有评分列；
            # 入队时从候选产物带过来的四维评分 overall 存在这里。
            "ALTER TABLE publish_jobs ADD COLUMN publish_score REAL NOT NULL DEFAULT 0",
        ],
    ),
    (
        5,
        "发布队列记录来源（v3.77）",
        [
            # 存下来源才知道该去哪个目录找文件、该用哪个前缀拼媒体流地址。
            # 目前只有剪辑成片一种来源 ⇒ 默认值 'edit'。
            "ALTER TABLE publish_jobs ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'edit'",
        ],
    ),
]


# ---------------------------------------------------------------- 连接

def db_path() -> Path:
    return ws.db_path


def connect(path: Path | None = None) -> sqlite3.Connection:
    """打开连接并应用统一 PRAGMA。调用方负责关闭（建议用 :func:`session`）。"""
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextlib.contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """事务化连接：正常退出提交，异常回滚。"""
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- migration

def _ensure_migration_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER PRIMARY KEY,
            name       TEXT    NOT NULL,
            applied_at TEXT    NOT NULL
        )
        """
    )


def current_version(conn: sqlite3.Connection) -> int:
    _ensure_migration_table(conn)
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations").fetchone()
    return int(row["v"])


def migrate(path: Path | None = None, *, verbose: bool = False) -> dict:
    """把数据库升到最新版本。幂等，可重复调用。返回执行摘要。"""
    applied: list[int] = []
    with session(path) as conn:
        _ensure_migration_table(conn)
        have = current_version(conn)
        for version, name, statements in MIGRATIONS:
            if version <= have:
                continue
            for sql in statements:
                conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, utcnow()),
            )
            applied.append(version)
            if verbose:
                print(f"[migrate] v{version} {name} 已应用", flush=True)
        final = current_version(conn)
    return {
        "from": have,
        "to": final,
        "applied": applied,
        "up_to_date": not applied,
    }


def health(path: Path | None = None) -> dict:
    """数据库健康快照，供 /api/editor/health 使用。"""
    target = path or db_path()
    info: dict = {
        "path": str(target),
        "exists": target.is_file(),
        "version": 0,
        "tables": 0,
        "error": None,
    }
    if not info["exists"]:
        return info
    try:
        with session(target) as conn:
            info["version"] = current_version(conn)
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()
            info["tables"] = int(row["n"])
    except sqlite3.Error as exc:
        info["error"] = str(exc)
    return info


def table_names(path: Path | None = None) -> list[str]:
    with session(path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
    return [r["name"] for r in rows]


def initialize(verbose: bool = False) -> dict:
    """建目录 + 跑 migration。服务启动时调用一次。"""
    ws.ensure_dirs()
    return migrate(verbose=verbose)
