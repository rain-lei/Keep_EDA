"""Schedule campus runs in Beijing time and package one FIT per selected date."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
import os
from pathlib import Path
from random import Random
import re
from tempfile import NamedTemporaryFile, TemporaryDirectory
from threading import Event
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from campus import ROUTES, CampusTrack, RunPlan
from fit_export import export_fit


BEIJING = timezone(timedelta(hours=8), "北京时间")
DEFAULT_PERIOD = "下午或晚上"
PERIODS = {
    DEFAULT_PERIOD: (("14:00", "18:00"), ("18:30", "21:30")),
    "白天": (("08:00", "18:00"),),
    "上午": (("08:00", "11:30"),),
    "下午": (("14:00", "18:00"),),
    "晚上": (("18:30", "21:30"),),
    "工作日晚 / 周末午后": (("18:30", "21:30"), ("14:00", "18:00")),
}
MAX_ZIP_BYTES = 500_000_000  # The Keep import screen specifies 500M per upload.
FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)
FIT_LAST = FIT_EPOCH + timedelta(seconds=0xfffffffe)
DATE_PATTERN = r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"


def _date(value: str) -> date:
    if not re.fullmatch(DATE_PATTERN, value):
        raise ValueError(f"无法识别「{value}」，日期请用 年-月-日。")
    try:
        day = date(*map(int, re.split(r"[-/.]", value)))
    except ValueError as error:
        raise ValueError(f"日期「{value}」不存在。") from error
    if not FIT_EPOCH.date() <= day <= FIT_LAST.date():
        raise ValueError(f"日期「{value}」超出 FIT 时间戳可记录的范围。")
    return day


def parse_dates(text: str) -> list[date]:
    text = text.strip().lstrip("\ufeff").replace('"', '')
    text = re.sub(rf"({DATE_PATTERN})\s*(?:~|～|至|到|—|–|\.\.|-)\s*({DATE_PATTERN})",
                  r"\1~\2", text)
    tokens = [s for s in re.split(r"[\s,，;；、]+", text) if s]
    if tokens and tokens[0].lower() in {"日期", "date"}:
        tokens = tokens[1:]
    days = set()
    for token in tokens:
        a, sep, b = token.partition("~")
        first = _date(a)
        last = _date(b) if sep else first
        if last < first:
            raise ValueError(f"日期范围「{token}」的结束日期早于开始日期。")
        days.update(date.fromordinal(n) for n in range(first.toordinal(), last.toordinal()+1))
    if not days:
        raise ValueError("请填写日期，或读取包含日期的 TXT / CSV 文件。")
    return sorted(days)


def read_dates(path: Path) -> list[date]:
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return parse_dates(data.decode("utf-16"))
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("gb18030")
    return parse_dates(text)


def _clock_seconds(value: str, *, end: bool = False) -> int:
    text = value.strip().replace("：", ":")
    if end and text == "24:00":
        return 24*3600
    if not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", text):
        raise ValueError("时段请用 24 小时制，例如 14:00 或 21:30。")
    hour, minute = map(int, text.split(":"))
    return hour*3600 + minute*60


@dataclass(frozen=True)
class ScheduledRun:
    start: datetime
    end: datetime
    route_seed: int
    route_id: str | None = None
    plan: RunPlan | None = None
    locked: bool = False

    def effective_plan(self, default: RunPlan) -> RunPlan:
        return replace(self.plan or default, seed=self.route_seed)


def filter_dates(days: list[date], weekdays=range(7), excluded=()) -> list[date]:
    allowed, omitted = set(weekdays), set(excluded)
    return [day for day in sorted(set(days)) if day.weekday() in allowed and day not in omitted]


def validate_run(run: ScheduledRun, default: RunPlan) -> None:
    plan = run.effective_plan(default)
    RunPlan.parse(str(plan.meters/1000), f"{plan.seconds_per_km//60}:{plan.seconds_per_km%60:02}",
                  plan.clockwise, plan.seed, plan.random_start)
    if run.route_id is not None and run.route_id not in ROUTES:
        raise ValueError("批次中含有未知路线。")
    if run.start.utcoffset() != BEIJING.utcoffset(None) or run.end.utcoffset() != BEIJING.utcoffset(None):
        raise ValueError("批次时间须使用北京时间。")
    if run.start.microsecond or run.end.microsecond:
        raise ValueError("批次时间须精确到整秒。")
    if (run.end-run.start).total_seconds() != max(1, round(plan.duration)):
        raise ValueError("预览起止时间与距离、配速不一致，请重新生成该天。")
    if run.end > datetime.combine(run.start.date()+timedelta(days=1), time(), BEIJING):
        raise ValueError("运动结束时间超过当天，请提前起跑或缩短用时。")
    if run.start < FIT_EPOCH or run.end + timedelta(hours=8) > FIT_LAST:
        raise ValueError(f"{run.start.date()} 的起止时间超出 FIT 可记录的范围。")


def edit_run(run: ScheduledRun, default: RunPlan, *, km: str, pace: str, clock: str,
             route_id: str, clockwise: bool, random_start: bool, locked: bool) -> ScheduledRun:
    plan = RunPlan.parse(km, pace, clockwise, run.route_seed, random_start)
    text = clock.strip().replace("：", ":")
    if not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?", text):
        raise ValueError("起跑时间请填 HH:MM 或 HH:MM:SS，例如 18:35。")
    parts = list(map(int, text.split(":")))
    start = datetime.combine(run.start.date(), time(*parts), BEIJING)
    updated = replace(run, start=start, end=start+timedelta(seconds=max(1, round(plan.duration))),
                      route_id=route_id, plan=plan, locked=locked)
    validate_run(updated, default)
    return updated


def schedule_runs(days: list[date], plan: RunPlan, period: str, time_seed: int,
                  custom: tuple[str, str] = ("14:00", "21:30"), *, evening_percent: int = 50) -> tuple[ScheduledRun, ...]:
    if not days:
        raise ValueError("请至少选择一个日期。")
    if period != "自定义" and period not in PERIODS:
        raise ValueError("请选择有效的随机时段。")
    if not 0 <= evening_percent <= 100:
        raise ValueError("晚上占比须在 0 至 100 之间。")
    if period == "工作日晚 / 周末午后":
        return tuple(schedule_runs([day], plan, "晚上" if day.weekday() < 5 else "下午", time_seed)[0]
                     for day in sorted(set(days)))
    windows = (custom,) if period == "自定义" else PERIODS[period]
    duration = max(1, round(plan.duration))  # Same whole seconds as the exported FIT.
    choices = []
    weights = []
    for i, (a, b) in enumerate(windows):
        first, last = _clock_seconds(a), _clock_seconds(b, end=True)
        if first >= last:
            raise ValueError("结束时间须晚于开始时间。当天最晚可填 24:00。")
        weight = (100-evening_percent if i == 0 else evening_percent) if period == DEFAULT_PERIOD else 100
        if first + duration <= last and weight:
            choices.append((first, last-duration))
            weights.append(weight)
    if not choices:
        raise ValueError("当前跑步用时超过所选时段，请扩大时段或调整距离、配速。")
    runs = []
    for day in sorted(set(days)):
        # Changing the list of dates leaves the time and route for other dates intact.
        number = day.toordinal()
        rng = Random((time_seed << 22) | number)
        first, last = rng.choices(choices, weights=weights, k=1)[0]
        start = datetime.combine(day, time(), BEIJING) + timedelta(seconds=rng.randint(first, last))
        end = start + timedelta(seconds=duration)
        if start < FIT_EPOCH or end + timedelta(hours=8) > FIT_LAST:
            raise ValueError(f"{day} 的起止时间超出 FIT 可记录的范围。")
        route_seed = Random((plan.seed << 22) | number).getrandbits(64)
        runs.append(ScheduledRun(start, end, route_seed))
    return tuple(runs)


def archive_name(runs: tuple[ScheduledRun, ...]) -> str:
    return f"DUT-EDA-{runs[0].start:%Y%m%d}-{runs[-1].start:%Y%m%d}-{len(runs)}runs.zip"


def export_batch(path: Path, track: CampusTrack, plan: RunPlan, runs: tuple[ScheduledRun, ...],
                 cancel: Event, progress: Callable[[int, int], None]) -> bool:
    """Publish only a complete archive. A cancelled retry leaves the existing ZIP intact."""
    if not runs:
        raise ValueError("请先生成日期预览。")
    if len({run.start.date() for run in runs}) != len(runs):
        raise ValueError("同一批次每天只能有一条记录。")
    for run in runs:
        validate_run(run, plan)
    tracks = {track.route_id: track}
    with TemporaryDirectory(prefix="keep-eda-fit-") as workspace:
        fit = Path(workspace, "run.fit")
        with NamedTemporaryFile(dir=path.parent, prefix=".keep-eda-", suffix=".tmp", delete=False) as tmp:
            staged = Path(tmp.name)
        try:
            with ZipFile(staged, "w", compression=ZIP_DEFLATED) as archive:
                for index, run in enumerate(runs, 1):
                    if cancel.is_set():
                        return False
                    row_plan = run.effective_plan(plan)
                    route_id = run.route_id or track.route_id
                    if route_id not in tracks:
                        tracks[route_id] = CampusTrack(route_id)
                    export_fit(fit, tracks[route_id], row_plan, run.end)
                    name = f"DUT-EDA-sim-{run.start:%Y%m%d-%H%M%S}-{row_plan.meters}m.fit"
                    # ZIP directory times are local wall times. DOS dates end in 2107.
                    stamp = run.start.timetuple()[:6] if run.start.year <= 2107 else (2107,12,31,23,59,58)
                    info = ZipInfo(name, stamp)
                    archive.writestr(info, fit.read_bytes(), compress_type=ZIP_DEFLATED)
                    if staged.stat().st_size > MAX_ZIP_BYTES:
                        raise ValueError("ZIP 超过 Keep 的 500M 上限，请分批选择日期。")
                    progress(index, len(runs))
            if cancel.is_set():
                return False
            if staged.stat().st_size > MAX_ZIP_BYTES:
                raise ValueError("ZIP 超过 Keep 的 500M 上限，请分批选择日期。")
            os.replace(staged, path)
            return True
        finally:
            staged.unlink(missing_ok=True)
