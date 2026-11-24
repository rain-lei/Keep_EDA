"""Last successfully sent position and virtual step progress for each LDPlayer instance."""

from dataclasses import asdict
from datetime import datetime
import json
from os import replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from time import monotonic
from campus import CampusTrack, ROUTES, RunPlan, distance_m


class LastReplay:
    def __init__(self, path: Path, route: str, plan):
        self.path = path
        self.route = route
        self.plan = plan
        self.lock = Lock()
        self.last_write = -float("inf")
        self.state = "回放中"
        self.error = None

    def update(self, control, state=None):
        with self.lock:
            now = monotonic()
            if state is not None:
                self.state = state
            elif now-self.last_write < 2:
                return
            point, steps = control.last_sent()
            if point is None:
                return  # Waiting for GO must not overwrite the previous replay.
            self.last_write = now
            data = {"updated":datetime.now().astimezone().isoformat(timespec="seconds"),
                    "state":self.state, "route":self.route, "target_m":self.plan.meters, "plan":asdict(self.plan),
                    "elapsed_s":round(point.seconds,3), "location_m":round(point.meters,3),
                    "longitude":point.point[0], "latitude":point.point[1], "steps_sent":steps}
            staged = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with NamedTemporaryFile("w", encoding="utf-8", prefix=".last-replay-", dir=self.path.parent,
                                        delete=False) as output:
                    staged = Path(output.name)
                    json.dump(data, output, ensure_ascii=False)
                replace(staged, self.path)
                self.error = None
            except OSError as error:
                self.error = str(error)
            finally:
                if staged is not None and staged.exists():
                    try:
                        staged.unlink()
                    except OSError:
                        pass


def interrupted_progress(data, track, plan):
    try:
        state = data["state"]
        if ((not state.startswith("中断") and state not in {"回放中", "已暂停"})
                or data["route"] != track.name or data["target_m"] != plan.meters
                or data.get("plan", asdict(plan)) != asdict(plan)):
            return None
        seconds, steps = data["elapsed_s"], data["steps_sent"]
        if not isinstance(steps, int) or not 0 <= steps < plan.pacing.at(plan.duration).steps or not 0 < seconds < plan.duration:
            return None
        meters = plan.distance_at(seconds)
        expected = track.travel_path(plan).at(meters)
        if abs(meters - data["location_m"]) <= .1 and distance_m(expected, (data["longitude"], data["latitude"])) <= 1:
            return seconds, steps
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        pass
    return None


def restorable_plan(data):
    try:
        values = data["plan"]
        route = next(key for key, name in ROUTES.items() if name == data["route"])
        if (not isinstance(values["clockwise"], bool) or not isinstance(values["random_start"], bool)
                or type(values["seed"]) is not int or not 0 <= values["seed"] < 2**64):
            return None
        seconds = values["seconds_per_km"]
        pace = f"{seconds//60}:{seconds%60:02}"
        plan = RunPlan.parse(str(values["meters"]/1000), pace, values["clockwise"], values["seed"], values["random_start"])
        track = CampusTrack(route)
        if interrupted_progress(data, track, plan):
            return track, plan
    except (KeyError, TypeError, ValueError, OverflowError, StopIteration):
        pass
    return None
