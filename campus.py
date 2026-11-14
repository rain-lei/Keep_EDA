"""Mapped routes and run settings for the DUT Development Zone campus."""

from bisect import bisect_right
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import cached_property, lru_cache
from itertools import accumulate, pairwise
import json
from math import asin, ceil, cos, hypot, radians, sin, sqrt
from pathlib import Path
from random import Random
import re
from secrets import randbits

from motion import Pacing


Point = tuple[float, float]  # WGS-84, longitude first throughout the application.
DATA_FILE = Path(__file__).resolve().parent / "data" / "dut-eda.geojson"
ROUTES = {"lake": "涌泉湖—院训路环线", "track": "开发区校区田径场"}


def distance_m(first: Point, second: Point) -> float:
    lon1, lat1 = map(radians, first)
    lon2, lat2 = map(radians, second)
    a = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 12_742_017.6 * asin(min(1.0, sqrt(a)))


class Line:
    def __init__(self, points):
        self.points = tuple(points)
        self.offsets = tuple(accumulate((distance_m(a, b) for a, b in pairwise(self.points)), initial=0.0))
        self.length = self.offsets[-1]

    def at(self, distance: float) -> Point:
        offset = min(self.length, max(0.0, distance))
        segment = min(bisect_right(self.offsets, offset) - 1, len(self.points) - 2)
        fraction = (offset - self.offsets[segment]) / (self.offsets[segment + 1] - self.offsets[segment])
        lon1, lat1 = self.points[segment]
        lon2, lat2 = self.points[segment + 1]
        return lon1 + fraction * (lon2 - lon1), lat1 + fraction * (lat2 - lat1)


class CampusTrack(Line):
    def __init__(self, route_id: str = "track") -> None:
        self.route_id = route_id
        self.name = ROUTES[route_id]
        path = DATA_FILE if route_id == "track" else DATA_FILE.with_name("dut-lake.geojson")
        self.features = json.loads(path.read_text(encoding="utf-8"))["features"]
        route = next(f for f in self.features if f["properties"]["kind"] == "route")
        super().__init__(tuple(p) for p in route["geometry"]["coordinates"])
        if self.points[0] != self.points[-1] or any(b <= a for a, b in pairwise(self.offsets)):
            raise ValueError("路线数据需要包含首尾相连的有效线段。")

    def position(self, distance: float, clockwise: bool = False) -> Point:
        return self.at((-distance if clockwise else distance) % self.length)

    def start_point(self, plan: "RunPlan") -> Point:
        return self.at(plan.start_fraction * self.length)

    @lru_cache(maxsize=4)
    def travel_path(self, plan: "RunPlan") -> Line:
        """Follow every mapped corner, moving laterally in smooth, bounded sections."""
        rng = Random(plan.seed ^ 0x43D1)
        band = 2.8 if self.route_id == "track" else 0.85  # metres from the mapped line
        x_scale = 111195*cos(radians(self.points[0][1]))
        y_scale = 111195.0
        start = self.start_point(plan)
        split = bisect_right(self.offsets, plan.start_fraction*self.length)
        lap_points = [start]
        for point in self.points[split:-1] + self.points[:split] + (start,):
            if point != lap_points[-1]:
                lap_points.append(point)
        directed = Line(reversed(lap_points) if plan.clockwise else lap_points)
        stations = []
        for a, b in pairwise(directed.offsets):
            divisions = ceil((b-a)/7)
            for j in range(1, divisions+1):
                s = a+(b-a)*j/divisions
                before, after = (directed.at(u % directed.length) for u in (s-2, s+2))
                dx, dy = (after[0]-before[0])*x_scale, (after[1]-before[1])*y_scale
                norm = hypot(dx, dy) or 1.0
                stations.append((s, directed.at(s), -dy/(norm*x_scale), dx/(norm*y_scale)))
        next_s, next_shift = rng.uniform(24, 50), rng.uniform(-band, band)
        last_s = last_shift = base_m = traveled = 0.0
        path = [start]
        while True:
            for s, center, nx, ny in stations:
                nominal = base_m+s
                while nominal > next_s:
                    last_s, last_shift = next_s, next_shift
                    next_s += rng.uniform(24, 50)
                    next_shift = rng.uniform(-band, band)
                u = (nominal-last_s)/(next_s-last_s)
                shift = last_shift+(next_shift-last_shift)*u*u*(3-2*u)
                p = (center[0]+nx*shift, center[1]+ny*shift)
                segment = distance_m(path[-1], p)
                if traveled+segment >= plan.meters:
                    mix = (plan.meters-traveled)/segment
                    lon, lat = path[-1]
                    path.append((lon+(p[0]-lon)*mix, lat+(p[1]-lat)*mix))
                    return Line(path)
                path.append(p)
                traveled += segment
            base_m += directed.length


@dataclass(frozen=True)
class RunPlan:
    meters: int
    seconds_per_km: int
    clockwise: bool
    seed: int = 0
    random_start: bool = True

    @classmethod
    def parse(cls, km: str, pace: str, clockwise: bool, seed: int | None = None,
              random_start: bool = True) -> "RunPlan":
        message = "距离可填 0.01 至 42.195 公里，最多三位小数。"
        try:
            distance = Decimal(km.strip())
            if not distance.is_finite() or not Decimal("0.01") <= distance <= Decimal("42.195"):
                raise ValueError(message)
            meters = distance * 1000
            if meters != meters.to_integral_value():
                raise ValueError(message)
        except InvalidOperation as error:
            raise ValueError(message) from error
        match = re.fullmatch(r"(\d{1,2})[:：]([0-5]\d)", pace.strip())
        if not match:
            raise ValueError("配速请用 分:秒 填写，例如 6:00。")
        minutes, seconds = map(int, match.groups())
        seconds += minutes * 60
        if not 120 <= seconds <= 1200:
            raise ValueError("每公里配速可填 2:00 至 20:00。")
        return cls(int(meters), seconds, clockwise, randbits(48) if seed is None else seed, random_start)

    @cached_property
    def start_fraction(self) -> float:
        return Random(self.seed ^ 0xD471).random() if self.random_start else 0.0

    @property
    def duration(self) -> float:
        return self.meters * self.seconds_per_km / 1000

    @cached_property
    def pacing(self) -> Pacing:
        return Pacing(self.meters, self.seconds_per_km, self.seed)

    def distance_at(self, seconds: float) -> float:
        return self.pacing.at(seconds).meters


def clock_text(seconds: float) -> str:
    minutes, second = divmod(round(seconds), 60)
    if minutes < 60:
        return f"{minutes:02}:{second:02}"
    hours, minute = divmod(minutes, 60)
    return f"{hours}:{minute:02}:{second:02}"
