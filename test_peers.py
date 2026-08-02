"""Guard tests for peer enrichment - the checks that run before any request.

Run:  python -m unittest -v test_peers
"""

import unittest

import peers


def plan(expected=172, todo=None):
    return {"slabs": [], "candidates": [], "members": [],
            "todo": todo if todo is not None else [], "expected": expected}


class TestWorstCase(unittest.TestCase):
    def test_worst_case_is_every_item_retrying_to_exhaustion(self):
        self.assertEqual(peers.worst_case_calls(172), 516)

    def test_worst_case_tracks_the_client_retry_setting(self):
        import ebay_api
        self.assertEqual(peers.worst_case_calls(1),
                         ebay_api.get_item.__defaults__[-1])


class TestPlanGuard(unittest.TestCase):
    """--plan may report anything; it never spends a call."""

    def test_plan_tolerates_an_unreadable_quota(self):
        peers.guard(None, plan(), None, executing=False)   # must not raise

    def test_plan_still_rejects_a_run_over_the_cap(self):
        with self.assertRaises(SystemExit) as e:
            peers.guard(None, plan(peers.PEER_CAP + 1), 99999, executing=False)
        self.assertIn("exceeds the cap", str(e.exception))


class TestExecutionGuard(unittest.TestCase):
    """--run must refuse anything it cannot prove is safe."""

    def test_unreadable_quota_aborts(self):
        with self.assertRaises(SystemExit) as e:
            peers.guard(None, plan(), None, executing=True)
        self.assertIn("UNREADABLE", str(e.exception))

    def test_quota_below_the_plan_aborts(self):
        with self.assertRaises(SystemExit) as e:
            peers.guard(None, plan(172), 100, executing=True)
        self.assertIn("< 172 expected", str(e.exception))

    def test_quota_covering_the_plan_but_not_the_retries_aborts(self):
        """172 calls fit in 300, but 172 failing three times each do not."""
        with self.assertRaises(SystemExit) as e:
            peers.guard(None, plan(172), 300, executing=True)
        self.assertIn("worst case", str(e.exception))

    def test_quota_just_short_of_the_reserve_aborts(self):
        need = peers.worst_case_calls(172) + peers.QUOTA_RESERVE
        with self.assertRaises(SystemExit):
            peers.guard(None, plan(172), need - 1, executing=True)

    def test_quota_covering_worst_case_plus_reserve_proceeds(self):
        need = peers.worst_case_calls(172) + peers.QUOTA_RESERVE
        peers.guard(None, plan(172), need, executing=True)   # must not raise

    def test_cap_is_high_enough_for_the_planned_172(self):
        self.assertGreaterEqual(peers.PEER_CAP, 172)


class TestScope(unittest.TestCase):
    def test_rebuild_scope_covers_settled_candidates_only(self):
        """Held candidates have no settled identity, so nothing to rebuild on."""
        import db
        conn = db.connect()
        scoped = set(peers.affected_groups(conn).values())
        cls = {r[0]: r[1] for r in conn.execute(
            "SELECT item_id, classification FROM tierb WHERE http_status = 200")}
        self.assertTrue(scoped)
        for iid in scoped:
            self.assertIn(cls[iid], (peers.REKEY, "verified"))
        for iid, c in cls.items():
            if c == "held_for_parallel_resolution":
                self.assertNotIn(iid, scoped)


class TestQuotaResourceSelection(unittest.TestCase):
    """getItem spends buy.browse; no other pool may be read in its place."""

    LIVE = [{"resource": "buy.browse", "limit": 5000, "remaining": 4321,
             "reset": "2026-08-01T07:00:00.000Z"},
            {"resource": "buy.browse.item.bulk", "limit": 5000,
             "remaining": 5000, "reset": "2026-08-01T07:00:00.000Z"}]

    def test_reads_buy_browse_not_the_bulk_pool(self):
        import ebay_api
        self.assertEqual(ebay_api.browse_quota(self.LIVE), 4321)

    def test_order_does_not_decide_the_answer(self):
        import ebay_api
        self.assertEqual(ebay_api.browse_quota(list(reversed(self.LIVE))), 4321)

    def test_absent_resource_returns_none_rather_than_guessing(self):
        import ebay_api
        self.assertIsNone(ebay_api.browse_quota(
            [{"resource": "buy.browse.item.bulk", "remaining": 5000}]))
        self.assertIsNone(ebay_api.browse_quota([]))
        self.assertIsNone(ebay_api.browse_quota(None))

    def test_a_none_quota_then_blocks_execution(self):
        """The two halves compose: unknown resource -> unreadable -> abort."""
        import ebay_api
        q = ebay_api.browse_quota([{"resource": "other", "remaining": 9999}])
        with self.assertRaises(SystemExit) as e:
            peers.guard(None, plan(172), q, executing=True)
        self.assertIn("UNREADABLE", str(e.exception))


if __name__ == "__main__":
    unittest.main()
