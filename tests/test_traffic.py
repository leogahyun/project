from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest
from unittest.mock import patch
from urllib.error import URLError

os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
import pygame as pg

from main import parser
from traffic.config import DT, ROOT, TICKS, districts, load_site
from traffic.engine import GAP, Lane, Simulation, Vehicle
from traffic.experiment import run
from traffic.observations import (LivePoller, Observation, ObservationFeed, PollResult,
                                  TopisClient, TrafficInfoError, parse_xml, read_recording)
from traffic.policy import FixedPolicy, QPolicy, QueuePolicy, load_model, save_model, state_key
from traffic.scenario import Scenario
from traffic.signals import Action, Signal
from traffic.ui import Dashboard, Demo, SIGNALS, SIZE, signal_color

STAMP = "2026-01-12T08:00:00+09:00"
# DOCUMENTED 필드 구조와 공개 예시 링크 1220003800을 사용한 합성 XML
XML = b"""<TrafficInfo><list_total_count>1</list_total_count>
<RESULT><CODE>INFO-000</CODE></RESULT><row><link_id>1220003800</link_id>
<prcs_spd>23.5</prcs_spd><prcs_trv_time>39</prcs_trv_time></row></TrafficInfo>"""


class SignalTests(unittest.TestCase):
    def test_phase_lengths_and_complete_red_intervals(self):
        site = load_site("seolleung")
        for desired, red_seconds in ((10, 15), (30, 35), (60, 65)):
            signal = Signal(site.signal)
            policy = FixedPolicy(desired)
            segments = defaultdict(list)
            for _ in range(1500 * TICKS):
                allowed = signal.allowed()
                if allowed:
                    signal.request(policy.decide((0, 0, signal.group, signal.elapsed // TICKS), allowed))
                self.assertFalse(all(signal.aspect(i) == "green" for i in (0, 1)))
                counts = segments["phase"]
                current = (signal.group, signal.phase)
                if not counts or counts[-1][0] != current:
                    counts.append([current, 0])
                counts[-1][1] += 1
                for group in (0, 1):
                    counts = segments[group]
                    current = signal.aspect(group)
                    if not counts or counts[-1][0] != current:
                        counts.append([current, 0])
                    counts[-1][1] += 1
                signal.advance()
            for index, ((group, phase), ticks) in enumerate(segments["phase"][:-1]):
                self.assertEqual(ticks, {"green": desired, "yellow": 3, "all_red": 1}[phase] * TICKS)
                following = segments["phase"][index + 1][0]
                expected = {"green": (group, "yellow"), "yellow": (group, "all_red"),
                            "all_red": (1 - group, "green")}
                self.assertEqual(following, expected[phase])
            for group in (0, 1):
                self.assertTrue(any(phase == "red" for phase, _ in segments[group][1:-1]))
                for phase, ticks in segments[group][1:-1]:
                    if phase == "red":
                        self.assertEqual(ticks / TICKS, red_seconds)

    def test_decisions_after_minimum_and_maximum(self):
        signal = Signal(load_site("taereung").signal)
        self.assertEqual(signal.allowed(), ())
        with self.assertRaises(ValueError):
            signal.request(Action.SWITCH)
        for elapsed in range(60 * TICKS):
            if signal.allowed():
                if elapsed == 55 * TICKS:
                    self.assertEqual(signal.allowed(), (Action.SWITCH, Action.EXTEND_5))
                    with self.assertRaises(ValueError):
                        signal.request(Action.EXTEND_10)
                signal.request(Action.EXTEND_5)
            signal.advance()
        self.assertEqual(signal.phase, "yellow")
        self.assertTrue(signal.closed)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.site = load_site("seolleung")

    def test_queue_spacing_no_crossing_on_red_and_clearance(self):
        lane = Lane(self.site.approaches[0])
        count = 45
        lane.pending.extend(Vehicle(i) for i in range(count))
        for _ in range(45 * TICKS):
            lane.step(False, 12, 13)
            positions = [car.position for car in lane.cars]
            self.assertTrue(all(left - right >= GAP - 1e-8 for left, right in zip(positions, positions[1:])))
        self.assertEqual(lane.crossed, 0)
        self.assertEqual(lane.cars[0].position, lane.approach.stop)
        self.assertGreater(lane.queue, 0)
        self.assertGreater(len(lane.pending), 0)
        lane.step(True, 12, 13)
        self.assertEqual(lane.crossed, 1)
        lead = lane.cars[0]
        before = lead.position
        for _ in range(4 * TICKS):
            lane.step(False, 12, 13)
        self.assertGreater(lead.position, before)
        self.assertEqual(lane.crossed, 1)
        for _ in range(90 * TICKS):
            lane.step(True, 12, 13)
        self.assertEqual(lane.exited, count)
        self.assertFalse(lane.pending)

    def test_three_policies_share_a_reproducible_schedule(self):
        scenario = Scenario(self.site, "shift", 682)
        another = Scenario(self.site, "shift", 682, speed_override=12)
        self.assertEqual(scenario.schedule, another.schedule)
        self.assertEqual(scenario.signature, another.signature)
        self.assertNotEqual(scenario.signature, Scenario(self.site, "shift", 683).signature)
        model = load_model(self.site.model_dir / "final.json", self.site.id)
        by_policy = run(self.site, scenario, {"fixed": FixedPolicy(30), "queue": QueuePolicy(),
                                               "q": QPolicy(model["q_table"])})
        expected = sum(map(len, scenario.schedule))
        for metrics in by_policy.values():
            self.assertEqual(metrics["arrivals"], expected)
            self.assertEqual(sum(metrics[key] for key in ("exited", "on_road_end", "pending_end")), expected)
            self.assertAlmostEqual(metrics["mean_wait_s"], metrics["total_wait_vehicle_s"] / expected, places=4)

    def test_low_speed_keeps_more_cars_with_same_inflow(self):
        fast = Scenario(self.site, "evening", 17, speed_override=45)
        slow = Scenario(self.site, "evening", 17, speed_override=10)
        self.assertEqual(fast.signature, slow.signature)
        totals = []
        for scenario in (fast, slow):
            sim = Simulation(self.site, FixedPolicy(30))
            cars, congestion = 0, 0
            for arrivals, speeds in scenario.tick_inputs():
                sim.step(arrivals, speeds)
                cars += sum(len(lane.cars) for lane in sim.lanes.values())
                congestion += sum(lane.condition() == "정체" for lane in sim.lanes.values())
            totals.append((cars, congestion))
        self.assertGreater(totals[1][0], totals[0][0])
        self.assertGreater(totals[1][1], totals[0][1])


class QTests(unittest.TestCase):
    def test_smdp_reward_allowed_bootstrap_and_terminal(self):
        state, following = (0, 2, 0, 55), (4, 2, 1, 55)
        self.assertNotEqual(state_key(state), state_key((1, 2, 0, 55)))
        table = {state_key(state): [2.0, 0.0, 200.0], state_key(following): [4.0, 8.0, 100.0]}
        policy = QPolicy(table, training=True, alpha=1, gamma=0.9)
        allowed = (Action.SWITCH, Action.EXTEND_5)
        self.assertEqual(policy.decide(state, allowed), Action.SWITCH)
        for reward in (-2.0, -3.0, -4.0):
            policy.observe(reward)
        self.assertEqual(policy.decide(following, allowed), Action.EXTEND_5)
        self.assertAlmostEqual(table[state_key(state)][0], -2 - 0.9 * 3 - 0.9**2 * 4 + 0.9**3 * 8)
        policy.observe(-7)
        policy.finish()
        self.assertEqual(table[state_key(following)][1], -7)

    def test_save_load_epsilon_and_no_learning_in_evaluation(self):
        state = (0, 1, 0, 10)
        allowed = tuple(Action)
        with TemporaryDirectory() as temp:
            path = Path(temp) / "model.json"
            save_model(path, {state_key(state): [2, 5, 3]}, {"site": "seolleung", "episodes": 2})
            data = load_model(path, "seolleung")
            table = data["q_table"]
            with self.assertRaises(ValueError):
                load_model(path, "taereung")
        frozen = json.dumps(table, sort_keys=True)
        eval_policy = QPolicy(table, epsilon=1, seed=8)
        train_policy = QPolicy({key: values[:] for key, values in table.items()},
                               training=True, epsilon=1, seed=8)
        for _ in range(15):
            self.assertEqual(eval_policy.decide(state, allowed), Action.EXTEND_5)
            eval_policy.observe(-200)
        self.assertEqual(len({train_policy.decide(state, allowed) for _ in range(15)}), 3)
        eval_policy.finish()
        self.assertEqual(json.dumps(table, sort_keys=True), frozen)


def site_with_link():
    site = load_site("seolleung")
    return replace(site, approaches=(replace(site.approaches[0], link_id="1220003800"),
                                     *(replace(lane, link_id=None) for lane in site.approaches[1:])))


class ObservationTests(unittest.TestCase):
    def test_reference_xml_http_request_and_invalid_values(self):
        client = TopisClient("test_key", timeout=1)
        with patch.object(client.opener, "open", return_value=io.BytesIO(XML)) as first:
            result = client.fetch("1220003800")
            self.assertTrue(first.call_args.args[0].endswith("/xml/TrafficInfo/1/5/1220003800"))
            self.assertEqual(first.call_args.kwargs["timeout"], 1)
        self.assertEqual(result.speed_kmh, 23.5)
        self.assertEqual(result.travel_seconds, 39)
        self.assertIsNone(result.observed_at)
        self.assertEqual(parse_xml(XML.replace(b"39", b"NaN"), "1220003800", STAMP).travel_seconds, None)
        for value in (b"NaN", b"-1", b"0", b"151"):
            with self.assertRaises(TrafficInfoError):
                parse_xml(XML.replace(b"23.5", value), "1220003800", STAMP)
        with patch.object(client.opener, "open", side_effect=URLError("test_key")):
            with self.assertRaises(TrafficInfoError) as failure:
                client.fetch("1220003800")
            self.assertNotIn("test_key", str(failure.exception))
        with self.assertRaises(TrafficInfoError):
            parse_xml(b"<RESULT><CODE>INFO-100</CODE></RESULT>", "1220003800", STAMP)

    def test_no_key_and_failed_worker_keep_offline_available(self):
        site = site_with_link()
        scenario = Scenario(site, "shift", 9)
        with patch.dict(os.environ, {"SEOUL_OPEN_API_KEY": ""}):
            poller = LivePoller(site)
            self.assertIsNone(poller.thread)
            self.assertIn("인증키", poller.reason)
            self.assertEqual(scenario.feed(poller).apply(0)["west"].mode, "synthetic")

        class FailingClient:
            def fetch(self, link_id):
                raise TrafficInfoError("실패")

        poller = LivePoller(site, interval=60, client=FailingClient())
        try:
            feed = scenario.feed(poller)
            deadline = monotonic() + 1
            while not feed.failed and monotonic() < deadline:
                sleep(0.01)
                self.assertEqual(feed.apply(0)["west"].mode, "synthetic")
            self.assertEqual(feed.failed, {"west"})
        finally:
            poller.close()
            poller.thread.join(timeout=1)

    def test_stale_live_value_and_explicit_recording_modes(self):
        site = site_with_link()
        scenario = Scenario(site, "shift", 9)
        old = Observation(25, "live", "1220003800", 39, STAMP)
        self.assertTrue(old.outdated(180, datetime.fromisoformat(STAMP) + timedelta(minutes=5)))
        with patch.dict(os.environ, {"SEOUL_OPEN_API_KEY": ""}):
            poller = LivePoller(site)
            feed = ObservationFeed(scenario.timeline, poller)
            poller.results.put(PollResult({"west": old}, frozenset({"west"})))
            self.assertEqual(feed.apply(0)["west"], old)
            self.assertTrue(feed.is_stale("west"))
        with TemporaryDirectory() as temp:
            path = Path(temp) / "log.jsonl"
            raw = {"fetched_at": STAMP, "mode": "live", "link_id": "1220003800", "speed_kmh": 23.5}
            path.write_text(json.dumps({"site": "seolleung", "values": {"west": raw}}) + "\n",
                            encoding="utf-8")
            self.assertEqual(read_recording(path, site, TICKS)[0][1]["west"].mode, "recorded")
        sample = ROOT / "data/recordings/seolleung_example.jsonl"
        self.assertEqual(read_recording(sample, site, TICKS, 2)[1][0], 30 * TICKS)
        self.assertEqual(read_recording(sample, site, TICKS)[0][1]["west"].mode, "synthetic_recorded")

    def test_archived_topis_links_and_automatic_grid(self):
        for site_id in ("seolleung", "taereung"):
            site = load_site(site_id)
            path = ROOT / site.archive["path"]
            timeline = read_recording(path, site, TICKS, site.archive["compression"])
            self.assertEqual(len(timeline), 13)
            self.assertEqual(timeline[1][0], 16 * TICKS)
            first, second = timeline[0][1], timeline[1][1]
            self.assertTrue(all(first[lane.id].link_id == lane.link_id for lane in site.approaches))
            self.assertTrue(all(obs.mode == "recorded" for obs in first.values()))
            self.assertTrue(all(obs.fetched_at is None and obs.observed_at for obs in first.values()))
            self.assertNotEqual(first["west"].speed_kmh, second["west"].speed_kmh)

    def test_replay_speed_to_flow_to_automatic_decision(self):
        site = load_site("seolleung")
        sample = ROOT / "data/recordings/seolleung_example.jsonl"
        observed = Scenario(site, "shift", 2026, recording=sample)
        constant = Scenario(site, "shift", 2026, speed_override=44)
        self.assertEqual(observed.schedule, constant.schedule)
        model = load_model(site.model_dir / "final.json", site.id)
        output = []
        for scenario in (observed, constant):
            sim = Simulation(site, QPolicy(model["q_table"]), trace=True)
            snapshots = {}
            for tick, (arrivals, speeds) in enumerate(scenario.tick_inputs()):
                sim.step(arrivals, speeds)
                snapshots[tick] = (sim.lanes["north"].speed, sim.queues)
            output.append((snapshots, sim.decisions))
        self.assertLess(output[0][0][130 * TICKS][0], output[0][0][115 * TICKS][0])
        self.assertTrue(any(output[0][0][tick][1] != output[1][0][tick][1]
                            for tick in range(120 * TICKS, 180 * TICKS)))
        self.assertNotEqual([item[2] for item in output[0][1]], [item[2] for item in output[1][1]])


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pg.display.init()
        pg.font.init()

    @classmethod
    def tearDownClass(cls):
        pg.quit()

    def test_map_pixels_vehicle_signal_color_and_site_switch(self):
        args = parser().parse_args(["--mode", "offline"])
        demo = Demo(args, load_site("seolleung"))
        screen = pg.Surface(SIZE)
        dash = Dashboard(screen, demo)
        for _ in range(20 * TICKS):
            demo.step()
        dash.draw()
        view = dash.maps[("seolleung", 27)]
        lane = demo.fixed.lanes["north"]
        x, y, angle = lane.path.at(lane.approach.stop)
        px, py = view.point(x, y)
        from math import cos, sin
        dot = (round(px - sin(angle) * 12) + view.rect.x,
               round(py + cos(angle) * 12) + view.rect.y)
        self.assertEqual(screen.get_at(dot), pg.Color(SIGNALS["red"]))
        self.assertEqual(signal_color(demo.fixed, 0), SIGNALS["green"])
        self.assertIn(lane.condition(), {"원활", "서행", "정체"})
        occupied = next(lane for lane in demo.fixed.lanes.values()
                        if any(10 < car.position < lane.approach.stop for car in lane.cars))
        car = next(car for car in occupied.cars if 10 < car.position < occupied.approach.stop)
        car_x, car_y, _ = occupied.path.at(car.position - 2.6)
        cx, cy = view.point(car_x, car_y)
        self.assertNotEqual(screen.get_at((cx + view.rect.x, cy + view.rect.y)), view.base.get_at((cx, cy)))

        second = next(row for row in districts() if row.get("site") == "taereung")
        dash.click((1080, 48))
        self.assertEqual(demo.region, second)
        self.assertTrue(demo.overview)
        dash.draw()
        dash.click((1000, 398))
        self.assertFalse(demo.overview)
        for _ in range(12 * TICKS):
            demo.step()
        dash.draw()
        self.assertIn(("taereung", 27), dash.maps)
        self.assertEqual(demo.q.tick, demo.fixed.tick)
        self.assertEqual(demo.q.arrivals, demo.fixed.arrivals)
        self.assertNotEqual(demo.site.approaches[0].path, lane.approach.path)

        dash.click((1194, 49))
        dash.draw()
        self.assertEqual(demo.region["id"], "seocho")
        self.assertTrue(demo.overview)
        demo.close()


if __name__ == "__main__":
    unittest.main()
