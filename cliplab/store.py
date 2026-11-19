import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ACTIVE = {
    "VALIDATING",
    "IMPORTING",
    "EXTRACTING_AUDIO",
    "TRANSCRIBING",
    "ANALYZING",
    "SELECTING",
    "RENDERING",
    "VERIFYING",
}
RETRYABLE = {"FAILED", "BLOCKED", "CANCELLED"}


class MissingRecord(LookupError):
    pass


class Conflict(ValueError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat()


def identity() -> str:
    return uuid4().hex


def clip_by_id(job: dict, clip_id: str) -> dict:
    for clip in job["clips"]:
        if clip["id"] == clip_id:
            return clip
    raise MissingRecord("Corte não encontrado.")


def revision_files(root: Path, clip: dict) -> tuple[Path, Path, Path]:
    stem = f"{clip['id']}-{clip['revision']}"
    return root / f"{stem}-preview.mp4", root / f"{stem}.mp4", root / f"{stem}.srt"


class Store:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                    state TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(state, created_at);
                CREATE TABLE IF NOT EXISTS music (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self, job: dict) -> dict:
        with self.connect() as db:
            db.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?)",
                (job["id"], job["created_at"], job["state"], json.dumps(job)),
            )
        return job

    def get(self, job_id: str) -> dict:
        with self.connect() as db:
            return self._get(db, job_id)

    @staticmethod
    def _get(db: sqlite3.Connection, job_id: str) -> dict:
        row = db.execute("SELECT payload FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise MissingRecord("Projeto não encontrado.")
        return json.loads(row[0])

    @staticmethod
    def _save(db: sqlite3.Connection, job: dict) -> None:
        db.execute(
            "UPDATE jobs SET state = ?, payload = ? WHERE id = ?",
            (job["state"], json.dumps(job), job["id"]),
        )

    def list_jobs(self) -> list[dict]:
        with self.connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute("SELECT payload FROM jobs ORDER BY created_at DESC")
            ]

    def update(self, job_id: str, change: Callable[[dict], None]) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self._get(db, job_id)
            change(job)
            self._save(db, job)
            return job

    def recover(self) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in ACTIVE)
            sql = f"SELECT payload FROM jobs WHERE state IN ({placeholders})"
            for (payload,) in db.execute(sql, tuple(ACTIVE)).fetchall():
                job = json.loads(payload)
                cancelled = job["cancel_requested"]
                job.update(
                    state="CANCELLED" if cancelled else "CREATED",
                    message="Processamento cancelado." if cancelled else "Retomada na fila.",
                    progress=0,
                    cancel_requested=False,
                )
                self._save(db, job)

    def next_work(self) -> tuple[str, str | None] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT id FROM jobs WHERE state = 'CREATED' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row:
                return row[0], None
            for (payload,) in db.execute(
                "SELECT payload FROM jobs WHERE state = 'READY_FOR_REVIEW' ORDER BY created_at"
            ):
                job = json.loads(payload)
                for clip in job["clips"]:
                    if clip["operation"]:
                        return job["id"], clip["id"]
        return None

    def add_music(self, track: dict) -> dict:
        with self.connect() as db:
            db.execute("INSERT INTO music VALUES (?, ?)", (track["id"], json.dumps(track)))
        return track

    def music(self, track_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM music WHERE id = ?", (track_id,)).fetchone()
            if row is None:
                raise MissingRecord("Trilha licenciada não encontrada.")
            return json.loads(row[0])

    def tracks(self) -> list[dict]:
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM music")]
