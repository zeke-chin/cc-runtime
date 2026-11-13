import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import make_config

from cc_runtime import jobs


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.cfg = make_config(Path(self.temporary.name))

    def tearDown(self):
        self.temporary.cleanup()

    def fake(self, result=None, error=None):
        def run(cfg):
            if error:
                raise error
            return result
        return {"fake": jobs.Job("fake", "test job", run, interval=60)}

    def test_run_records_outcome_and_appends_log(self):
        with patch.dict(jobs.JOBS, self.fake((0, {"n": 1})), clear=True):
            record = jobs.run_job(self.cfg, "fake")
            jobs.run_job(self.cfg, "fake")
        self.assertEqual((record["exit"], record["summary"]), (0, {"n": 1}))
        self.assertEqual(jobs.last_run(self.cfg, "fake")["summary"], {"n": 1})
        lines = (self.cfg.log_dir / "fake.log").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["job"], "fake")

    def test_a_crashing_job_is_recorded_as_failed(self):
        with patch.dict(jobs.JOBS, self.fake(error=RuntimeError("boom")), clear=True):
            record = jobs.run_job(self.cfg, "fake")
        self.assertEqual(record["exit"], 1)
        self.assertIn("boom", record["summary"]["error"])

    def test_dedupe_job_waits_for_a_healthy_session(self):
        unhealthy = jobs.sync.SessionState(exists=True, status="[Paused]", connected=True)
        with patch.object(jobs.sync, "session_state", return_value=unhealthy):
            code, summary = jobs._dedupe(self.cfg)
        self.assertEqual(code, 2)
        self.assertIn("not healthy", summary["skipped"])

    def test_launchd_definition(self):
        with patch.object(jobs, "which", return_value="/opt/uv"):
            daily = jobs.plist(self.cfg, "dedupe")
            frequent = jobs.plist(self.cfg, "externalize")
        self.assertEqual(daily["Label"], "io.cc-runtime.dedupe")
        self.assertEqual(daily["StartCalendarInterval"], {"Hour": 4, "Minute": 0})
        self.assertEqual(frequent["StartInterval"], 300)
        self.assertEqual(daily["ProgramArguments"][:2], ["/opt/uv", "run"])
        self.assertIn("--frozen", daily["ProgramArguments"])
        self.assertEqual(daily["ProgramArguments"][-3:], ["job", "run", "dedupe"])
        self.assertIn("/usr/local/bin", daily["EnvironmentVariables"]["PATH"])
        self.assertEqual(jobs.JOBS["dedupe"].schedule_text(), "daily 04:00")
        self.assertEqual(jobs.JOBS["externalize"].schedule_text(), "every 5 min")


if __name__ == "__main__":
    unittest.main()
