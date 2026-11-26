"""Pygame 지도, 시연 제어, 관측과 실험 결과."""
from dataclasses import asdict
from datetime import datetime
from functools import lru_cache
import math
import os
from pathlib import Path

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame as pg

from .config import DT, ROOT, TICKS, districts, load_site, read_json, write_json
from .engine import Simulation
from .experiment import METRIC_BASIS
from .observations import KST, LABELS, LivePoller, parse_time
from .policy import FixedPolicy, QPolicy, load_model
from .scenario import Scenario

SIZE = (1280, 854)
BG = "#eef2f7"
CARD = "#ffffff"
INK = "#152638"
MUTED = "#53667a"
BORDER = "#dbe3ed"
BLUE = "#194eae"
TEAL = "#006e66"
MAP = "#182637"
SIGNALS = {"green": "#4ee1a1", "yellow": "#ffd260", "red": "#fc7079"}
CONDITIONS = {"원활": "#76e3c0", "서행": "#ffd28c", "정체": "#ff9eab"}
CARS = ("#72c6ff", "#f3c674", "#e5eef8", "#b8b6ff", "#7bd4bf")
SNAPSHOTS = {"initial": "학습 전", "middle": "학습 중간", "final": "학습 완료"}
ACTIONS = {0: "다음 방향 전환", 5: "녹색 5초 연장", 10: "녹색 10초 연장"}


@lru_cache(maxsize=24)
def font(size, bold):
    bundled = ROOT / "assets/NotoSansKR.ttf"
    choice = bundled if bundled.exists() else pg.font.match_font("malgungothic,notosanscjkkr")
    face = pg.font.Font(choice, size)
    face.set_bold(bold)
    return face


@lru_cache(maxsize=1024)
def label_surface(value, size, color, bold):
    return font(size, bold).render(value, True, color)


def label(surface, value, pos, size=15, color=INK, *, bold=False, align="topleft"):
    image = label_surface(str(value), size, color, bold)
    surface.blit(image, image.get_rect(**{align: pos}))


def box(surface, rect, fill=CARD, outline=BORDER, radius=12):
    pg.draw.rect(surface, fill, rect, border_radius=radius)
    if outline:
        pg.draw.rect(surface, outline, rect, width=1, border_radius=radius)


def signal_color(simulation, group):
    return SIGNALS[simulation.signal.aspect(group)]


def clock_text(ticks):
    seconds = ticks // TICKS
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class MapView:
    def __init__(self, site, rect):
        self.site, self.rect = site, pg.Rect(rect)
        self.scale = 1.31
        self.base = pg.Surface(self.rect.size)
        self.base.fill(MAP)
        self.origin = self.base.get_rect().center
        self._streets()

    def point(self, x, y):
        return round(self.origin[0] + x * self.scale), round(self.origin[1] + y * self.scale)

    def _streets(self):
        for entry in self.site.drawing["buildings"]:
            pg.draw.polygon(self.base, "#243548", [self.point(x, y) for x, y in entry["polygon"]])
        for entry in self.site.drawing["roads"]:
            points = [self.point(x, y) for x, y in entry["path"]]
            if len(points) > 1:
                thick = 7 if entry["class"] in ("primary", "secondary", "tertiary") else 3
                pg.draw.lines(self.base, "#304256", False, points, thick)
        for approach in self.site.approaches:
            points = [self.point(x, y) for x, y in approach.path]
            pg.draw.lines(self.base, "#67788b", False, points, 13)
        for approach in self.site.approaches:
            points = [self.point(x, y) for x, y in approach.path]
            pg.draw.lines(self.base, "#37495e", False, points, 10)

    def draw(self, surface, simulation, fraction):
        frame = self.base.copy()
        for lane in simulation.lanes.values():
            approach = lane.approach
            x, y, angle = lane.path.at(approach.stop)
            px, py = self.point(x, y)
            nx, ny = -math.sin(angle), math.cos(angle)
            pg.draw.line(frame, "#e0e9f5", (px - 5 * nx, py - 5 * ny),
                         (px + 5 * nx, py + 5 * ny), 2)
            for car in lane.cars:
                distance = car.position - 2.6 - car.speed * DT * (1 - fraction)
                self._vehicle(frame, car.number, *lane.path.at(distance))
            dot = (round(px + nx * 12), round(py + ny * 12))
            pg.draw.circle(frame, "#0b1522", dot, 8)
            pg.draw.circle(frame, signal_color(simulation, approach.group), dot, 5)
            lx, ly = self.point(*approach.label)
            condition = lane.condition()
            box(frame, (lx - 23, ly - 10, 46, 21), "#122032", None, 6)
            label(frame, condition, (lx, ly), 12, CONDITIONS[condition], bold=True, align="center")

        pg.draw.line(frame, "#a7bbd2", (23, 45), (23, 28), 1)
        pg.draw.polygon(frame, "#a7bbd2", [(23, 24), (19, 31), (27, 31)])
        label(frame, "N", (23, 12), 11, "#c2d1e2", align="midtop")
        label(frame, "가상 교통 상태", (13, self.rect.height - 13), 11, "#bbcce0", align="bottomleft")
        label(frame, "© OpenStreetMap contributors", (self.rect.width - 13, self.rect.height - 13),
              11, "#bbcce0", align="bottomright")
        surface.blit(frame, self.rect)

    def _vehicle(self, surface, number, x, y, angle):
        px, py = self.point(x, y)
        dx, dy = math.cos(angle), math.sin(angle)
        nx, ny = -dy, dx

        def point(front, side):
            return (round(px + dx * front + nx * side), round(py + dy * front + ny * side))

        body = [point(-3.4, -1.8), point(3.4, -1.8), point(3.8, 1.8), point(-3.4, 1.8)]
        pg.draw.polygon(surface, CARS[number % len(CARS)], body)
        pg.draw.line(surface, "#a1adbb", point(1.1, -1.5), point(1.1, 1.5), 1)


