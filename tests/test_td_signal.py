from queue import SimpleQueue
import unittest

from traffic.config import load_site
from traffic.engine import Simulation, Vehicle
from traffic.policy import FixedPolicy
from traffic.td_signal import SignalPoller, parse_snapshot

MAPPING = {"west": "wt", "east": "et", "north": "nt", "south": "st"}


class TDataTests(unittest.TestCase):
    def test_direction_colors_and_countdown_expiry(self):
        row = {"itstId": "12345", "trsmUtcTime": 1000000}
        for prefix in MAPPING.values():
            row[f"{prefix}StsgStatNm"] = "stop-And-Remain"
            row[f"{prefix}StsgRmdrCs"] = 35
        row["ntStsgStatNm"] = "protected-Movement-Allowed"
        row["wtStsgStatNm"] = "protected-clearance"
        snapshot = parse_snapshot([row], "12345", MAPPING, 1000)
        feed = SignalPoller.__new__(SignalPoller)
        feed.results = SimpleQueue()
        feed.results.put((snapshot, "수신"))
        self.assertEqual(feed.read(now=1001)["north"], ("green", 2.5))
        self.assertEqual(feed.read(now=1001)["west"], ("yellow", 2.5))
        self.assertEqual(feed.read(now=1003.5), {})
        with self.assertRaisesRegex(ValueError, "지정 교차로"):
            parse_snapshot([row], "54321", MAPPING, 1000)
        row["ntStsgStatNm"] = None
        with self.assertRaisesRegex(ValueError, "NT 직진"):
            parse_snapshot([row], "12345", MAPPING, 1000)

    def test_external_signal_controls_simulated_stop_line(self):
        site = load_site("taereung")
        sim = Simulation(site, FixedPolicy(30))
        for lane in sim.lanes.values():
            lane.cars.append(Vehicle(1, position=lane.approach.stop))
        speeds = dict.fromkeys(sim.lanes, 30)
        aspects = dict.fromkeys(sim.lanes, "red")
        aspects["north"] = "green"
        sim.step((), speeds, external_aspects=aspects)
        self.assertEqual(sim.lanes["north"].crossed, 1)
        self.assertEqual(sim.lanes["south"].crossed, 0)
        self.assertEqual(sim.signal.elapsed, 0)
