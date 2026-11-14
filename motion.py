"""A continuous pace and cadence curve shared by live replay and activity files."""

from bisect import bisect_right
from dataclasses import dataclass
from math import ceil, exp, pi, sin
from random import Random


@dataclass(frozen=True)
class Motion:
    meters: float
    speed: float     # m/s
    cadence: float   # both feet, steps/min
    footfalls: float

    @property
    def steps(self) -> int:
        return round(self.footfalls)

    @property
    def step_length(self) -> float:
        """Metres per footfall, using both feet for cadence."""
        return self.speed * 60 / self.cadence


def _blend(u: float) -> float:
    return u*u*(3 - 2*u)


def _area(first: float, change: float, u: float) -> float:
    # Integral of first + change*smoothstep(u) from 0 to u.
    return first*u + change*u**3*(1 - u/2)


class Pacing:
    def __init__(self, meters: int, seconds_per_km: int, seed: int):
        self.meters = meters
        self.duration = meters * seconds_per_km / 1000
        self.count = max(2, ceil(self.duration / 12))
        self.interval = self.duration / self.count
        rng = Random(seed)
        waves = [(amp, 2*pi/rng.uniform(low, high), rng.uniform(0, 2*pi))
                 for amp, low, high in ((0.008, 170, 260), (0.004, 90, 140), (0.003, 350, 540))]
        warmup, finish = rng.uniform(.012, .018), rng.uniform(.005, .009)
        target_speed = 1000/seconds_per_km
        cadence_base = min(190, max(134, 169 + 7*(target_speed - 1000/360))) + rng.uniform(-2, 2)
        cadence_phase = rng.uniform(0, 2*pi)
        progression = rng.uniform(.009, .013) * min(1, self.duration/600)
        factors = []
        for i in range(self.count + 1):
            t = i * self.interval
            factors.append(1 + progression*(2*_blend(t/self.duration)-1)
                           - warmup*exp(-t/70) + finish*exp((t-self.duration)/85)
                           + sum(amp*sin(freq*t+phase) for amp, freq, phase in waves))
        integral = sum((a+b)*self.interval/2 for a, b in zip(factors, factors[1:]))
        self.speeds = [factor*meters/integral for factor in factors]
        self.cadences = [cadence_base*(1 + .46*(speed/target_speed-1))
                         + 1.25*sin(i*self.interval/41 + cadence_phase)
                         for i, speed in enumerate(self.speeds)]
        self.distances, self.footfalls = [0.0], [0.0]
        for i in range(self.count):
            self.distances.append(self.distances[-1] + self.interval*(self.speeds[i]+self.speeds[i+1])/2)
            self.footfalls.append(self.footfalls[-1] + self.interval*(self.cadences[i]+self.cadences[i+1])/120)
        self.distances[-1] = float(meters)

    def at(self, seconds: float) -> Motion:
        t = min(self.duration, max(0.0, seconds))
        i = min(self.count-1, int(t / self.interval))
        dt = t - i*self.interval
        mix = dt / self.interval
        ds = self.speeds[i+1] - self.speeds[i]
        dc = self.cadences[i+1] - self.cadences[i]
        eased = _blend(mix)
        speed = self.speeds[i] + ds*eased
        cadence = self.cadences[i] + dc*eased
        meters = self.distances[i] + self.interval*_area(self.speeds[i], ds, mix)
        steps = self.footfalls[i] + self.interval*_area(self.cadences[i], dc, mix)/60
        return Motion(self.meters if t == self.duration else meters, speed, cadence, steps)

    def _invert(self, value, totals, rates, units_per_second):
        if value <= 0:
            return 0.0
        if value >= totals[-1]:
            return self.duration
        i = bisect_right(totals, value)-1
        local = (value-totals[i]) / (self.interval*units_per_second)
        first, change = rates[i], rates[i+1]-rates[i]
        u = (value-totals[i])/(totals[i+1]-totals[i])
        for _ in range(3):
            u -= (_area(first, change, u)-local)/(first+change*_blend(u))
        return (i+u)*self.interval

    def time_for_footfall(self, footfalls: float) -> float:
        if footfalls > self.footfalls[-1] + 1e-9:
            return float("inf")
        return self._invert(footfalls, self.footfalls, self.cadences, 1/60)

    def time_at(self, meters: float) -> float:
        return self._invert(meters, self.distances, self.speeds, 1.0)
