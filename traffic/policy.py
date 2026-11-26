"""고정, 대기열, 가변 행동 시간 Q-learning 정책."""
from random import Random

from .config import read_json, write_json
from .signals import Action

ACTIONS = tuple(Action)


class Policy:
    def observe(self, reward):
        pass

    def finish(self):
        pass


class FixedPolicy(Policy):
    def __init__(self, seconds):
        self.seconds = seconds

    def decide(self, state, allowed):
        remaining = self.seconds - state[3]
        return max(action for action in allowed if action <= remaining)


def queue_action(state, allowed):
    first, second, group, _ = state
    queues = (first, second)
    if queues[1 - group] > queues[group] or len(allowed) == 1:
        return Action.SWITCH
    return Action.EXTEND_5


class QueuePolicy(Policy):
    def decide(self, state, allowed):
        return queue_action(state, allowed)


def queue_bin(length):
    return sum(length >= limit for limit in (1, 3, 6, 12, 24))


def state_key(state):
    first, second, group, elapsed = state
    return ",".join(map(str, (queue_bin(first), queue_bin(second), group, elapsed // 5)))


class QPolicy(Policy):
    def __init__(self, table=None, *, training=False, alpha=0.15, gamma=0.998,
                 epsilon=0.0, seed=0):
        self.table = {} if table is None else table
        self.training, self.alpha, self.gamma, self.epsilon = training, alpha, gamma, epsilon
        self.random = Random(seed)
        self.previous = None
        self.reward, self.discount = 0.0, 1.0

    def values(self, key):
        return self.table.get(key, [0.0] * len(ACTIONS))

    def decide(self, state, allowed):
        key = state_key(state)
        indices = [ACTIONS.index(action) for action in allowed]
        if self.training:
            self._update(max(self.values(key)[i] for i in indices))
        values = self.values(key)
        best = max(values[i] for i in indices)
        tied = [ACTIONS[i] for i in indices if values[i] == best]
        baseline = queue_action(state, allowed)
        action = baseline if baseline in tied else tied[0]
        if self.training and self.random.random() < self.epsilon:
            action = self.random.choice(allowed)
        if self.training:
            self.previous = (key, ACTIONS.index(action))
            self.reward, self.discount = 0.0, 1.0
        return action

    def observe(self, reward):
        if self.training and self.previous is not None:
            self.reward += self.discount * reward
            self.discount *= self.gamma

    def _update(self, next_value):
        if self.previous is None:
            return
        key, index = self.previous
        values = self.table.setdefault(key, [0.0] * len(ACTIONS))
        target = self.reward + self.discount * next_value
        values[index] += self.alpha * (target - values[index])

    def finish(self):
        if self.training:
            self._update(0.0)
            self.previous = None


def save_model(path, table, metadata):
    write_json(path, {"version": 1, "actions": list(ACTIONS), "metadata": metadata, "q_table": table})


def load_model(path, site_id):
    data = read_json(path)
    if (data.get("version") != 1 or data.get("actions") != list(ACTIONS)
            or data["metadata"]["site"] != site_id):
        raise ValueError(f"현재 지점과 모델이 호환되지 않습니다: {path.name}")
    return data
