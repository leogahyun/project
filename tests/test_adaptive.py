import os
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from threading import Event
from unittest.mock import patch

from main import parser
from traffic.adaptive import AdaptiveTraining, SpeedChange, train_candidate
from traffic.config import load_site
from traffic.observations import Observation
from traffic.policy import load_model


def speeds(site, speed):
    return {lane.id: Observation(speed, "live", lane.link_id) for lane in site.approaches}


class AdaptiveTests(unittest.TestCase):
    def setUp(self):
        self.site = load_site("taereung")
        self.args = parser().parse_args(["--adaptive", "--mode", "live"])
        self.model = load_model(self.site.model_dir / "final.json", self.site.id)

    def test_confirmation_and_rearming(self):
        detector = SpeedChange(20)
        for speed in (30, 30, 30, 22, 30, 22):
            self.assertEqual(detector.observe(speeds(self.site, speed)), {})
        self.assertEqual(detector.observe(speeds(self.site, 22))["west"], -26.7)
        for speed in (22, 22, 22):
            self.assertEqual(detector.observe(speeds(self.site, speed)), {})
        self.assertEqual(detector.observe(speeds(self.site, 30)), {})
        self.assertEqual(detector.observe(speeds(self.site, 30))["west"], 36.4)

    def test_candidate_uses_distinct_arrival_seeds_and_keeps_original(self):
        table, metadata, report, _ = train_candidate(
            self.site, "shift", self.model, speeds(self.site, 22), {"west": -26.7},
            2, 101, Event())
        self.assertIsNot(table, self.model["q_table"])
        self.assertEqual(metadata["episodes"], 502)
        self.assertEqual(len(report["runs"]), 5)
        self.assertFalse(set(metadata["training_arrival_seeds"]) & {r["seed"] for r in report["runs"]})

    def test_confirmed_poll_runs_background_training(self):
        self.args.adaptive_episodes = 2
        with tempfile.TemporaryDirectory() as temp:
            training = AdaptiveTraining(self.site, "shift", self.args, temp)
            try:
                for speed in (30, 30, 30, 22, 22):
                    training.observe(speeds(self.site, speed))
                training.update(self.model)
                self.assertIsNotNone(training.future)
                training.future.result(timeout=10)
                training.update(self.model)
                self.assertTrue(list(Path(temp).glob("*.json")))
                self.assertIsNone(training.future)
            finally:
                training.close()

    def test_only_accepted_model_is_persisted(self):
        with tempfile.TemporaryDirectory() as temp:
            training = AdaptiveTraining(self.site, "shift", self.args, temp)
            try:
                for accepted in (False, True):
                    training.future = Future()
                    training.future.set_result((self.model["q_table"], self.model["metadata"], {
                        "summary": {"current": {"mean_wait_s": 10}, "candidate": {"mean_wait_s": 9}}
                    }, accepted))
                    self.assertEqual(training.update(self.model) is not None, accepted)
                    self.assertEqual((Path(temp) / "accepted.json").exists(), accepted)
            finally:
                training.close()

    def test_live_dashboard_continues_past_four_minutes(self):
        os.environ["SDL_VIDEODRIVER"] = "dummy"
        from traffic.ui import Demo
        with patch.dict(os.environ, {"SEOUL_OPEN_API_KEY": ""}):
            demo = Demo(self.args, self.site)
        try:
            for _ in range(demo.scenario.ticks + 4):
                demo.step()
            self.assertFalse(demo.finished)
            self.assertEqual(demo.fixed.arrivals, demo.q.arrivals)
            self.assertEqual(demo.fixed.tick, demo.scenario.ticks + 4)
        finally:
            demo.close()


if __name__ == "__main__":
    unittest.main()
