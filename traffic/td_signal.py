"""서울 T-Data C-ITS 10381의 차량 직진 신호를 읽는다."""
import json
import math
from queue import Empty, SimpleQueue
from threading import Event, Thread
from time import time
from urllib.parse import urlencode
from urllib.request import build_opener

from .observations import NoRedirect

URL = "https://t-data.seoul.go.kr/apig/apiman-gateway/tapi/v2xSignalPhaseTimingFusionCurrentInfo/1.0"
DIRECTIONS = {"nt", "et", "st", "wt", "ne", "nw", "se", "sw"}
STATES = {
    "protected-movement-allowed": "green",
    "permissive-movement-allowed": "green",
    "protected-clearance": "yellow",
    "permissive-clearance": "yellow",
    "stop-and-remain": "red",
}


def parse_snapshot(payload, intersection, mapping, now):
    if not isinstance(payload, list):
        raise ValueError("T-Data 응답 형식 확인 필요")
    matching = [row for row in payload if isinstance(row, dict) and str(row.get("itstId")) == intersection]
    if not matching:
        raise ValueError("지정 교차로의 신호 응답 없음")

    def timestamp(row):
        try:
            seconds = float(row.get("trsmUtcTime", 0)) / 1000
            return seconds if math.isfinite(seconds) else 0
        except (TypeError, ValueError):
            return 0

    row = max(matching, key=timestamp)
    observed = timestamp(row)
    if not -2 <= now - observed < 8:
        raise ValueError("신호 관측 시각이 오래되었거나 유효하지 않음")
    phases = {}
    for lane, prefix in mapping.items():
        state = STATES.get(str(row.get(f"{prefix}StsgStatNm")).lower())
        try:
            # 공식 명세의 출력 설명은 1/10초를 명시한다.
            remaining = float(row.get(f"{prefix}StsgRmdrCs")) / 10
        except (TypeError, ValueError):
            remaining = 0
        if not state or not math.isfinite(remaining) or remaining <= 0:
            raise ValueError(f"{prefix.upper()} 직진 신호 또는 잔여시간 미제공 · 방향 확인")
        phases[lane] = state, observed + remaining
    return observed, phases


class SignalPoller:
    def __init__(self, key, intersection, mapping):
        self.stop = Event()
        self.results = SimpleQueue()
        self.snapshot = None
        self.status = "T-Data 첫 조회 대기"
        url = URL + "?" + urlencode({"apikey": key, "itstId": intersection,
                                     "type": "json", "pageNo": 1, "numOfRows": 10})
        Thread(target=self._poll, args=(url, intersection, mapping), daemon=True).start()

    def _poll(self, url, intersection, mapping):
        opener = build_opener(NoRedirect())
        while not self.stop.is_set():
            try:
                with opener.open(url, timeout=4) as response:
                    payload = response.read(262144)
                payload = json.loads(payload)
            except (OSError, ValueError):
                self.results.put((None, "T-Data 연결 또는 응답 해석 실패"))
            else:
                try:
                    self.results.put((parse_snapshot(payload, intersection, mapping, time()), "T-Data 수신"))
                except ValueError as error:
                    self.results.put((None, str(error)))
            self.stop.wait(1)

    def read(self, now=None):
        while True:
            try:
                self.snapshot, self.status = self.results.get_nowait()
            except Empty:
                break
        if not self.snapshot:
            return {}
        observed, states = self.snapshot
        now = time() if now is None else now
        if now - observed >= 8 or any(until <= now for _, until in states.values()):
            self.status = "다음 실측 신호 수신 대기"
            return {}
        return {lane: (state, until - max(now, observed)) for lane, (state, until) in states.items()}

    def close(self):
        self.stop.set()
