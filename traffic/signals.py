"""두 방향 공통 제어기. 정책은 결정 시점에만 녹색 행동을 요청한다."""
from enum import IntEnum

from .config import TICKS


class Action(IntEnum):
    SWITCH = 0
    EXTEND_5 = 5
    EXTEND_10 = 10


class Signal:
    def __init__(self, settings):
        self.settings = settings
        self.group = 0
        self.phase = "green"
        self.elapsed = 0
        self.limit = settings.minimum * TICKS
        self.closed = False

    def allowed(self):
        if self.phase != "green" or self.elapsed != self.limit:
            return ()
        return tuple(action for action in Action
                     if self.elapsed + int(action) * TICKS <= self.settings.maximum * TICKS)

    def request(self, action):
        if action not in self.allowed():
            raise ValueError(f"허용되지 않은 신호 행동: {action}")
        if action == Action.SWITCH:
            self._change("yellow", self.settings.yellow)
        else:
            self.limit += int(action) * TICKS

    def advance(self):
        self.elapsed += 1
        if self.elapsed < self.limit:
            return
        if self.phase == "green" and self.elapsed == self.settings.maximum * TICKS:
            self._change("yellow", self.settings.yellow)
            self.closed = True
        elif self.phase == "yellow":
            self._change("all_red", self.settings.all_red)
        elif self.phase == "all_red":
            self.group = 1 - self.group
            self._change("green", self.settings.minimum)

    def _change(self, phase, seconds):
        self.phase, self.elapsed, self.limit = phase, 0, seconds * TICKS

    def aspect(self, group):
        if group == self.group and self.phase != "all_red":
            return self.phase
        return "red"
