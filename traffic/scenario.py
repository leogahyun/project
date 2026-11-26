"""외부 속도 입력과 가상 차량 수요를 독립된 시간표로 관리한다."""
from bisect import bisect_right
from hashlib import sha256
from itertools import count, islice
from random import Random

from .config import ROOT, DT, TICKS, read_json
from .observations import Observation, ObservationFeed, read_recording


class Scenario:
    def __init__(self, site, name, seed, *, duration=None, recording=None, spacing=1,
                 speed_override=None, demand_rates=None):
        settings = read_json(ROOT / site.scenarios[name])
        self.id, self.name, self.seed = name, settings["name"], seed
        self.duration = int(duration if duration is not None else settings["duration"])
        self.ticks = self.duration * TICKS
        if self.ticks <= 0:
            raise ValueError("실험 시간은 양수여야 합니다.")
        demand = ([{"at_s": 0, "per_hour": demand_rates}] if demand_rates is not None
                  else sorted(settings["demand"], key=lambda row: row["at_s"]))
        starts = [row["at_s"] * TICKS for row in demand]
        if not starts or starts[0] != 0:
            raise ValueError("수요 입력은 0초에서 시작해야 합니다.")
        self.demand, self.demand_starts = demand, starts
        self.schedule = tuple(islice(self.arrivals(site), self.ticks))
        self.signature = sha256(repr(self.schedule).encode()).hexdigest()
        self.timeline = self._timeline(site, settings, speed_override)
        if recording:
            recorded = dict(read_recording(recording, site, TICKS, spacing))
            if max(recorded) >= self.ticks:
                raise ValueError("기록의 마지막 관측이 실험 범위를 벗어납니다. 실행 시간을 늘리거나 기록을 압축하세요.")
            frames = dict(self.timeline)
            # 한 링크의 실제 기록이 시작되면 이후 해당 링크의 합성 갱신을 멈춘다.
            started = set()
            for tick in sorted(frames.keys() | recorded.keys()):
                row = recorded.get(tick, {})
                started.update(row)
                frames[tick] = {**{k: v for k, v in frames.get(tick, {}).items()
                                   if k not in started}, **row}
            self.timeline = tuple(sorted(frames.items()))
        self.recording = recording
        self.spacing = spacing

    def arrivals(self, site):
        rng = Random(self.seed)
        number = 0
        for tick in count():
            index = bisect_right(self.demand_starts, tick % self.ticks) - 1
            row = self.demand[index]
            arrivals = []
            for approach in site.approaches:
                per_hour = row["per_hour"][approach.id]
                if rng.random() < per_hour * DT / 3600:
                    number += 1
                    arrivals.append((approach.id, number))
            yield tuple(arrivals)

    def _timeline(self, site, settings, speed_override):
        frames = []
        if not settings["speeds"] or min(row["at_s"] for row in settings["speeds"]) != 0:
            raise ValueError("속도 입력은 0초에서 시작해야 합니다.")
        for row in sorted(settings["speeds"], key=lambda item: item["at_s"]):
            values = {a.id: Observation(float(speed_override if speed_override is not None
                                              else row["kmh"][a.id]), "synthetic")
                      for a in site.approaches}
            frames.append((row["at_s"] * TICKS, values))
        return tuple(frames)

    def feed(self, poller=None, stale_after=180):
        return ObservationFeed(self.timeline, poller, stale_after)

    def tick_inputs(self):
        feed = self.feed()
        speeds = {}
        for tick, arrivals in enumerate(self.schedule):
            if feed.next is not None and feed.next[0] <= tick:
                speeds = {lane: obs.speed_kmh for lane, obs in feed.apply(tick).items()}
            yield arrivals, speeds
