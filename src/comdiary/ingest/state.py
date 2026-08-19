"""Ingest bookkeeping + the search index, in one SQLite file.

Everything here is *derived*: `comdiary reindex` rebuilds it from the meeting
JSONs, so the database can be deleted at any time without losing anything.
That is why it lives under ``.comdiary/`` and is gitignored.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from ..models import Meeting
from ..util import ensure_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    sha256      TEXT PRIMARY KEY,
    path        TEXT NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL,          -- ok | failed | skipped
    meeting_id  TEXT,
    meeting_doc TEXT,
    error       TEXT,
    ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meetings (
    meeting_id TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    date       TEXT NOT NULL,
    doc        TEXT NOT NULL,
    json_path  TEXT NOT NULL,
    projects   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS segments (
    meeting_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    project_id TEXT,
    title      TEXT NOT NULL,
    summary    TEXT NOT NULL DEFAULT '',
    date       TEXT NOT NULL,
    PRIMARY KEY (meeting_id, segment_id)
);

CREATE TABLE IF NOT EXISTS signals (
    meeting_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    project_id TEXT,
    date       TEXT NOT NULL,
    speaker    TEXT,
    kind       TEXT NOT NULL,
    topic      TEXT NOT NULL,
    intensity  TEXT NOT NULL,
    evidence   TEXT NOT NULL DEFAULT '',
    quote      TEXT,
    concern    TEXT,
    confidence REAL NOT NULL DEFAULT 0.5
);
CREATE INDEX IF NOT EXISTS idx_signals_speaker ON signals(speaker);
CREATE INDEX IF NOT EXISTS idx_signals_project ON signals(project_id);

CREATE TABLE IF NOT EXISTS items (
    meeting_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    project_id TEXT,
    date       TEXT NOT NULL,
    item_type  TEXT NOT NULL,           -- decision | action | question | risk | agenda
    text       TEXT NOT NULL,
    owner      TEXT,
    due        TEXT,
    status     TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_type ON items(item_type, project_id);

CREATE TABLE IF NOT EXISTS segment_topics (
    meeting_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    project_id TEXT,
    date       TEXT NOT NULL,
    topic      TEXT NOT NULL,
    PRIMARY KEY (meeting_id, segment_id, topic)
);
CREATE INDEX IF NOT EXISTS idx_segment_topics ON segment_topics(topic);

CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
    body, title, project_id UNINDEXED, meeting_id UNINDEXED,
    segment_id UNINDEXED, date UNINDEXED, doc UNINDEXED, tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    processed  INTEGER NOT NULL DEFAULT 0,
    failed     INTEGER NOT NULL DEFAULT 0,
    note       TEXT
);
"""