class Demo:
    def __init__(self, args, site):
        self.args = args
        self.record_site = site.id
        self.running = True
        self.rate = args.speed
        self.snap_path = Path(args.snapshot) if args.snapshot not in SNAPSHOTS else None
        self.site, self.poller = None, None
        self.snapshot = args.snapshot if not self.snap_path else "custom"
        self.region = next(row for row in districts() if row["site"] == site.id)
        self.overview = False
        self.load(site, args.scenario or next(iter(site.scenarios)))

    def load(self, site, scenario_name):
        model_path = self.snap_path if self.snapshot == "custom" else site.model_dir / f"{self.snapshot}.json"
        model = load_model(model_path, site.id)
        recording = self.args.recording if site.id == self.record_site else None
        live_enabled = self.args.mode in ("auto", "live") and not recording
        compression = self.args.recording_compression
        if not recording and self.args.mode != "synthetic" and site.archive and scenario_name == "shift":
            recording = ROOT / site.archive["path"]
            compression = site.archive["compression"] * max(1, 240 / self.args.duration)
        scenario = Scenario(site, scenario_name, self.args.seed, duration=self.args.duration,
                            recording=recording, spacing=compression)
        if not self.site or self.site.id != site.id:
            self.close()
            self.poller = (LivePoller(site, self.args.poll_seconds, ROOT / "runtime/logs")
                           if live_enabled else None)
        self.site, self.model, self.scenario = site, model, scenario
        self.scenario_options = [(key, read_json(ROOT / value)["name"])
                                 for key, value in site.scenarios.items()]
        self.reset()

    def reset(self):
        self.feed = self.scenario.feed(self.poller, max(180, self.args.poll_seconds * 3))
        if self.poller and hasattr(self, "observations"):
            mapped = {a.id: a.link_id for a in self.site.approaches if a.link_id}
            self.feed.live = {lane: obs for lane, obs in self.observations.items()
                              if obs.mode == "live" and mapped.get(lane) == obs.link_id}
        self.observations = self.feed.apply(0)
        self.fixed = Simulation(self.site, FixedPolicy(self.site.signal.fixed))
        self.q = Simulation(self.site, QPolicy(self.model["q_table"]), trace=True)
        self.input_events = []
        self.tick = 0
        self.result_message = ""
        self.save_pending = True

    def close(self):
        if self.poller:
            self.poller.close()

    def step(self):
        current = self.feed.apply(self.tick)
        if current != self.observations or not self.input_events:
            self.input_events.append({"at_s": self.tick * DT,
                                      "values": {lane: asdict(obs) for lane, obs in current.items()}})
        self.observations = current
        speeds = {lane: obs.speed_kmh for lane, obs in current.items()}
        arrivals = self.scenario.schedule[self.tick]
        self.fixed.step(arrivals, speeds)
        self.q.step(arrivals, speeds)
        self.tick += 1

    def save(self):
        if not self.save_pending:
            return
        self.save_pending = False
        stamp = datetime.now(KST).strftime("%Y%m%d-%H%M%S-%f")
        path = ROOT / "runtime/results" / f"{self.site.id}-{stamp}.json"
        try:
            write_json(path, {"site": self.site.id, "scenario": self.scenario.id,
                              "seed": self.scenario.seed, "duration_s": self.scenario.duration,
                              "mean_wait_basis": METRIC_BASIS, "arrivals_sha256": self.scenario.signature,
                              "snapshot": self.snapshot, "episode": self.model["metadata"]["episodes"],
                              "inputs": self.input_events, "decisions": self.q.decisions,
                              "fixed": self.fixed.metrics(), "q_learning": self.q.metrics()})
            self.result_message = f"저장됨 · runtime/results/{path.name}"
        except OSError:
            self.result_message = "결과 파일 저장 실패 · 화면에서 결과 확인"

    def input_status(self):
        modes = set(obs.mode for obs in self.observations.values())
        status = " + ".join(LABELS[key] for key in LABELS if key in modes)
        if self.poller and self.poller.reason:
            return f"{status} · {self.poller.reason}"
        if self.feed.log_error:
            return f"{status} · 조회 로그 저장 실패"
        if self.poller and not self.feed.live:
            reason = "TOPIS 연결 실패" if self.feed.failed else "TOPIS 첫 조회 대기"
            return f"{status} · {reason}"
        return status


