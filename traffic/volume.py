"""TOPIS 시간별 교통량과 접근로 유입 모델."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from random import Random
from re import fullmatch
from xml.etree import ElementTree as ET

from .config import DT
from .observations import KST, TopisClient, TrafficInfoError

# D-10 관측소 유입 방향은 화랑대역에서 태릉입구역으로 향한다.
SITE_VOLUME = {"taereung": ("D-10", "east", 5)}
UNMONITORED_RATE = 300


@dataclass(frozen=True)
class Volume:
    station: str
    hour: str
    fetched_at: str
    count: int
    lanes: int


def parse_volume(payload, station, hour, lanes, fetched_at):
    try:
        root = ET.fromstring(payload)
        code = root.findtext("./RESULT/CODE") or root.findtext("./CODE")
        if code == "INFO-200":
            raise TrafficInfoError("해당 시간대 집계 없음 (INFO-200)")
        if code != "INFO-000":
            if code and fullmatch(r"(?:INFO|ERROR)-[0-9]{3}", code):
                raise TrafficInfoError(f"교통량 API 응답 {code}")
            raise TrafficInfoError("교통량 API 결과 코드 누락")
        counts, directions = {}, set()
        for element in root.findall("row"):
            row = {child.tag.upper(): child.text for child in element}
            if (row["SPOT_NUM"] != station or row["YMD"] != hour.strftime("%Y%m%d")
                    or int(row["HH"]) != hour.hour):
                raise ValueError
            lane, count = int(row["LANE_NUM"]), int(row["VOL"])
            if count < 0 or lane in counts:
                raise ValueError
            counts[lane] = count
            directions.add(row["IO_TYPE"])
        # 공개 명세는 유입만 제공한다. 방향이 섞이거나 차로가 누락된 응답은 제외한다.
        if not counts:
            raise TrafficInfoError("해당 시간대 차량 집계 없음")
        if len(directions) != 1 or None in directions:
            raise TrafficInfoError("유입 방향 확인 불가")
        if len(counts) != lanes:
            raise TrafficInfoError(f"차로 집계 {len(counts)}/{lanes}개")
        return Volume(station, hour.isoformat(), fetched_at.isoformat(), sum(counts.values()), lanes)
    except (ET.ParseError, KeyError, TypeError, ValueError):
        raise TrafficInfoError("교통량 응답 형식 또는 집계값 오류") from None


class VolumeClient:
    def __init__(self, client: TopisClient, mapping):
        self.client, self.mapping = client, mapping
        self.latest = None

    def fetch(self):
        now = datetime.now(KST)
        hour = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
        if self.latest and self.latest.hour == hour.isoformat():
            return None
        station, _, lanes = self.mapping
        try:
            payload = self.client.request(f"VolInfo/1/100/{station}/{hour:%Y%m%d}/{hour:%H}/")
            self.latest = parse_volume(payload, station, hour, lanes, datetime.now(KST))
        except TrafficInfoError as error:
            raise TrafficInfoError(f"D-10 {hour:%m/%d %H}시 · {error}") from None
        return self.latest


class VolumeChange:
    def __init__(self, threshold):
        self.threshold = threshold
        self.previous = None

    def observe(self, volume):
        previous = self.previous
        if previous and volume.hour <= previous.hour:
            return None
        self.previous = volume
        if previous is None or datetime.fromisoformat(volume.hour) - datetime.fromisoformat(previous.hour) != timedelta(hours=1):
            return None
        percent = ((volume.count - previous.count) / previous.count * 100 if previous.count else None)
        if percent is None or abs(percent) >= self.threshold:
            if volume.count != previous.count:
                return {"previous": previous.count, "current": volume.count, "percent": percent}
        return None


def rates_for(site, volume):
    _, measured_lane, _ = SITE_VOLUME[site.id]
    return {lane.id: volume.count / volume.lanes if lane.id == measured_lane else UNMONITORED_RATE
            for lane in site.approaches}


def live_arrivals(site, seed, rates):
    rng = Random(seed)
    number = 0
    while True:
        arrivals = []
        current = rates()
        for lane in site.approaches:
            if rng.random() < current[lane.id] * DT / 3600:
                number += 1
                arrivals.append((lane.id, number))
        yield tuple(arrivals)