class State:
    def __init__(self, path: Path) -> None:
        ensure_dir(path.parent)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.executescript(SCHEMA)
        except sqlite3.OperationalError:
            # trigram needs SQLite >= 3.34. Without it, Japanese substring search
            # degrades, so fall back to unicode61 and let `search` use LIKE.
            self.conn.executescript(SCHEMA.replace("tokenize='trigram'", "tokenize='unicode61'"))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> State:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- source bookkeeping ----------------------------------------------
    def seen(self, sha256: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM sources WHERE sha256 = ?", (sha256,)).fetchone()

    def is_done(self, sha256: str) -> bool:
        row = self.seen(sha256)
        return bool(row and row["status"] == "ok")

    def record_source(
        self,
        sha256: str,
        path: Path,
        name: str,
        kind: str,
        status: str,
        meeting_id: str | None = None,
        meeting_doc: str | None = None,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO sources (sha256, path, name, kind, status, meeting_id, meeting_doc, error,"
            " ingested_at) VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(sha256) DO UPDATE SET path=excluded.path, status=excluded.status,"
            " meeting_id=excluded.meeting_id, meeting_doc=excluded.meeting_doc,"
            " error=excluded.error, ingested_at=excluded.ingested_at",
            (
                sha256,
                str(path),
                name,
                kind,
                status,
                meeting_id,
                meeting_doc,
                error,
                datetime.now().astimezone().isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()

    def failures(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM sources WHERE status = 'failed' ORDER BY ingested_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    # -- runs -------------------------------------------------------------
    def start_run(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at) VALUES (?)",
            (datetime.now().astimezone().isoformat(timespec="seconds"),),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def end_run(self, run_id: int, processed: int, failed: int, note: str = "") -> None:
        self.conn.execute(
            "UPDATE runs SET ended_at=?, processed=?, failed=?, note=? WHERE id=?",
            (
                datetime.now().astimezone().isoformat(timespec="seconds"),
                processed,
                failed,
                note,
                run_id,
            ),
        )
        self.conn.commit()

    # -- indexing ---------------------------------------------------------
    def forget_meeting(self, meeting_id: str) -> None:
        for table in ("meetings", "segments", "signals", "items", "segment_topics"):
            self.conn.execute(f"DELETE FROM {table} WHERE meeting_id = ?", (meeting_id,))
        self.conn.execute("DELETE FROM docs WHERE meeting_id = ?", (meeting_id,))

    def index_meeting(self, meeting: Meeting, doc: str, json_path: str) -> None:
        self.forget_meeting(meeting.meeting_id)
        date = meeting.date.isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT INTO meetings (meeting_id, title, kind, date, doc, json_path, projects)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                meeting.meeting_id,
                meeting.title,
                meeting.kind,
                date,
                doc,
                json_path,
                ",".join(meeting.project_ids()),
            ),
        )
        self.conn.execute(
            "INSERT INTO docs (body, title, project_id, meeting_id, segment_id, date, doc)"
            " VALUES (?,?,?,?,?,?,?)",
            (meeting.summary, meeting.title, "", meeting.meeting_id, "", date, doc),
        )
        for seg in meeting.segments:
            self.conn.execute(
                "INSERT INTO segments (meeting_id, segment_id, project_id, title, summary, date)"
                " VALUES (?,?,?,?,?,?)",
                (meeting.meeting_id, seg.segment_id, seg.project_id, seg.title, seg.summary, date),
            )
            body = "\n".join(
                [seg.title, seg.summary, " ".join(seg.topics)]
                + [d.what for d in seg.decisions]
                + [a.what for a in seg.actions]
                + [q.question for q in seg.open_questions]
                + [r.what for r in seg.risks]
                + [f"{s.topic} {s.evidence} {s.concern or ''}" for s in seg.signals]
                + seg.next_agenda
            )
            self.conn.execute(
                "INSERT INTO docs (body, title, project_id, meeting_id, segment_id, date, doc)"
                " VALUES (?,?,?,?,?,?,?)",
                (body, seg.title, seg.project_id or "", meeting.meeting_id, seg.segment_id, date, doc),
            )
            for topic in dict.fromkeys(t.strip() for t in seg.topics if t.strip()):
                self.conn.execute(
                    "INSERT OR IGNORE INTO segment_topics"
                    " (meeting_id, segment_id, project_id, date, topic) VALUES (?,?,?,?,?)",
                    (meeting.meeting_id, seg.segment_id, seg.project_id, date, topic),
                )
            for sig in seg.signals:
                self.conn.execute(
                    "INSERT INTO signals (meeting_id, segment_id, project_id, date, speaker, kind,"
                    " topic, intensity, evidence, quote, concern, confidence)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        meeting.meeting_id,
                        seg.segment_id,
                        seg.project_id,
                        date,
                        sig.speaker,
                        sig.kind,
                        sig.topic,
                        sig.intensity,
                        sig.evidence,
                        sig.quote,
                        sig.concern,
                        sig.confidence,
                    ),
                )
            rows: list[tuple] = []
            rows += [("decision", d.what, None, None, None) for d in seg.decisions]
            rows += [("action", a.what, a.owner, a.due, a.status) for a in seg.actions]
            rows += [("question", q.question, q.raised_by, None, q.status) for q in seg.open_questions]
            rows += [("risk", r.what, r.raised_by, None, r.impact) for r in seg.risks]
            rows += [("agenda", n, None, None, None) for n in seg.next_agenda]
            for item_type, text, owner, due, status in rows:
                self.conn.execute(
                    "INSERT INTO items (meeting_id, segment_id, project_id, date, item_type, text,"
                    " owner, due, status) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        meeting.meeting_id,
                        seg.segment_id,
                        seg.project_id,
                        date,
                        item_type,
                        text,
                        owner,
                        due,
                        status,
                    ),
                )
        self.conn.commit()

    # -- queries ----------------------------------------------------------
    def search(
        self, query: str, project_id: str | None = None, since: str | None = None, limit: int = 20
    ) -> list[sqlite3.Row]:
        # The trigram tokenizer cannot match fewer than 3 characters, and short
        # Japanese queries ("納期") are exactly the common case — use LIKE there.
        if len(query.strip()) < 3 or any(c in query for c in '"*'):
            return self._search_like(query, project_id, since, limit)
        try:
            return self._search_fts(query, project_id, since, limit)
        except sqlite3.OperationalError:
            return self._search_like(query, project_id, since, limit)

    def _search_like(
        self, query: str, project_id: str | None, since: str | None, limit: int
    ) -> list[sqlite3.Row]:
        sql = [
            "SELECT title, project_id, meeting_id, segment_id, date, doc,",
            " substr(body, 1, 200) AS snippet FROM docs",
            " WHERE (body LIKE ? OR title LIKE ?)",
        ]
        like = f"%{query.strip()}%"
        params: list[object] = [like, like]
        if project_id:
            sql.append(" AND project_id = ?")
            params.append(project_id)
        if since:
            sql.append(" AND date >= ?")
            params.append(since)
        sql.append(" ORDER BY date DESC LIMIT ?")
        params.append(limit)
        return self.conn.execute("".join(sql), params).fetchall()

    def _search_fts(
        self, query: str, project_id: str | None, since: str | None, limit: int
    ) -> list[sqlite3.Row]:
        sql = [
            "SELECT title, project_id, meeting_id, segment_id, date, doc,",
            " snippet(docs, 0, '»', '«', '…', 20) AS snippet",
            " FROM docs WHERE docs MATCH ?",
        ]
        params: list[object] = [query]
        if project_id:
            sql.append(" AND project_id = ?")
            params.append(project_id)
        if since:
            sql.append(" AND date >= ?")
            params.append(since)
        sql.append(" ORDER BY date DESC LIMIT ?")
        params.append(limit)
        return self.conn.execute("".join(sql), params).fetchall()

    def open_items(
        self, item_type: str, project_id: str | None = None, limit: int = 100
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM items WHERE item_type = ? AND (status IS NULL OR status = 'open')"
        params: list[object] = [item_type]
        if project_id:
            sql += " AND project_id = ?"
            params.append(project_id)
        sql += " ORDER BY date DESC LIMIT ?"
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def concerns(
        self,
        speaker: str | None = None,
        project_id: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM signals WHERE 1=1"
        params: list[object] = []
        if speaker:
            sql += " AND speaker = ?"
            params.append(speaker)
        if project_id:
            sql += " AND project_id = ?"
            params.append(project_id)
        if since:
            sql += " AND date >= ?"
            params.append(since)
        sql += " ORDER BY date DESC LIMIT ?"
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def recent_segments(
        self, project_id: str, limit: int = 10
    ) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT s.*, m.title AS meeting_title, m.doc AS meeting_doc FROM segments s"
            " JOIN meetings m ON m.meeting_id = s.meeting_id"
            " WHERE s.project_id = ? ORDER BY s.date DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()

    def known_topics(self, limit: int = 40) -> list[str]:
        """Topics already in use, most frequent first.

        Fed back into the extraction prompt so the vocabulary converges instead
        of fragmenting into 納期 / スケジュール / 期日 — aggregation across
        meetings only works if the same idea keeps the same label.
        """
        rows = self.conn.execute(
            "SELECT topic, COUNT(*) AS c FROM segment_topics"
            " GROUP BY topic ORDER BY c DESC, topic LIMIT ?",
            (limit,),
        ).fetchall()
        return [r["topic"] for r in rows]

    def topic_rows(self, since: str | None = None, project_id: str | None = None):
        """Every (segment, topic) pair with the context needed to rank it."""
        sql = [
            "SELECT t.topic, t.project_id, t.meeting_id, t.segment_id, t.date,",
            " m.title AS meeting_title, m.doc AS meeting_doc, s.title AS segment_title,",
            " s.summary AS segment_summary",
            " FROM segment_topics t",
            " JOIN meetings m ON m.meeting_id = t.meeting_id",
            " JOIN segments s ON s.meeting_id = t.meeting_id AND s.segment_id = t.segment_id",
            " WHERE 1=1",
        ]
        params: list[object] = []
        if since:
            sql.append(" AND t.date >= ?")
            params.append(since)
        if project_id:
            sql.append(" AND t.project_id = ?")
            params.append(project_id)
        sql.append(" ORDER BY t.date DESC")
        return self.conn.execute("".join(sql), params).fetchall()

    def signals_by_segment(self, since: str | None = None):
        sql = "SELECT * FROM signals WHERE 1=1"
        params: list[object] = []
        if since:
            sql += " AND date >= ?"
            params.append(since)
        return self.conn.execute(sql, params).fetchall()

    def open_questions_by_segment(self):
        return self.conn.execute(
            "SELECT meeting_id, segment_id, text FROM items"
            " WHERE item_type = 'question' AND (status IS NULL OR status = 'open')"
        ).fetchall()

    def stats(self) -> dict[str, int]:
        out = {}
        for table in ("meetings", "segments", "signals", "items", "segment_topics", "sources"):
            out[table] = int(
                self.conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            )
        return out
