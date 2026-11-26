"""속도 변화 감지와 백그라운드 Q-learning 검증."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from random import Random
from statistics import fmean
from threading import Event
from uuid import uuid4

from .config import write_json
from .experiment import evaluate, run
from .policy import QPolicy, save_model
from .scenario import Scenario


class SpeedChange:
    def __init__(self, percent):
        self.threshold = percent / 100
        self.history = {}
        self.hits = {}

    def observe(self, values):
        changes = {}
        for lane in self.history.keys() - values.keys():
            self.hits.pop(lane, None)
            self.history[lane].clear()
        for lane, observation in values.items():
            speed = observation.speed_kmh
            history = self.history.setdefault(lane, deque(maxlen=3))
            if len(history) == 3:
                baseline = fmean(history)
                change = (speed - baseline) / baseline
                if abs(change) >= self.threshold:
                    direction = 1 if change > 0 else -1
                    if self.hits.get(lane) == direction:
                        changes[lane] = round(change * 100, 1)
                        self.hits.pop(lane)
                        history.clear()
                    else:
                        self.hits[lane] = direction
                        continue
                else:
                    self.hits.pop(lane, None)
            history.append(speed)
        return changes


def train_candidate(site, name, base, observations, changes, episodes, seed, stop):
    table = {key: row[:] for key, row in base["q_table"].items()}
    seeds = Random(seed).sample(range(10000, 2**31), episodes + 5)

    def scenario(arrival_seed):
        sample = Scenario(site, name, arrival_seed)
        sample.timeline = ((0, observations),)
        return sample

    for index, arrival_seed in enumerate(seeds[:episodes]):
        if stop.is_set():
            return None
        policy = QPolicy(table, training=True, alpha=0.08, gamma=0.998,
                         epsilon=0.20 - 0.15 * index / max(1, episodes - 1), seed=arrival_seed)
        run(site, scenario(arrival_seed), {"candidate": policy})

    if stop.is_set():
        return None
    report = evaluate(site, [scenario(s) for s in seeds[episodes:]],
                      {"current": base["q_table"], "candidate": table})
    current = report["summary"]["current"]
    candidate = report["summary"]["candidate"]
    accepted = (candidate["mean_wait_s"] < current["mean_wait_s"]
                and candidate["mean_crossed_stop_line"] >= current["mean_crossed_stop_line"]
                and all(new <= old for new, old in zip(candidate["max_wait_s_by_group"],
                                                       current["max_wait_s_by_group"])))
    metadata = {"site": site.id, "scenario": name, "snapshot": "adaptive",
                "episodes": base["metadata"]["episodes"] + episodes,
                "new_episodes": episodes, "changes_percent": changes, "seed": seed,
                "training_arrival_seeds": seeds[:episodes],
                "observations": {lane: asdict(obs) for lane, obs in observations.items()}}
    return table, metadata, report, accepted


class AdaptiveTraining:
    def __init__(self, site, name, args, directory):
        self.site, self.name = site, name
        self.detector = SpeedChange(args.change_percent)
        self.episodes = args.adaptive_episodes
        self.stale_after = max(180, args.poll_seconds * 3)
        self.directory = Path(directory)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="signal-training")
        self.stop = Event()
        self.future = self.waiting = None
        self.status = "속도 변화 감시 중"

    def observe(self, values):
        if len(values) != len(self.site.approaches):
            self.detector.observe({})
            self.waiting = None
            return
        changes = self.detector.observe(values)
        if changes:
            self.waiting = (dict(values), changes)

    def update(self, model):
        replacement = None
        if self.future and self.future.done():
            try:
                table, metadata, report, accepted = self.future.result()
                current = report["summary"]["current"]["mean_wait_s"]
                candidate = report["summary"]["candidate"]["mean_wait_s"]
                write_json(self.directory / f"{uuid4().hex}.json", {
                    "accepted": accepted, "training": metadata, "evaluation": report})
                if accepted:
                    path = self.directory / f"accepted-{uuid4().hex}.tmp"
                    save_model(path, table, metadata)
                    path.replace(self.directory / "accepted.json")
                    replacement = {"metadata": metadata, "q_table": table}
                decision = "새 모델 적용" if accepted else "기존 모델 유지"
                self.status = f"{decision} · 검증 대기 {current:.2f}초 → {candidate:.2f}초"
            except (OSError, ValueError, KeyError):
                self.status = "추가 학습 또는 저장 실패 · 기존 모델 유지"
            self.future = None

        if self.future is None and self.waiting:
            values, changes = self.waiting
            self.waiting = None
            if not all(not obs.outdated(self.stale_after) for obs in values.values()):
                return replacement
            seed = uuid4().int
            self.future = self.executor.submit(train_candidate, self.site, self.name,
                                               replacement or model, values, changes,
                                               self.episodes, seed, self.stop)
            change = next(iter(changes.values()))
            self.status = f"속도 변화 {change:+.1f}% · 추가 학습 중"
        return replacement

    def close(self):
        self.stop.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
