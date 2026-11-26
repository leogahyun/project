"""화면 없이 동작하는 고정 스텝 차량 환경과 동일 입력 비교."""
from collections import deque
from dataclasses import dataclass

from .config import DT, TICKS
from .geometry import Path
from .signals import Signal

GAP = 7.0
STOPPED = 0.5


@dataclass(slots=True)
class Vehicle:
    number: int
    position: float = 0.0
    waiting: int = 0
    crossed: bool = False
    speed: float = 0.0
    arrival_tick: int = 0


class Lane:
    def __init__(self, approach):
        self.approach = approach
        self.path = Path(approach.path)
        if not 0 < approach.stop < self.path.length:
            raise ValueError("정지선이 이동 경로 위에 있어야 합니다.")
        self.cars = deque()
        self.pending = deque()
        self.speed = 0.0
        self.queue = 0
        self.crossed = 0
        self.exited = 0
        self.max_wait = 0
        self.tick = 0

    def step(self, green, speed, exit_speed):
        self.speed = speed
        if self.pending and (not self.cars or self.cars[-1].position >= GAP):
            car = self.pending.popleft()
            car.waiting += self.tick - car.arrival_tick
            self.cars.append(car)

        leader = self.path.length + GAP
        waiting = len(self.pending)
        if self.pending:
            oldest = self.pending[0]
            self.max_wait = max(self.max_wait, oldest.waiting + self.tick - oldest.arrival_tick + 1)

        for car in self.cars:
            target = exit_speed if car.crossed else speed
            limit = leader - GAP
            if not green and not car.crossed:
                limit = min(limit, self.approach.stop)
            position = max(car.position, min(car.position + target * DT, limit))
            car.speed = (position - car.position) / DT
            car.position = leader = position
            if not car.crossed and position > self.approach.stop:
                car.crossed = True
                self.crossed += 1
            if not car.crossed and car.speed < STOPPED:
                waiting += 1
                car.waiting += 1
                self.max_wait = max(self.max_wait, car.waiting)

        while self.cars and self.cars[0].position >= self.path.length:
            self.cars.popleft()
            self.exited += 1
        self.queue = waiting
        self.tick += 1
        return waiting

    def condition(self):
        inbound = [car for car in self.cars if not car.crossed]
        if not inbound and not self.pending:
            return "원활"
        mean = sum(car.speed for car in inbound) / max(1, len(inbound))
        if self.queue >= 4 or mean < 3:
            return "정체"
        return "서행" if self.queue >= 2 or mean < 7 else "원활"


class Simulation:
    def __init__(self, site, policy, trace=False):
        self.site, self.policy = site, policy
        self.signal = Signal(site.signal)
        self.lanes = {a.id: Lane(a) for a in site.approaches}
        self.tick = self.total_wait = self.arrivals = 0
        self.queues = (0, 0)
        self.last_action = None
        self.decisions = [] if trace else None
        self.green_durations = [] if trace else None

    def step(self, arrivals, speeds, *, external_aspects=None):
        for lane_id, number in arrivals:
            lane = self.lanes[lane_id]
            lane.pending.append(Vehicle(number, arrival_tick=self.tick))
            lane.queue += 1
            self.arrivals += 1

        allowed = self.signal.allowed() if external_aspects is None else ()
        if allowed:
            queues = self._queues()
            state = (*queues, self.signal.group, self.signal.elapsed // TICKS)
            action = self.policy.decide(state, allowed)
            if action == 0 and self.green_durations is not None:
                self.record_green()
            self.signal.request(action)
            self.last_action = (self.tick, action, "policy")
            if self.decisions is not None:
                self.decisions.append((self.tick, state, int(action)))

        settings = self.site.speed
        waiting = 0
        for lane_id, lane in self.lanes.items():
            road_speed = max(settings["minimum_mps"], min(settings["maximum_mps"],
                             speeds[lane_id] * settings["scale"] / 3.6))
            aspect = (self.signal.aspect(lane.approach.group) if external_aspects is None
                      else external_aspects[lane_id])
            waiting += lane.step(aspect == "green",
                                 road_speed, settings["exit_mps"])
        self.total_wait += waiting
        self.queues = self._queues()
        self.policy.observe(-waiting * DT)
        if (external_aspects is None and self.green_durations is not None and self.signal.phase == "green"
                and self.signal.elapsed + 1 == self.signal.settings.maximum * TICKS):
            self.record_green(1)
        if external_aspects is None:
            self.signal.advance()
        self.tick += 1
        if self.signal.closed:
            self.last_action = (self.tick, 0, "maximum")
            self.signal.closed = False

    def record_green(self, extra_tick=0):
        self.green_durations.append({"group": self.signal.group,
                                     "seconds": (self.signal.elapsed + extra_tick) * DT})

    def _queues(self):
        groups = [0, 0]
        for lane in self.lanes.values():
            groups[lane.approach.group] += lane.queue
        return tuple(groups)

    def metrics(self):
        lanes = tuple(self.lanes.values())
        total = self.total_wait * DT
        return {
            "arrivals": self.arrivals,
            "mean_wait_s": round(total / self.arrivals, 4) if self.arrivals else 0.0,
            "total_wait_vehicle_s": round(total, 4),
            "max_wait_s_by_approach": {a.id: self.lanes[a.id].max_wait * DT for a in self.site.approaches},
            "max_wait_s_by_group": [max(lane.max_wait * DT for lane in lanes
                                         if lane.approach.group == group) for group in (0, 1)],
            "crossed_stop_line": sum(lane.crossed for lane in lanes),
            "exited": sum(lane.exited for lane in lanes),
            "on_road_end": sum(len(lane.cars) for lane in lanes),
            "queued_on_road_end": sum(lane.queue - len(lane.pending) for lane in lanes),
            "pending_end": sum(len(lane.pending) for lane in lanes),
        }
