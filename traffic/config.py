"""지역별 JSON과 시뮬레이션의 공통 단위."""
from dataclasses import dataclass
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DT = 0.25
TICKS = round(1 / DT)
DEFAULT_EVALUATION_SEEDS = (8101, 8102, 8103, 8104, 8105)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


@dataclass(frozen=True)
class SignalSettings:
    minimum: int = 10
    maximum: int = 60
    yellow: int = 3
    all_red: int = 1
    fixed: int = 30

    def __post_init__(self):
        if not (0 < self.minimum <= self.fixed <= self.maximum):
            raise ValueError("신호 시간은 최소 ≤ 고정 ≤ 최대여야 합니다.")
        if (self.fixed - self.minimum) % 5 or min(self.yellow, self.all_red) <= 0:
            raise ValueError("고정 녹색은 최소 녹색에서 5초 단위여야 합니다.")


@dataclass(frozen=True)
class Approach:
    id: str
    group: int
    road: str
    direction: str
    section: str
    link_id: str | None
    path: tuple
    stop: float
    label: tuple


@dataclass(frozen=True)
class Site:
    id: str
    title: str
    district: str
    groups: tuple
    approaches: tuple
    signal: SignalSettings
    speed: dict
    drawing: dict
    source: dict
    scenarios: dict
    archive: dict
    model_dir: Path


def districts():
    return read_json(ROOT / "data/regions.json")


def load_site(site_id):
    entry = next((r for r in districts() if r.get("site") == site_id), None)
    if entry is None:
        raise ValueError(f"등록된 상세 지점이 아닙니다: {site_id}")
    raw = read_json(ROOT / entry["map"])
    approaches = tuple(Approach(**{**a, "path": tuple(map(tuple, a["path"])),
                                    "label": tuple(a["label"])}) for a in raw["approaches"])
    if len({a.id for a in approaches}) != len(approaches) or {a.group for a in approaches} != {0, 1}:
        raise ValueError("고유한 접근로 ID와 두 신호 그룹이 필요합니다.")
    return Site(site_id, raw["title"], entry["name"], tuple(raw["groups"]), approaches,
                SignalSettings(**raw.get("signal", {})), raw["speed"], raw["drawing"],
                raw["source"], raw["scenarios"], raw.get("archive", {}), ROOT / raw["model_dir"])