class Dashboard:
    def __init__(self, screen, demo):
        self.screen, self.demo = screen, demo
        self.buttons = []
        self.popup = None
        self.popup_data = None
        self.error = ""
        self.maps = {}
        self.regions = districts()
        self.overview_maps = {}

    def button(self, rect, name, callback, *, active=False, filled=False):
        self.buttons.append((pg.Rect(rect), callback))
        fill = BLUE if filled else "#e1edff" if active else CARD
        box(self.screen, rect, fill, "#a8c4ef" if active else None if filled else BORDER, 8)
        label(self.screen, name, pg.Rect(rect).center, 14, CARD if filled else BLUE if active else INK,
              bold=active or filled, align="center")

    def select(self, rect, name, key, options, action):
        def toggle():
            self.popup = None if self.popup == key else key
        self.button(rect, name, toggle, active=self.popup == key)
        px, py = rect[0] + rect[2] - 15, rect[1] + rect[3] // 2
        pg.draw.lines(self.screen, MUTED, False, [(px - 4, py - 2), (px, py + 2), (px + 4, py - 2)], 1)
        if self.popup == key:
            self.popup_data = (rect, options, action)

    def draw(self, fraction=1):
        demo, screen = self.demo, self.screen
        self.buttons.clear()
        self.popup_data = None
        screen.fill(BG)
        label(screen, "교통 신호 시뮬레이터", (28, 18), 30, bold=True)
        region = demo.region
        location = region["title"] if demo.overview else demo.site.title
        title = f"서울 {region['name']}  /  {location}"
        label(screen, title, (29, 65), 16, MUTED)
        for index, row in enumerate(self.regions):
            self.button((929 + index * 109, 28, 101, 44), row["name"], lambda r=row: self.region(r),
                        active=row["id"] == region["id"])
        pg.draw.line(screen, BORDER, (26, 101), (1254, 101))
        self.button((27, 118, 104, 37), "지역 개요",
                    lambda: setattr(demo, "overview", True), active=demo.overview)
        if region.get("site"):
            self.button((137, 118, 108, 37), "교차로 상세",
                        lambda: setattr(demo, "overview", False), active=not demo.overview)
        if demo.overview:
            self._overview()
        else:
            self._controls()
            self._panel(demo.fixed, 27, "고정 신호", BLUE, fraction)
            self._panel(demo.q, 649, "강화학습 신호", TEAL, fraction)
            self._footer()
        if self.error:
            label(screen, self.error, (1252, 843), 13, "#a33154", align="bottomright")
        if self.popup_data:
            rect, options, action = self.popup_data
            x, y, width, height = rect
            box(screen, (x, y + height + 4, width, 40 * len(options) + 4))
            for index, (key, name) in enumerate(options):
                self.button((x + 2, y + height + 6 + 40 * index, width - 4, 38),
                            name, lambda k=key: self.choose(action, k))

    def _controls(self):
        demo = self.demo
        self.select((266, 118, 188, 37), demo.scenario.name, "scenario", demo.scenario_options,
                    lambda key: demo.load(demo.site, key))
        snapshots = list(SNAPSHOTS.items())
        if demo.snap_path and demo.site.id == demo.record_site:
            snapshots.append(("custom", "추가 학습 모델"))
        self.select((464, 118, 143, 37), dict(snapshots)[demo.snapshot], "snapshot", snapshots,
                    self.snapshot)
        self.button((623, 118, 87, 37), f"{demo.rate:g}×", self.cycle_rate)
        self.button((719, 118, 88, 37), "일시정지" if demo.running else "실행", self.toggle, filled=True)
        self.button((815, 118, 79, 37), "초기화", demo.reset)
        label(self.screen, f"{clock_text(demo.tick)}  /  {clock_text(demo.scenario.ticks)}",
              (1251, 136), 19, bold=True, align="midright")
        pg.draw.rect(self.screen, BORDER, (26, 168, 1228, 4), border_radius=3)
        width = round(1228 * demo.tick / demo.scenario.ticks)
        if width:
            pg.draw.rect(self.screen, BLUE, (26, 168, width, 4), border_radius=3)

    def _panel(self, sim, x, name, accent, fraction):
        box(self.screen, (x, 186, 604, 452))
        label(self.screen, name, (x + 18, 198), 22, accent, bold=True)
        stages = {"green": "녹색", "yellow": "황색", "all_red": "전적색"}
        aspect = sim.signal.phase
        phase_name = stages[aspect]
        if aspect != "all_red":
            phase_name = f"{sim.site.groups[sim.signal.group]} {phase_name}"
        phase_text = f"{phase_name}  {sim.signal.elapsed * DT:04.1f}초"
        pg.draw.circle(self.screen, signal_color(sim, sim.signal.group), (x + 405, 215), 5)
        label(self.screen, phase_text, (x + 587, 214), 15, INK, bold=True, align="midright")
        key = (sim.site.id, x)
        if key not in self.maps:
            self.maps[key] = MapView(sim.site, (x + 9, 234, 586, 351))
        self.maps[key].draw(self.screen, sim, fraction)
        if name == "고정 신호":
            message = f"양 방향 녹색 각 {sim.site.signal.fixed}초"
        elif not sim.last_action:
            message = f"첫 판단 대기 · 최소 녹색 {sim.site.signal.minimum}초"
        else:
            tick, action, kind = sim.last_action
            choice = "최대 녹색 도달 · 자동 전환" if kind == "maximum" else ACTIONS[action]
            message = f"최근 판단   {choice}   ·   {clock_text(tick)}"
        label(self.screen, message, (x + 18, 608), 15, accent, bold=True, align="midleft")

    def _footer(self):
        demo = self.demo
        screen = self.screen
        box(screen, (27, 654, 721, 175))
        box(screen, (760, 654, 493, 175))
        label(screen, "외부 도로 소통 입력", (45, 665), 17, bold=True)
        label(screen, demo.input_status(), (730, 677), 13, BLUE, align="midright")
        self._observations(45, 697, 700)
        label(screen, "가상 주행 비교", (778, 665), 17, bold=True)
        status = "시연 완료" if demo.tick >= demo.scenario.ticks else "진행 중" if demo.running else "일시정지"
        label(screen, status, (1234, 678), 13, TEAL, bold=True, align="midright")
        fixed, q = demo.fixed.metrics(), demo.q.metrics()
        for x, title, metrics, color in ((778, "고정 신호", fixed, BLUE), (934, "강화학습 신호", q, TEAL)):
            label(screen, title, (x, 702), 12, color, bold=True)
            label(screen, f"{metrics['mean_wait_s']:.1f}초", (x, 721), 25, INK, bold=True)
            label(screen, f"정지선 통과 {metrics['crossed_stop_line']}대", (x, 761), 13, MUTED)
        label(screen, "대기 차이 (AI − 고정)", (1093, 702), 12, MUTED)
        difference = q["mean_wait_s"] - fixed["mean_wait_s"]
        label(screen, f"{difference:+.1f}초", (1093, 721), 24, TEAL if difference < 0 else INK, bold=True)
        message = demo.result_message or "차량당 평균 대기 · 예정 도착 전체 기준"
        label(screen, message, (778, 803), 11, MUTED)

    def _observations(self, x, y, width):
        demo = self.demo
        for index, approach in enumerate(demo.site.approaches):
            line = y + index * 28
            obs = demo.observations.get(approach.id)
            if not obs:
                continue
            stamp = parse_time(obs.fetched_at).astimezone(KST).strftime("%H:%M:%S") if obs.fetched_at else "—"
            mode = LABELS[obs.mode]
            if demo.feed.is_stale(approach.id):
                mode += " · 오래됨"
            if obs.mode == "synthetic":
                mode += " · 링크 없음" if not approach.link_id else ""
            pg.draw.line(self.screen, BORDER, (x, line + 26), (x + width - 18, line + 26))
            label(self.screen, approach.road, (x, line + 3), 13, bold=True)
            label(self.screen, approach.direction, (x + 83, line + 3), 13, MUTED)
            label(self.screen, f"{obs.speed_kmh:g} km/h", (x + 177, line + 3), 13, bold=True)
            label(self.screen, f"조회 {stamp}" if obs.fetched_at else "조회 —", (x + 269, line + 3), 12, MUTED)
            if obs.observed_at:
                observed = parse_time(obs.observed_at).astimezone(KST)
                label(self.screen, f"관측 {observed:%m/%d %H:%M}", (x + 364, line + 3), 12, MUTED)
            if obs.link_id and not obs.observed_at:
                label(self.screen, obs.link_id, (x + 379, line + 3), 12, MUTED)
            label(self.screen, mode, (x + width - 16, line + 3), 12, BLUE, align="topright")

    def _overview(self):
        demo, region = self.demo, self.demo.region
        screen = self.screen
        box(screen, (27, 176, 785, 646))
        box(screen, (826, 176, 427, 646))
        label(screen, f"{region['locality']} 도로 지도", (47, 193), 21, bold=True)
        label(screen, "교통량 조사지점", (848, 193), 20, bold=True)
        for index, item in enumerate(region["volume_sites"]):
            label(screen, item, (848, 239 + 28 * index), 16, MUTED)
        if region.get("site"):
            roads, size = (demo.site.drawing[key] for key in ("overview", "overview_size"))
            label(screen, "상세 시뮬레이션", (848, 337), 20, bold=True)
            self.button((848, 375, 348, 48), region["title"] + " 열기",
                        lambda: setattr(demo, "overview", False), filled=True)
            label(screen, "대표 교차로 접근로 입력", (848, 464), 17, bold=True)
            for index, approach in enumerate(demo.site.approaches):
                obs = demo.observations[approach.id]
                cond = "원활" if obs.speed_kmh >= 25 else "서행" if obs.speed_kmh >= 15 else "정체"
                old = " · 오래됨" if demo.feed.is_stale(approach.id) else ""
                label(screen, f"{approach.road}  {approach.direction}  {obs.speed_kmh:g} km/h",
                      (848, 507 + index * 43), 13, bold=True)
                label(screen, f"{cond}  ·  {LABELS[obs.mode]}{old}",
                      (848, 525 + index * 43), 12, MUTED)
        else:
            if region["id"] not in self.overview_maps:
                raw = read_json(ROOT / region["overview"])
                roads, size = raw["roads"], raw["size"]
                self.overview_observations = raw["observations"]
            label(screen, "상세 교차로 · 준비 중", (848, 337), 18, MUTED)
            label(screen, "TOPIS 관측 구간 · 2020-11-16 18시", (848, 390), 16, MUTED)
            for index, item in enumerate(self.overview_observations):
                speed = item["speed_kmh"]
                condition = "원활" if speed >= 25 else "서행" if speed >= 15 else "정체"
                y = 435 + 62 * index
                label(screen, item["road"] + "  " + item["section"], (848, y), 14, bold=True)
                label(screen, f"{speed:g} km/h  ·  {condition}  ·  저장 기록", (848, y + 24), 13, BLUE)
        if region["id"] not in self.overview_maps:
            self.overview_maps[region["id"]] = self._overview_tiles(region, roads, size)
        screen.blit(self.overview_maps[region["id"]], (38, 236))

    def _overview_tiles(self, region, roads, size):
        map_surface = pg.Surface((762, 573))
        map_surface.fill(MAP)
        scale = min(745 / size[0], 552 / size[1])
        for road in roads:
            points = [(round(381 + x * scale), round(283 + y * scale)) for x, y in road["path"]]
            if len(points) > 1:
                main = road["class"] in ("primary", "secondary")
                pg.draw.lines(map_surface, "#829ab7" if main else "#43566e", False, points, 4 if main else 2)
        if not region.get("site"):
            for item in self.overview_observations:
                speed = item["speed_kmh"]
                condition = "원활" if speed >= 25 else "서행" if speed >= 15 else "정체"
                points = [(round(381 + x * scale), round(283 + y * scale)) for x, y in item["path"]]
                pg.draw.lines(map_surface, CONDITIONS[condition], False, points, 4)
        if region.get("site"):
            pg.draw.circle(map_surface, "#87dff5", (381, 283), 8)
            pg.draw.circle(map_surface, "#17509a", (381, 283), 4)
            label(map_surface, region["title"], (395, 273), 14, "#ecf7ff", bold=True)
        label(map_surface, "© OpenStreetMap contributors", (744, 553), 12, "#bbcce0", align="bottomright")
        return map_surface

    def choose(self, callback, key):
        self.popup = None
        callback(key)

    def snapshot(self, key):
        demo = self.demo
        if key == "custom" and demo.site.id != demo.record_site:
            raise ValueError("추가 학습 모델의 원래 지역에서 선택하세요.")
        previous = demo.snapshot
        demo.snapshot = key
        try:
            demo.load(demo.site, demo.scenario.id)
        except (ValueError, OSError, KeyError):
            demo.snapshot = previous
            raise

    def region(self, row):
        demo = self.demo
        if demo.region == row:
            return
        if row.get("site"):
            site = load_site(row["site"])
            if demo.snapshot == "custom" and demo.record_site != site.id:
                demo.snapshot = "final"
            demo.load(site, next(iter(site.scenarios)))
        demo.region = row
        demo.overview = True
        self.popup = None

    def cycle_rate(self):
        demo = self.demo
        rates = (1, 2, 3, 5, 10)
        demo.rate = rates[(rates.index(demo.rate) + 1) % len(rates)] if demo.rate in rates else 1

    def toggle(self):
        if self.demo.tick >= self.demo.scenario.ticks:
            self.demo.reset()
            self.demo.running = True
        else:
            self.demo.running = not self.demo.running

    def click(self, pos):
        self.error = ""
        for rect, callback in reversed(self.buttons):
            if rect.collidepoint(pos):
                try:
                    callback()
                except (ValueError, OSError, KeyError) as error:
                    self.error = str(error)
                return
        self.popup = None


