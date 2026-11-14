"""Recoverable batch drafts and an explicit record of local exports/transfers."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from batch_fit import BEIJING, DEFAULT_PERIOD, PERIODS, ScheduledRun, validate_run
from campus import ROUTES, RunPlan


@dataclass(frozen=True)
class BatchDraft:
    route_id: str
    plan: RunPlan
    time_seed: int
    dates_text: str = ""
    period: str = DEFAULT_PERIOD
    custom: tuple[str, str] = ("14:00", "21:30")
    evening_percent: int = 50
    weekdays: tuple[int, ...] = tuple(range(7))
    exclusions: str = ""
    rows: tuple[ScheduledRun, ...] = ()
    pending_days: tuple[str, ...] = ()

    def encode(self) -> str:
        value = asdict(self)
        value["version"] = 1
        for row in value["rows"]:
            row["start"], row["end"] = row["start"].isoformat(), row["end"].isoformat()
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    @classmethod
    def decode(cls, text: str) -> "BatchDraft":
        try:
            data = json.loads(text)
            if data.pop("version") != 1:
                raise ValueError("不支持的批次版本。")
            data["plan"] = RunPlan(**data["plan"])
            rows = []
            for item in data["rows"]:
                item["start"] = datetime.fromisoformat(item["start"])
                item["end"] = datetime.fromisoformat(item["end"])
                if item["plan"] is not None:
                    item["plan"] = RunPlan(**item["plan"])
                row = ScheduledRun(**item)
                validate_run(row, data["plan"])
                rows.append(row)
            data["rows"] = tuple(rows)
            data["custom"], data["weekdays"] = tuple(data["custom"]), tuple(data["weekdays"])
            data["pending_days"] = tuple(data.get("pending_days", ()))
            draft = cls(**data)
            if (draft.route_id not in ROUTES or draft.period not in (*PERIODS, "自定义")
                    or not 0 <= draft.evening_percent <= 100 or len(draft.custom) != 2
                    or any(day not in range(7) for day in draft.weekdays)
                    or not isinstance(draft.time_seed, int) or draft.time_seed < 0):
                raise ValueError("批次设置无效。")
            RunPlan.parse(str(draft.plan.meters/1000),
                          f"{draft.plan.seconds_per_km//60}:{draft.plan.seconds_per_km%60:02}",
                          draft.plan.clockwise, draft.plan.seed, draft.plan.random_start)
            if len({row.start.date() for row in rows}) != len(rows):
                raise ValueError("草稿中有重复日期。")
            return draft
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise ValueError(f"无法读取批次草稿：{error}") from error


class BatchStore:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS draft (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS exports (
                    id INTEGER PRIMARY KEY, created TEXT NOT NULL, kind TEXT NOT NULL,
                    target TEXT NOT NULL, digest TEXT NOT NULL, count INTEGER NOT NULL,
                    body TEXT NOT NULL, UNIQUE(kind, target, digest));
                CREATE TABLE IF NOT EXISTS exported_dates (
                    export_id INTEGER NOT NULL, day TEXT NOT NULL, PRIMARY KEY(export_id, day));
                CREATE INDEX IF NOT EXISTS by_day ON exported_dates(day);
            """)
            with db:
                yield db
        finally:
            db.close()

    def save_draft(self, draft: BatchDraft):
        with self.connection() as db:
            db.execute("INSERT INTO draft VALUES(1,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                       (draft.encode(),))

    def load_draft(self) -> BatchDraft | None:
        with self.connection() as db:
            row = db.execute("SELECT body FROM draft WHERE id=1").fetchone()
        return BatchDraft.decode(row[0]) if row else None

    def record_export(self, path: Path, kind: str, target: str, draft: BatchDraft,
                      runs: tuple[ScheduledRun, ...]):
        if kind not in ("保存 ZIP", "发送到雷电"):
            raise ValueError("未知导出类型。")
        digest = sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024*1024), b""):
                digest.update(chunk)
        with self.connection() as db:
            values = (kind, target, digest.hexdigest())
            db.execute("""INSERT INTO exports(created,kind,target,digest,count,body) VALUES(?,?,?,?,?,?)
                          ON CONFLICT(kind,target,digest) DO UPDATE SET created=excluded.created, body=excluded.body""",
                       (datetime.now(BEIJING).isoformat(timespec="seconds"), *values, len(runs), draft.encode()))
            identity = db.execute("SELECT id FROM exports WHERE kind=? AND target=? AND digest=?", values).fetchone()[0]
            db.executemany("INSERT OR IGNORE INTO exported_dates VALUES(?,?)",
                           ((identity, row.start.date().isoformat()) for row in runs))

    def exported_dates(self) -> set[str]:
        with self.connection() as db:
            return {row[0] for row in db.execute("SELECT DISTINCT day FROM exported_dates")}

    def history(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT id,created,kind,target,count FROM exports ORDER BY created DESC,id DESC LIMIT 100")]

    def restore_export(self, identity: int) -> BatchDraft:
        with self.connection() as db:
            row = db.execute("SELECT body FROM exports WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("找不到这条导出记录。")
        return BatchDraft.decode(row[0])
