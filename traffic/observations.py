"""TOPIS 조회, JSONL 기록, 입력 재생에 공통으로 쓰는 관측 구조."""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, build_opener
from xml.etree import ElementTree as ET

KST = timezone(timedelta(hours=9), "KST")
API = "http://openapi.seoul.go.kr:8088"
LABELS = {"live": "라이브", "recorded": "기록 재생", "synthetic": "합성 예제",
          "synthetic_recorded": "기록 재생(합성)"}


@dataclass(frozen=True)
class Observation:
    speed_kmh: float
    mode: str
    link_id: str | None = None
    travel_seconds: float | None = None
    fetched_at: str | None = None
    observed_at: str | None = None

    def __post_init__(self):
        if not 0 < self.speed_kmh <= 150:
            raise ValueError("유효하지 않은 속도")
        if self.mode not in LABELS:
            raise ValueError("유효하지 않은 관측 입력 모드")
        for stamp in (self.fetched_at, self.observed_at):
            if stamp:
                parse_time(stamp)

    def outdated(self, seconds, now=None):
        stamp = self.observed_at or self.fetched_at
        return bool(stamp and ((now or datetime.now(KST)) - parse_time(stamp)).total_seconds() > seconds)


def parse_time(stamp):
    if len(stamp) == 14 and stamp.isdigit():
        return datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=KST)
    value = datetime.fromisoformat(stamp)
    return value if value.tzinfo else value.replace(tzinfo=KST)


def valid_number(value, maximum):
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) and 0 < number <= maximum else None


class TrafficInfoError(Exception):
    pass


def parse_payload(payload, link_id, fetched_at):
    if not isinstance(payload, dict):
        raise TrafficInfoError("응답 형식 오류")
    service = payload.get("TrafficInfo", {})
    if not isinstance(service, dict):
        raise TrafficInfoError("서비스 응답 형식 오류")
    result = service.get("RESULT", payload.get("RESULT", {}))
    code = result.get("CODE") if isinstance(result, dict) else None
    if code != "INFO-000":
        safe_codes = {"INFO-100", "INFO-200", "ERROR-300", "ERROR-301", "ERROR-310",
                      "ERROR-331", "ERROR-332", "ERROR-333", "ERROR-334", "ERROR-335",
                      "ERROR-336", "ERROR-500", "ERROR-600", "ERROR-601"}
        raise TrafficInfoError(f"TOPIS {code}" if code in safe_codes else "TOPIS 응답 오류")

    rows = service.get("row", [])
    if not isinstance(rows, list):
        raise TrafficInfoError("관측 행 형식 오류")
    for row in rows:
        if not isinstance(row, dict):
            continue
        row = {str(key).upper(): value for key, value in row.items()}
        if str(row.get("LINK_ID")) != link_id:
            continue
        speed = valid_number(row.get("PRCS_SPD"), 150)
        if speed is None:
            continue
        travel = valid_number(row.get("PRCS_TRV_TIME"), 86400)
        observed = None
        for field in ("OBSERVED_AT", "PRCS_DT", "TRF_OBS_TM"):
            if row.get(field):
                try:
                    observed = parse_time(str(row[field])).isoformat()
                    break
                except ValueError:
                    continue
        return Observation(speed, "live", link_id, travel, fetched_at, observed)
    raise TrafficInfoError("일치하는 유효 관측 없음")


def parse_xml(content, link_id, fetched_at):
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        raise TrafficInfoError("XML 응답 형식 오류") from None

    def fields(element):
        return {child.tag.upper(): child.text for child in element}

    result = next((fields(child) for child in root if child.tag.upper() == "RESULT"), {})
    if not result and root.tag != "TrafficInfo":
        result = fields(root)
    rows = [fields(child) for child in root if child.tag.lower() == "row"]
    return parse_payload({"TrafficInfo": {"RESULT": result, "row": rows}}, link_id, fetched_at)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TopisClient:
    def __init__(self, key, timeout=5):
        self.key, self.timeout = key, timeout
        self.opener = build_opener(NoRedirect())

    def fetch(self, link_id):
        payload = self.request(f"TrafficInfo/1/5/{quote(link_id, safe='')}")
        return parse_xml(payload, link_id, datetime.now(KST).isoformat(timespec="seconds"))

    def request(self, path):
        url = f"{API}/{quote(self.key, safe='')}/xml/{path}"
        try:
            with self.opener.open(url, timeout=self.timeout) as response:
                payload = response.read(256 * 1024)
        except HTTPError as error:
            raise TrafficInfoError(f"HTTP {error.code}") from None
        except (OSError, URLError, ValueError):
            raise TrafficInfoError("연결 또는 응답 해석 실패") from None
        return payload


@dataclass(frozen=True)
class PollResult:
    values: dict
    failed: frozenset
    log_error: bool = False
    volume: object = None
    volume_error: str | None = None


