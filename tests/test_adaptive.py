import csv
import os
import tempfile
import unittest
from concurrent.futures import Future
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from unittest.mock import patch

from main import main, parser
from traffic.adaptive import AdaptiveTraining, train_candidate
from traffic.config import load_site
from traffic.observations import KST, Observation, PollResult, TrafficInfoError
from traffic.policy import load_model
from traffic.volume import Volume, VolumeChange, parse_volume, rates_for


def current_hour():
    return datetime.now(KST).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)


def speeds(site, speed):
    return {lane.id: Observation(speed, "live", lane.link_id, fetched_at=datetime.now(KST).isoformat())
            for lane in site.approaches}


def volume(hour, count):
    return Volume("D-10", hour.isoformat(), datetime.now(KST).isoformat(), count, 5)


class AdaptiveTests(unittest.TestCase):
    def setUp(self):
        self.site = load_site("taereung")
        self.args = parser().parse_args(["--adaptive", "--mode", "live"])
        self.model = load_model(self.site.model_dir / "final.json", self.site.id)
        self.hour = current_hour()
        self.observed = volume(self.hour, 1500)

    def test_launch_prompts_only_if_key_missing(self):
        with (patch("sys.argv", ["main.py", "--mode", "live", "--adaptive"]),
              patch.dict(os.environ, {"SEOUL_OPEN_API_KEY": ""}),
              patch("main.prompt_topis_key", side_effect=[None, " sample-key "]) as prompt,
              patch("traffic.ui.launch", return_value=0) as launch):
            self.assertEqual(main(), 1)
            launch.assert_not_called()
            self.assertEqual(main(), 0)
            self.assertEqual(os.environ["SEOUL_OPEN_API_KEY"], "sample-key")
            self.assertEqual(main(), 0)
            self.assertEqual(prompt.call_count, 2)

    def test_count_parser_requires_all_unique_lanes_and_matching_hour(self):
        rows = "".join(
            f"<row><spot_num>D-10</spot_num><ymd>{self.hour:%Y%m%d}</ymd><hh>{self.hour.hour}</hh>"
            f"<io_type>I</io_type><lane_num>{lane}</lane_num><vol>300</vol></row>"
            for lane in range(1, 6))
        response = "<VolInfo><RESULT><CODE>INFO-000</CODE></RESULT>" + rows + "</VolInfo>"
        result = parse_volume(response, "D-10", self.hour, 5, datetime.now(KST))
        self.assertEqual(result.count, 1500)
        self.assertEqual(rates_for(self.site, volume(self.hour, 2250))["east"], 450)
        with self.assertRaisesRegex(TrafficInfoError, "집계 없음"):
            parse_volume("<RESULT><CODE>INFO-200</CODE></RESULT>",
                         "D-10", self.hour, 5, datetime.now(KST))
        with self.assertRaisesRegex(TrafficInfoError, "차로 집계 4/5개"):
            parse_volume(response.replace(rows[rows.rfind("<row>"):], ""),
                         "D-10", self.hour, 5, datetime.now(KST))
        with self.assertRaises(TrafficInfoError):
            parse_volume(response.replace(">5</lane_num>", ">4</lane_num>"),
                         "D-10", self.hour, 5, datetime.now(KST))

    def test_trigger_compares_adjacent_distinct_hours(self):
        detector = VolumeChange(20)
        self.assertIsNone(detector.observe(volume(self.hour - timedelta(hours=1), 1000)))
        self.assertEqual(detector.observe(self.observed)["percent"], 50)
        self.assertIsNone(detector.observe(volume(self.hour, 2000)))
        self.assertIsNone(detector.observe(volume(self.hour + timedelta(hours=2), 1000)))

    def test_candidate_logs_green_times_for_same_inputs(self):
        table, metadata, report, _ = train_candidate(
            self.site, "shift", self.model, speeds(self.site, 22), self.observed,
            {"percent": 50, "previous": 1000, "current": 1500}, 2, 101, Event())
        self.assertIsNot(table, self.model["q_table"])
        self.assertEqual(metadata["episodes"], 502)
        self.assertEqual(len(report["runs"]), 5)
        self.assertFalse(set(metadata["training_arrival_seeds"]) & {r["seed"] for r in report["runs"]})
        fixed = report["runs"][0]["policies"]["fixed"]["green_durations"]
        self.assertTrue(fixed)
        self.assertTrue(all(phase["seconds"] == 30 for phase in fixed))

    def test_threshold_dispatches_background_training_and_writes_event(self):
        self.args.adaptive_episodes = 2
        with tempfile.TemporaryDirectory() as temp:
            training = AdaptiveTraining(self.site, "shift", self.args, temp)
            try:
                training.observe(speeds(self.site, 30), volume(self.hour - timedelta(hours=1), 1000))
                training.observe(speeds(self.site, 10), None)
                training.update(self.model)
                self.assertIsNone(training.future)
                training.observe({}, self.observed)
                training.update(self.model)
                self.assertIsNone(training.future)
                training.observe(speeds(self.site, 22), None)
                training.update(self.model)
                self.assertIsNotNone(training.future)
                training.future.result(timeout=10)
                training.update(self.model)
                self.assertTrue((Path(temp) / "events.csv").exists())
            finally:
                training.close()

    def test_csv_records_selected_and_candidate_against_fixed(self):
        with tempfile.TemporaryDirectory() as temp:
            training = AdaptiveTraining(self.site, "shift", self.args, temp)
            try:
                metadata = {**self.model["metadata"], "volume": asdict(self.observed),
                            "volume_change": {"percent": 50, "previous": 1000, "current": 1500}}
                for accepted in (False, True):
                    training.future = Future()
                    training.future.set_result((self.model["q_table"], metadata, {
                        "summary": {"fixed": {"mean_wait_s": 8}, "current": {"mean_wait_s": 10},
                                    "candidate": {"mean_wait_s": 7}}
                    }, accepted))
                    self.assertEqual(training.update(self.model) is not None, accepted)
                    self.assertEqual((Path(temp) / "accepted.json").exists(), accepted)
                with (Path(temp) / "events.csv").open(encoding="utf-8-sig") as file:
                    rows = list(csv.DictReader(file))
                self.assertEqual([float(row["적용_AI_빼기_고정_s"]) for row in rows], [2, -1])
            finally:
                training.close()

    def test_dashboard_waits_without_replay_then_runs_past_four_minutes(self):
        os.environ["SDL_VIDEODRIVER"] = "dummy"
        from traffic.ui import Demo, Dashboard, SIZE, pg
        with patch.dict(os.environ, {"SEOUL_OPEN_API_KEY": ""}):
            demo = Demo(self.args, self.site)
        try:
            demo.step()
            self.assertEqual(demo.tick, 0)
            self.assertEqual(demo.observations, {})
            pg.font.init()
            dashboard = Dashboard(pg.Surface(SIZE), demo)
            with patch.object(dashboard, "select") as select:
                dashboard.draw()
                select.assert_not_called()
            demo.overview = True
            dashboard.draw()
            demo.poller.results.put(PollResult(speeds(self.site, 22), frozenset(), volume=self.observed))
            demo.refresh_training()
            for _ in range(demo.scenario.ticks + 4):
                demo.step()
            self.assertFalse(demo.finished)
            self.assertEqual(demo.fixed.arrivals, demo.q.arrivals)
            self.assertEqual(demo.fixed.tick, demo.scenario.ticks + 4)
        finally:
            demo.close()


if __name__ == "__main__":
    unittest.main()