def launch(args, site):
    if args.headless:
        os.environ["SDL_VIDEODRIVER"] = "dummy"
    pg.display.init()
    pg.font.init()
    pg.display.set_caption("서울 교통 신호 시뮬레이터")
    flags = 0 if args.headless else pg.SCALED | pg.RESIZABLE
    screen = pg.display.set_mode(SIZE, flags)
    demo = Demo(args, site)
    dashboard = Dashboard(screen, demo)
    try:
        if args.screenshot:
            for _ in range(min(demo.scenario.ticks, args.advance * TICKS)):
                demo.step()
            dashboard.draw()
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            pg.image.save(screen, args.screenshot)
            return 0
        clock = pg.time.Clock()
        backlog = 0.0
        while True:
            elapsed = min(clock.tick(60) / 1000, 0.25)
            prior_tick = demo.tick
            for event in pg.event.get():
                if event.type == pg.QUIT:
                    return 0
                if event.type == pg.KEYDOWN and event.key == pg.K_SPACE:
                    dashboard.toggle()
                if event.type == pg.KEYDOWN and event.key == pg.K_ESCAPE:
                    dashboard.popup = None
                if event.type == pg.MOUSEBUTTONDOWN and event.button == 1:
                    dashboard.click(event.pos)
            if demo.tick < prior_tick:
                backlog = 0.0
            if demo.running and demo.tick < demo.scenario.ticks:
                backlog += elapsed * demo.rate
                while backlog >= DT and demo.tick < demo.scenario.ticks:
                    demo.step()
                    backlog -= DT
            if demo.tick == demo.scenario.ticks:
                demo.running = False
                demo.save()
            fraction = min(1, backlog / DT) if demo.running else 1
            dashboard.draw(fraction)
            pg.display.flip()
    finally:
        demo.close()
        pg.quit()
