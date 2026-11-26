"""VS Code와 명령행에서 공통으로 사용하는 실행 파일."""
import argparse
from pathlib import Path
import sys

from traffic.config import DEFAULT_EVALUATION_SEEDS, ROOT, districts, load_site, write_json
from traffic.experiment import dataset, evaluate, train
from traffic.policy import load_model


def positive(string):
    value = int(string)
    if value < 1:
        raise argparse.ArgumentTypeError("양의 정수가 필요합니다.")
    return value


def multiplier(string):
    value = float(string)
    if not 0.01 <= value <= 10000:
        raise argparse.ArgumentTypeError("배율은 0.01~10000 사이여야 합니다.")
    return value


def parser():
    result = argparse.ArgumentParser(description="서울 교통 신호 시뮬레이터")
    result.add_argument("command", nargs="?", choices=("gui", "train", "evaluate"), default="gui")
    result.add_argument("--site", choices=[r["site"] for r in districts() if r.get("site")],
                        default=None)
    result.add_argument("--scenario", help="shift, morning, day, evening 등 지역별 시나리오 ID")
    result.add_argument("--seed", type=int, default=2026)
    result.add_argument("--duration", type=positive, default=240, help="실험 시간(초)")
    result.add_argument("--mode", choices=("auto", "live", "offline", "synthetic"), default="auto")
    result.add_argument("--adaptive", action="store_true", help="TOPIS 교통량 변화 감시 및 추가 학습")
    result.add_argument("--change-percent", type=multiplier, default=20)
    result.add_argument("--adaptive-episodes", type=positive, default=24)
    result.add_argument("--poll-seconds", type=positive, default=60)
    result.add_argument("--recording", type=Path, help="재생하거나 추가 학습할 TOPIS JSONL 경로")
    result.add_argument("--recording-compression", type=multiplier, default=1,
                        help="직접 지정한 기록의 조회 또는 관측 시각 간격을 나누는 배율")
    result.add_argument("--snapshot", default="final",
                        help="GUI 초기 모델 이름(initial, middle, final) 또는 JSON 경로")
    result.add_argument("--speed", type=multiplier, default=3, help="GUI 시뮬레이션 속도")
    result.add_argument("--episodes", type=positive, default=160)
    result.add_argument("--checkpoint-every", type=positive, default=20)
    result.add_argument("--resume", type=Path, help="추가 학습을 시작할 모델 JSON")
    result.add_argument("--output", type=Path, help="학습 폴더 또는 평가 결과 JSON")
    result.add_argument("--models", nargs="+", type=Path, help="평가 대상 모델 JSON 여러 개")
    result.add_argument("--seeds", nargs="+", type=int, default=DEFAULT_EVALUATION_SEEDS)
    result.add_argument("--screenshot", type=Path, help="지정 시간의 Pygame 화면 저장 후 종료")
    result.add_argument("--advance", type=positive, default=74,
                        help="스크린샷을 찍을 시각(시뮬레이션 초)")
    result.add_argument("--headless", action="store_true", help="화면 장치 없이 스크린샷 생성")
    return result


def main():
    args = parser().parse_args()
    try:
        if args.adaptive:
            if args.command != "gui" or args.mode not in ("live", "auto") or args.recording or args.screenshot:
                raise ValueError("--adaptive는 TOPIS 라이브 창에서 사용합니다.")
            from traffic.volume import SITE_VOLUME
            args.site = args.site or "taereung"
            if args.site not in SITE_VOLUME:
                raise ValueError("상시 교통량 모드는 화랑대역 D-10 관측소가 매핑된 태릉입구역을 지원합니다.")
            if args.scenario:
                raise ValueError("상시 교통량 모드에는 시나리오를 지정하지 않습니다.")
            args.speed = 1
        site = load_site(args.site or "seolleung")
        if args.scenario and args.scenario not in site.scenarios:
            raise ValueError(f"가능한 시나리오: {', '.join(site.scenarios)}")
        if args.command == "train":
            args.output = args.output or ROOT / "runtime/models" / site.id
            if args.resume and args.resume.resolve().parent == args.output.resolve():
                raise ValueError("추가 학습 결과를 다른 --output 폴더에 지정하세요.")
            report = train(site, args)
        elif args.command == "evaluate":
            paths = args.models or [site.model_dir / f"{name}.json"
                                    for name in ("initial", "middle", "final")]
            tables = {}
            for path in paths:
                label = path.stem
                if label in tables:
                    label = f"{path.parent.name}/{label}"
                if label in tables or label in ("fixed", "queue"):
                    label = str(path.resolve())
                tables[label] = load_model(path, site.id)["q_table"]
            names = [args.scenario] if args.scenario else list(site.scenarios)
            report = evaluate(site, dataset(site, names, args.seeds, args.duration,
                                            args.recording, args.recording_compression), tables)
            args.output = args.output or ROOT / "runtime" / f"{site.id}_evaluation.json"
            write_json(args.output, report)
        else:
            if args.headless and not args.screenshot:
                raise ValueError("--headless는 --screenshot과 같이 사용합니다.")
            from traffic.ui import launch
            return launch(args, site)
        for name, metrics in report["summary"].items():
            print(f"{name:18s} | {metrics['mean_wait_s']:6.2f} s | "
                  f"{metrics['mean_crossed_stop_line']:6.1f} crossed")
        print(args.output)
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(f"실행 오류: {error}", file=sys.stderr)
        return 1
    except ImportError:
        print("python -m pip install -r requirements.txt", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
