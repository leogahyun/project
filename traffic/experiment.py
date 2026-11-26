"""그래픽 없이 세 정책을 평가하고 Q-table 스냅샷을 생성한다."""
from pathlib import Path
from random import Random
from statistics import fmean

from .config import DEFAULT_EVALUATION_SEEDS, DT, TICKS, write_json
from .engine import Simulation
from .policy import FixedPolicy, QPolicy, QueuePolicy, load_model, save_model
from .scenario import Scenario

METRIC_BASIS = "예정 도착 전체. 진입 대기와 정지 누적만 계산하고, 종료 시까지의 대기를 포함."


def run(site, scenario, policies):
    simulations = {label: Simulation(site, policy) for label, policy in policies.items()}
    for arrivals, speeds in scenario.tick_inputs():
        for simulation in simulations.values():
            simulation.step(arrivals, speeds)
    for policy in policies.values():
        policy.finish()
    return {label: sim.metrics() for label, sim in simulations.items()}


def dataset(site, names, seeds, duration, recording=None, spacing=1):
    return [Scenario(site, name, seed, duration=duration, recording=recording, spacing=spacing)
            for name in names for seed in seeds]


def evaluate(site, scenarios, tables):
    runs = []
    for scenario in scenarios:
        policies = {"fixed": FixedPolicy(site.signal.fixed), "queue": QueuePolicy()}
        policies.update({name: QPolicy(table) for name, table in tables.items()})
        metrics = run(site, scenario, policies)
        runs.append({"scenario": scenario.id, "seed": scenario.seed, "seconds": scenario.duration,
                     "recording": Path(scenario.recording).name if scenario.recording else None,
                     "recording_compression": scenario.spacing,
                     "arrivals_sha256": scenario.signature, "policies": metrics})
    return {"site": site.id, "mean_wait_basis": METRIC_BASIS, "runs": runs,
            "summary": summarize(runs)}


def summarize(runs):
    results = {}
    columns = ("arrivals", "total_wait_vehicle_s", "crossed_stop_line", "exited",
               "on_road_end", "queued_on_road_end", "pending_end")
    for name in runs[0]["policies"]:
        values = [row["policies"][name] for row in runs]
        weighted = sum(row["total_wait_vehicle_s"] for row in values) / max(1, sum(row["arrivals"] for row in values))
        results[name] = {"mean_wait_s": round(weighted, 3),
                         **{f"mean_{column}": round(fmean(row[column] for row in values), 3)
                            for column in columns},
                         "max_wait_s_by_approach": {lane: max(row["max_wait_s_by_approach"][lane]
                                                          for row in values)
                                                    for lane in values[0]["max_wait_s_by_approach"]},
                         "max_wait_s_by_group": [max(row["max_wait_s_by_group"][index] for row in values)
                                                 for index in (0, 1)]}
    return results


def train(site, args):
    if args.episodes < 2:
        raise ValueError("초기·중간·완료 스냅샷을 만들려면 2회 이상 학습해야 합니다.")
    out = Path(args.output)
    names = [args.scenario] if args.scenario else list(site.scenarios)
    base = load_model(Path(args.resume), site.id) if args.resume else None
    table = base["q_table"] if base else {}
    base_episodes = base["metadata"]["episodes"] if base else 0
    random = Random(args.seed)
    validation = dataset(site, names, (9101, 9102, 9103), args.duration)
    milestones = {0: "initial", args.episodes // 2: "middle", args.episodes: "final"}
    evaluation = []
    saved = {}
    inputs = []
    alpha, gamma = 0.16, 0.998

    def checkpoint(episode):
        label = milestones.get(episode)
        if label:
            save_model(out / f"{label}.json", table,
                       {"site": site.id, "snapshot": label, "episodes": base_episodes + episode,
                        "new_episodes": episode, "seed": args.seed, "scenarios": names,
                        "training_inputs": inputs, "alpha": alpha, "gamma_per_tick": gamma,
                        "step_seconds": DT, "duration_s": args.duration,
                        "signal": vars(site.signal),
                        "recording": Path(args.recording).name if args.recording else None,
                        "recording_compression": args.recording_compression,
                        "parent": Path(args.resume).name if args.resume else None})
            saved[label] = {key: q[:] for key, q in table.items()}
        report = evaluate(site, validation, {"q": table})
        evaluation.append({"episode": episode, "states": len(table), "metrics": report["summary"]})
        print(f"{site.id} | {episode}/{args.episodes} | states={len(table)} | "
              f"validation wait={report['summary']['q']['mean_wait_s']:.3f}s", flush=True)

    checkpoint(0)
    for episode in range(1, args.episodes + 1):
        name = names[(episode - 1) % len(names)]
        seed = random.randrange(10000, 2**31)
        epsilon = 0.035 + 0.52 * (1 - (episode - 1) / (args.episodes - 1))
        scenario = Scenario(site, name, seed, duration=args.duration, recording=args.recording,
                            spacing=args.recording_compression)
        inputs.append({"scenario": name, "seed": seed, "arrivals_sha256": scenario.signature})
        policy = QPolicy(table, training=True, alpha=alpha, gamma=gamma,
                         epsilon=epsilon, seed=seed ^ 0xF41)
        run(site, scenario, {"q": policy})
        if episode in milestones or episode % args.checkpoint_every == 0:
            checkpoint(episode)
    write_json(out / "learning_curve.json", evaluation)
    report = evaluate(site, dataset(site, names, DEFAULT_EVALUATION_SEEDS, args.duration), saved)
    write_json(out / "evaluation.json", report)
    return report
