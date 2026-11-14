"""Last successfully sent position and virtual step progress for each LDPlayer instance."""

from datetime import datetime
import json
from os import replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from time import monotonic


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
                    "state":self.state, "route":self.route, "target_m":self.plan.meters,
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
