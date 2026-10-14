"""Tests for the dependency-down pieces: the mapper's precedence rule, the executor's
"only start what is not running" guard, and the app-health verification.

Docker is stubbed, so no containers are needed.
Run from the repo root:  python -m unittest discover tests -v
"""
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))
sys.path.insert(0, str(ROOT))

from action_mapper import Action, map_action
from tools import executor
from verify import health_check as hc


class FakeContainer:
    def __init__(self, status):
        self.attrs = {"State": {"Status": status, "StartedAt": "t0"}}
        self.restarted = False

    def restart(self, timeout=None):
        self.restarted = True
        self.attrs["State"].update(Status="running", StartedAt="t1")

    def reload(self):
        pass


def fake_docker(containers):
    client = mock.Mock()
    client.containers.get.side_effect = lambda n: containers[n]
    return types.SimpleNamespace(from_env=lambda: client)


class MapperPrecedence(unittest.TestCase):
    def test_database_wording_maps_to_dependency(self):
        for t in ("Restart the database container", "Start the postgres container",
                  "Bring up the database"):
            self.assertEqual(map_action(t), Action.RESTART_DEPENDENCY, t)

    def test_generic_restart_still_generic(self):
        self.assertEqual(map_action("Restart the service"), Action.RESTART_CONTAINER)
        self.assertEqual(map_action("Restart the container"), Action.RESTART_CONTAINER)

    def test_other_conflicts_still_unknown(self):
        self.assertEqual(map_action("Restart the database and increase the memory limit"),
                         Action.UNKNOWN_NEEDS_HUMAN)


class RestartDependencyGuard(unittest.TestCase):
    def run_exec(self, containers, dry_run):
        with mock.patch.dict(sys.modules, {"docker": fake_docker(containers)}):
            return executor.restart_dependency(list(containers), dry_run)

    def test_running_container_is_never_touched(self):
        c = FakeContainer("running")
        status, _, ev = self.run_exec({"db": c}, dry_run=False)
        self.assertEqual(status, "no_target")
        self.assertFalse(c.restarted)

    def test_exited_container_is_started_live(self):
        c = FakeContainer("exited")
        status, _, ev = self.run_exec({"db": c}, dry_run=False)
        self.assertEqual(status, "executed")
        self.assertTrue(c.restarted)
        self.assertEqual(ev["started"]["db"]["state_after"], "running")

    def test_dry_run_changes_nothing(self):
        c = FakeContainer("exited")
        status, _, _ = self.run_exec({"db": c}, dry_run=True)
        self.assertEqual(status, "would_execute")
        self.assertFalse(c.restarted)

    def test_only_the_stopped_one_is_started(self):
        up, down = FakeContainer("running"), FakeContainer("exited")
        self.run_exec({"a": up, "b": down}, dry_run=False)
        self.assertFalse(up.restarted)
        self.assertTrue(down.restarted)

    def test_policy_allows_it_only_with_confidence(self):
        pol = executor.load_policy()
        self.assertTrue(executor.check_policy("RESTART_DEPENDENCY", 0.9, pol)[0])
        self.assertFalse(executor.check_policy("RESTART_DEPENDENCY", 0.5, pol)[0])


class AppVerification(unittest.TestCase):
    def check(self, status, body):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.status, resp.read.return_value = status, body.encode()
        with mock.patch.object(hc.urllib.request, "urlopen", return_value=resp):
            return hc._check_app(5050)

    def test_200_with_db_ok_passes(self):
        self.assertTrue(self.check(200, '{"db_ok": true, "status": "healthy"}')[0])

    def test_200_without_db_ok_fails(self):
        self.assertFalse(self.check(200, '{"status": "healthy"}')[0])

    def test_garbage_body_fails(self):
        self.assertFalse(self.check(200, "<html>")[0])

    def test_no_port_fails_closed(self):
        self.assertFalse(hc._check_app(None)[0])

    def test_unreachable_app_fails(self):
        with mock.patch.object(hc.urllib.request, "urlopen",
                               side_effect=hc.urllib.error.URLError("refused")):
            self.assertFalse(hc._check_app(5050)[0])


if __name__ == "__main__":
    unittest.main()
