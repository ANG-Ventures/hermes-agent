"""P3: identity browser placement requires both remote lease transport legs."""
import unittest
from hermes_cli import kanban_host_capabilities as hc


class BrowserRemotePlacementTest(unittest.TestCase):
    def context(self, record):
        import json
        doc = {"schema": 1, "capabilities": {"browser-session": {}, "screen": {}},
               "hosts": {"studio": {"capabilities": ["browser-session"]},
                         "ace-ai": {"capabilities": [], "browser_remote": record}}}
        skills = {}
        index = {"schema": 1, "skills": skills, "generated_by": hc.INDEX_GENERATOR,
                 "source_hash": hc.index_hash(skills)}
        cmap = hc.map_from_bytes(json.dumps(doc).encode(), json.dumps(index).encode())
        return hc.CapabilityTick(cmap, hc.ENFORCE, hc.DEFAULT_ENFORCE, ("ace-ai",))

    def test_both_legs_release_identity_pin(self):
        ctx = self.context({"front_door": True, "ssh": True})
        v = hc.evaluate("requires: browser-session", (), ctx)
        self.assertIsNone(v.hard_rule)
        self.assertEqual(v.only, ("ace-ai",))
        self.assertIsNone(hc.evaluate("requires: browser-session", (), ctx,
                                    pin_host="ace-ai").hard_rule)

    def test_each_failed_leg_refuses_pinned_remote(self):
        for front, ssh, leg in [(False, True, "front_door"), (True, False, "ssh"),
                                (False, False, "front_door")]:
            with self.subTest(front=front, ssh=ssh):
                ctx = self.context({"front_door": front, "ssh": ssh})
                v = hc.evaluate("requires: browser-session", (), ctx, pin_host="ace-ai")
                self.assertEqual(v.hard_rule, "requires:browser-session")
                self.assertEqual(v.wait, "BROWSER_REMOTE_UNREACHABLE:" + leg)
                self.assertEqual(hc.evaluate("requires: browser-session", (), ctx).only, ())

    def test_absent_and_malformed_records_never_release_pin(self):
        for record in [None, {}, True, {"front_door": "true", "ssh": True},
                       {"front_door": True, "ssh": 1}]:
            with self.subTest(record=record):
                self.assertEqual(hc.evaluate("requires: browser-session", (),
                                            self.context(record)).hard_rule,
                                 "requires:browser-session")

    def test_mixed_pool_excludes_failed_host(self):
        from dataclasses import replace
        ctx = self.context({"front_door": True, "ssh": True})
        cmap = replace(ctx.cmap, browser_remote={
            "ace-ai": {"front_door": True, "ssh": True},
            "ci-box": {"front_door": True, "ssh": False}})
        ctx = replace(ctx, cmap=cmap, pool_hosts=("ace-ai", "ci-box"))
        self.assertEqual(hc.evaluate("requires: browser-session", (), ctx).only, ("ace-ai",))

    def test_failed_record_vetoes_static_browser_advertisement(self):
        from dataclasses import replace
        ctx = self.context({"front_door": False, "ssh": True})
        ctx = replace(ctx, cmap=replace(ctx.cmap, hosts={"ace-ai": frozenset({"browser-session"})}))
        self.assertEqual(hc.evaluate("requires: browser-session", (), ctx,
                                    pin_host="ace-ai").wait,
                         "BROWSER_REMOTE_UNREACHABLE:front_door")

    def test_remote_browser_does_not_supply_screen(self):
        ctx = self.context({"front_door": True, "ssh": True})
        self.assertEqual(hc.evaluate("requires: screen", (), ctx).hard_rule, "requires:screen")

    def test_unobserved_static_host_is_excluded_only_with_remote_observations(self):
        from dataclasses import replace
        ctx = self.context({"front_door": True, "ssh": True})
        ctx = replace(ctx, pool_hosts=("ace-ai", "ci-box"), cmap=replace(
            ctx.cmap, hosts={"ci-box": frozenset({"browser-session"})}))
        self.assertEqual(hc.evaluate("requires: browser-session", (), ctx).only, ("ace-ai",))
        self.assertEqual(hc.evaluate("requires: browser-session", (), ctx,
                                    pin_host="ci-box").wait,
                         "BROWSER_REMOTE_UNREACHABLE:front_door")
        legacy = replace(ctx, cmap=replace(ctx.cmap, browser_remote={}))
        self.assertIsNone(hc.evaluate("requires: browser-session", (), legacy,
                                     pin_host="ci-box").hard_rule)


def test_workspace_browser_pin_waits_on_its_own_failed_leg(tmp_path, monkeypatch):
    from dataclasses import replace
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd
    from tests.hermes_cli.test_kanban_host_capabilities import _plan

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(kbd, "_root_kanban_cfg", lambda: {})
    monkeypatch.setattr(kbd, "_native_command_profile", lambda *a: False)
    kb.init_db()
    ctx = BrowserRemotePlacementTest().context({"front_door": True, "ssh": False})
    ctx = replace(ctx, pool_hosts=("ace-ai", "ci-box"), cmap=replace(
        ctx.cmap, browser_remote={"ace-ai": {"front_door": True, "ssh": False},
                                  "ci-box": {"front_door": True, "ssh": True}}))
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="browser", assignee="alpha",
                             body="requires: browser-session", workspace_kind="scratch")
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        binds = {tid: ("workspace_of", "owner", "ace-ai")}
        assert kbd._route_row(conn, row, _plan(), caps=ctx, binds=binds,
                              linked_ids={}, lineage_mode="none") == (
                                  "wait", "BROWSER_REMOTE_UNREACHABLE:ssh")
        healthy = replace(ctx, cmap=replace(ctx.cmap, browser_remote={
            "ace-ai": {"front_door": True, "ssh": True}}))
        assert kbd._route_row(conn, row, _plan(), caps=healthy, binds=binds,
                              linked_ids={}, lineage_mode="none") == ("pin", "ace-ai")


if __name__ == "__main__":
    unittest.main()