class LivePoller:
    def __init__(self, site, interval=60, log_dir=None, client=None, volume_mapping=None):
        self.site_id = site.id
        self.links = {a.id: a.link_id for a in site.approaches if a.link_id}
        self.interval = interval
        self.stop_event = Event()
        self.results = Queue()
        self.thread = None
        key = os.environ.get("SEOUL_OPEN_API_KEY", "").strip()
        self.reason = ""
        if not self.links:
            self.reason = "확인된 TOPIS 링크 미등록"
        elif not key and client is None:
            self.reason = "TOPIS 인증키 미설정"
        else:
            self.client = client or TopisClient(key)
            self.log_path = Path(log_dir) / f"{site.id}.jsonl" if log_dir else None
            self.volume_client = None
            if volume_mapping:
                from .volume import VolumeClient
                self.volume_client = VolumeClient(self.client, volume_mapping)
            self.thread = Thread(target=self._run, name="topis-poll", daemon=True)
            self.thread.start()

    def _run(self):
        while not self.stop_event.is_set():
            started = monotonic()
            values, failed = {}, set()
            log_error = False
            for approach, link in self.links.items():
                if self.stop_event.is_set():
                    return
                try:
                    values[approach] = self.client.fetch(link)
                except TrafficInfoError:
                    failed.add(approach)
            if values and self.log_path is not None:
                try:
                    self.log_path.parent.mkdir(parents=True, exist_ok=True)
                    log = {approach: asdict(observation) for approach, observation in values.items()}
                    with self.log_path.open("a", encoding="utf-8") as file:
                        file.write(json.dumps({"site": self.site_id, "values": log}, ensure_ascii=False) + "\n")
                except OSError:
                    log_error = True
            volume = volume_error = None
            if self.volume_client:
                try:
                    volume = self.volume_client.fetch()
                    if volume and self.log_path:
                        volume_path = self.log_path.with_name(f"{self.site_id}-volume.jsonl")
                        volume_path.parent.mkdir(parents=True, exist_ok=True)
                        with volume_path.open("a", encoding="utf-8") as file:
                            file.write(json.dumps(asdict(volume), ensure_ascii=False) + "\n")
                except TrafficInfoError as error:
                    volume_error = str(error)
                except OSError:
                    log_error = True
            self.results.put(PollResult(values, frozenset(failed), log_error, volume, volume_error))
            self.stop_event.wait(max(0, self.interval - (monotonic() - started)))

    def drain(self):
        while True:
            try:
                yield self.results.get_nowait()
            except Empty:
                return

    def close(self):
        self.stop_event.set()


class ObservationFeed:
    def __init__(self, timeline, poller=None, stale_after=180):
        self.timeline = iter(timeline)
        self.next = next(self.timeline, None)
        self.replay = {}
        self.live = {}
        self.poller = poller
        self.stale_after = stale_after
        self.failed = set()
        self.log_error = False
        self.volume_error = None

    def apply(self, tick, on_poll=None):
        while self.next is not None and self.next[0] <= tick:
            self.replay.update(self.next[1])
            self.next = next(self.timeline, None)
        if self.poller:
            for result in self.poller.drain():
                fresh = {}
                for lane, obs in result.values.items():
                    previous = self.live.get(lane)
                    if (previous and previous.observed_at and obs.observed_at
                            and parse_time(obs.observed_at) < parse_time(previous.observed_at)):
                        continue
                    self.live[lane] = obs
                    if not obs.outdated(self.stale_after):
                        fresh[lane] = obs
                self.failed = result.failed
                self.volume_error = result.volume_error
                self.log_error = self.log_error or result.log_error
                if on_poll:
                    on_poll(fresh, result.volume)
        return {**self.replay, **self.live}

    def is_stale(self, lane):
        obs = self.live.get(lane)
        return bool(obs and (lane in self.failed or obs.outdated(self.stale_after)))


def read_recording(path, site, ticks_per_second, spacing=1):
    """조회 시각 또는 보관된 관측 시각을 시뮬레이션 시간으로 옮긴다."""
    if not 0 < spacing:
        raise ValueError("기록 시간 압축 배율은 양수여야 합니다.")
    known = {a.id: a.link_id for a in site.approaches}
    rows = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            if record.get("site", site.id) != site.id:
                continue
            synthetic = record.get("synthetic") is True
            historical = record.get("kind") == "topis_hourly"
            for lane, raw in record["values"].items():
                if lane not in known:
                    continue
                if synthetic and raw.get("link_id") is None:
                    obs = Observation(**{**raw, "mode": "synthetic_recorded"})
                elif (known[lane] and raw["link_id"] == known[lane]
                      and (raw.get("mode") == "live" or historical and raw.get("mode") == "recorded")):
                    obs = Observation(**{**raw, "mode": "recorded"})
                else:
                    continue
                rows.append((parse_time(obs.fetched_at or obs.observed_at), lane, obs))
        except (KeyError, AttributeError, TypeError, ValueError) as error:
            raise ValueError(f"기록 {line_number}번째 줄의 형식이 잘못되었습니다.") from error
    if not rows:
        raise ValueError("기록에 현재 지도와 일치하는 관측이 없습니다.")
    rows.sort(key=lambda row: row[0])
    start = rows[0][0]
    events = {}
    for stamp, lane, obs in rows:
        tick = round((stamp - start).total_seconds() * ticks_per_second / spacing)
        events.setdefault(tick, {})[lane] = obs
    return tuple(events.items())
