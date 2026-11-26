"""TOPIS 시간별 교통량과 접근로 유입 모델."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from random import Random
from decimal import Decimal
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


def volume_integer(row, name):
    value = row.get(name, "")
    if not fullmatch(r"[0-9]{1,9}(?:\.0+)?", value):
        raise TrafficInfoError(f"교통량 {name} 필드 누락 또는 정수 형식 오류")
    return int(Decimal(value))


def parse_volume(payload, station, hour, lanes, fetched_at):
    try:
        root = ET.fromstring(payload)
        code = (root.findtext("./RESULT/CODE") or root.findtext("./CODE") or "").strip()
        if code == "INFO-200":
            raise TrafficInfoError("해당 시간대 집계 없음 (INFO-200)")
        if code != "INFO-000":
            if code and fullmatch(r"(?:INFO|ERROR)-[0-9]{3}", code):
                raise TrafficInfoError(f"교통량 API 응답 {code}")
            raise TrafficInfoError("교통량 API 결과 코드 누락")
        counts, directions = {}, set()
        for element in root.findall("row"):
            row = {child.tag.upper(): (child.text or "").strip() for child in element}
            if row.get("SPOT_NUM") != station:
                raise TrafficInfoError("요청 지점과 응답 지점 불일치")
            if row.get("YMD", "").replace("-", "") != hour.strftime("%Y%m%d"):
                raise TrafficInfoError("요청 날짜와 응답 날짜 불일치")
            if volume_integer(row, "HH") != hour.hour:
                raise TrafficInfoError("요청 시간과 응답 시간 불일치")
            lane, count = volume_integer(row, "LANE_NUM"), volume_integer(row, "VOL")
            if lane in counts:
                raise TrafficInfoError("교통량 응답에 같은 차로 번호 중복")
            counts[lane] = count
            directions.add(row.get("IO_TYPE"))
        # 공개 명세는 유입만 제공한다. 방향이 섞이거나 차로가 누락된 응답은 제외한다.
        if not counts:
            raise TrafficInfoError("해당 시간대 차량 집계 없음")
        if len(directions) != 1 or not all(directions):
            raise TrafficInfoError("유입 방향 확인 불가")
        if len(counts) != lanes:
            raise TrafficInfoError(f"차로 집계 {len(counts)}/{lanes}개")
        return Volume(station, hour.isoformat(), fetched_at.isoformat(), sum(counts.values()), lanes)
    except ET.ParseError:
        raise TrafficInfoError("교통량 XML 응답 해석 실패") from None


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


def fresh_volume(volume):
    return volume if volume and datetime.now(KST) - datetime.fromisoformat(volume.hour) < timedelta(hours=2) else None


def rates_for(site, volume):
    _, measured_lane, _ = SITE_VOLUME[site.id]
    return {lane.id: volume.count / volume.lanes if volume and lane.id == measured_lane else UNMONITORED_RATE
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
