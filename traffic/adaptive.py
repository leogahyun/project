"""교통량 변화 감지와 백그라운드 Q-learning 검증."""
import csv
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from random import Random
from datetime import datetime
from threading import Event
from uuid import uuid4

from .config import write_json
from .observations import KST
from .volume import VolumeChange, fresh_volume, rates_for
from .experiment import evaluate, run
from .policy import QPolicy, save_model
from .scenario import Scenario


def train_candidate(site, name, base, observations, volume, change, episodes, seed, stop):
    table = {key: row[:] for key, row in base["q_table"].items()}
    seeds = Random(seed).sample(range(10000, 2**31), episodes + 5)

    demand = rates_for(site, volume)

    def scenario(arrival_seed):
        sample = Scenario(site, name, arrival_seed, demand_rates=demand)
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
                      {"current": base["q_table"], "candidate": table}, signals=True)
    current = report["summary"]["current"]
    candidate = report["summary"]["candidate"]
    accepted = (candidate["mean_wait_s"] < current["mean_wait_s"]
                and candidate["mean_crossed_stop_line"] >= current["mean_crossed_stop_line"]
                and all(new <= old for new, old in zip(candidate["max_wait_s_by_group"],
                                                       current["max_wait_s_by_group"])))
    metadata = {"site": site.id, "scenario": name, "snapshot": "adaptive",
                "episodes": base["metadata"]["episodes"] + episodes,
                "new_episodes": episodes, "volume_change": change, "volume": asdict(volume),
                "arrival_rates_per_hour": demand, "seed": seed,
                "training_arrival_seeds": seeds[:episodes],
                "observations": {lane: asdict(obs) for lane, obs in observations.items()}}
    return table, metadata, report, accepted


class AdaptiveTraining:
    def __init__(self, site, name, args, directory):
        self.site, self.name = site, name
        self.detector = VolumeChange(args.change_percent)
        self.volume = None
        self.episodes = args.adaptive_episodes
        self.stale_after = max(180, args.poll_seconds * 3)
        self.directory = Path(directory)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="signal-training")
        self.stop = Event()
        self.future = self.waiting = None
        self.status = "TOPIS 교통량 대기 중"

    def observe(self, values, volume):
        if volume is None:
            if self.waiting and len(values) == len(self.site.approaches):
                _, observed_volume, change = self.waiting
                self.waiting = (dict(values), observed_volume, change)
            return
        self.volume = volume
        change = self.detector.observe(volume)
        if change:
            self.waiting = (dict(values), volume, change)
        elif self.future is None and self.waiting is None and self.status == "TOPIS 교통량 대기 중":
            self.status = "교통량 변화 감시 중"

    def log_comparison(self, event_id, metadata, report, applied):
        summary = report["summary"]
        fixed = summary["fixed"]["mean_wait_s"]
        previous = summary["current"]["mean_wait_s"]
        candidate = summary["candidate"]["mean_wait_s"]
        selected = candidate if applied else previous
        change = metadata["volume_change"]
        row = {"시간_KST": datetime.now(KST).isoformat(timespec="seconds"),
               "이벤트": event_id, "관측소": metadata["volume"]["station"],
               "집계_시작_KST": metadata["volume"]["hour"],
               "직전_교통량": change["previous"], "새_교통량": change["current"],
               "교통량_변화율_pct": change["percent"], "교체": applied,
               "고정_평균_대기_s": fixed, "기존_AI_평균_대기_s": previous,
               "새_AI_평균_대기_s": candidate, "새_AI_빼기_고정_s": round(candidate - fixed, 3),
               "적용_AI_빼기_고정_s": round(selected - fixed, 3)}
        path = self.directory / "events.csv"
        new_file = not path.exists() or path.stat().st_size == 0
        with path.open("a", encoding="utf-8-sig" if new_file else "utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=row)
            if new_file:
                writer.writeheader()
            writer.writerow(row)
        return row["적용_AI_빼기_고정_s"]

    def update(self, model):
        replacement = None
        if self.future and self.future.done():
            try:
                table, metadata, report, accepted = self.future.result()
                event_id = uuid4().hex
                write_json(self.directory / f"{event_id}.json", {
                    "accepted": accepted, "training": metadata, "evaluation": report})
                if accepted:
                    path = self.directory / f"accepted-{uuid4().hex}.tmp"
                    save_model(path, table, metadata)
                    path.replace(self.directory / "accepted.json")
                    replacement = {"metadata": metadata, "q_table": table}
                delta = self.log_comparison(event_id, metadata, report, accepted)
                decision = "새 모델 적용" if accepted else "기존 모델 유지"
                self.status = f"{decision} · 고정 신호 대비 {delta:+.2f}초"
            except (OSError, ValueError, KeyError):
                self.status = ("새 모델 적용 · CSV 저장 실패" if replacement else
                               "추가 학습 또는 저장 실패 · 기존 모델 유지")
            self.future = None

        if self.future is None and self.waiting:
            values, volume, change = self.waiting
            if self.volume != volume or not fresh_volume(volume):
                self.waiting = None
                return replacement
            if len(values) != len(self.site.approaches) or any(
                    obs.outdated(self.stale_after) for obs in values.values()):
                return replacement
            self.waiting = None
            seed = uuid4().int
            self.future = self.executor.submit(train_candidate, self.site, self.name,
                                               replacement or model, values, volume, change,
                                               self.episodes, seed, self.stop)
            percent = change["percent"]
            change_text = f"{percent:+.1f}%" if percent is not None else f"0 → {volume.count}대"
            self.status = f"교통량 변화 {change_text} · 추가 학습 중"
        return replacement

    def close(self):
        self.stop.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
