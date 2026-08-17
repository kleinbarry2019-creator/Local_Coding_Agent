import unittest

from autonomous_agent.runtime.paths import (
    STATE_ROOT,
    AUDIT_ROOT,
    SNAPSHOT_ROOT,
    CACHE_ROOT,
)


class RuntimeIsolationTests(unittest.TestCase):

    def test_state_path_isolated(self):
        self.assertEqual(STATE_ROOT.name, "state")

    def test_audit_path_isolated(self):
        self.assertEqual(AUDIT_ROOT.name, "audit")

    def test_snapshot_path_isolated(self):
        self.assertEqual(SNAPSHOT_ROOT.name, "snapshots")

    def test_cache_path_isolated(self):
        self.assertEqual(CACHE_ROOT.name, "cache")


if __name__ == "__main__":
    unittest.main()
