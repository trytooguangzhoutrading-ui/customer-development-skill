#!/usr/bin/env python3
"""Offline accounting tests. Every business and URL is synthetic."""
import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import leadkit as kit
import leadkit_research as research
import test_leadkit as fixtures


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "workspace"
        kit.init_workspace(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def packet(self, bid="round-1", lid="TEST-001", sid="SRC-001", **policy):
        packet = fixtures.LeadkitTests.packet(self, bid, lid, sid)
        packet["task"].update(collection_strategy="legacy", completion_policy="target_or_coverage")
        packet["task"].update(policy)
        self.save(packet)
        return packet

    def save(self, packet):
        kit.safe_write(self.root / "research" / packet["batch_id"] / "batch.json", packet)

    def channel(self, cid="synthetic_directory", status="readable"):
        return {"channel_id": cid, "name": "仅测试目录", "kind": "website", "readiness": {"status": status, "checked_at": kit.now(), "observed_url": "https://example.invalid/merchant", "detail": "仅离线测试观察；不是实际联网结果"}, "alternatives": []}

    def entry(self, packet, eid="page-1", cid="synthetic_directory"):
        research.register_channel(self.root, self.channel(cid))
        entry = {"entry_id": eid, "channel_id": cid, "kind": "page", "url": "https://example.invalid/" + eid, "products": ["智能眼镜"], "next_action": "读取该虚构页面"}
        research.plan_entry(self.root, packet["batch_id"], entry)
        return entry

    def attempt(self, packet, aid="attempt-1", eid="page-1", state="read", **overrides):
        stamp = kit.now()
        result = {"attempt_id": aid, "entry_id": eid, "status": state, "started_at": stamp, "ended_at": stamp, "detail": "仅离线测试执行观察", "source_ids": [packet["sources"][0]["source_id"]] if state == "read" else [], "discovered_lead_ids": [packet["leads"][0]["lead_id"]] if state == "read" and packet["leads"] else []}
        if state in {"blocked", "needs_auth", "failed"}:
            result["next_trigger"] = "找到可读的官方替代入口"
        result.update(overrides)
        return result

    def log(self, bid="round-1"):
        return kit.read_json(self.root / "research" / bid / "research-log.json")

    def deviation(self, did="deviation-1"):
        path = self.root / (did + ".json")
        path.write_text('{"synthetic_tool_result":"' + did + '"}')
        stamp = kit.now()
        return {"deviation_id": did, "kind": "unplanned_network", "started_at": stamp, "ended_at": stamp,
                "detail": "Synthetic call occurred after a rejected plan; never a compliant registered attempt",
                "evidence": {"path": path.name, "sha256": kit.digest_bytes(path.read_bytes())}}

    def test_deviation_consumes_budget_without_discovery_or_master_mutation(self):
        packet = self.packet(work_budget={"max_attempts": 2, "max_minutes": 45})
        self.entry(packet)
        master = (self.root / "execution/master.json").read_bytes()
        deviation = self.deviation()
        research.record_deviation(self.root, packet["batch_id"], deviation)
        self.assertTrue(research.record_deviation(self.root, packet["batch_id"], deviation)["already_recorded"])
        self.assertEqual(len(self.log()["attempts"]), 0)
        self.assertEqual(research.status(self.root, packet["batch_id"])["research_effort"]["overall"]["unique_discovered"], 0)
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet))
        report = research.status(self.root, packet["batch_id"])
        self.assertEqual((report["attempts"], report["ledger_attempts"], report["unplanned_attempts"]), (2, 1, 1))
        self.assertIs(report["protocol_conformant"], False)
        self.assertEqual(report["stop_reason"], "attempt_budget")
        with self.assertRaisesRegex(kit.LeadkitError, "budget exhausted"):
            self.entry(packet, eid="later")
        with self.assertRaisesRegex(kit.LeadkitError, "budget exhausted"):
            research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, aid="attempt-2", retry_condition="new actual condition"))
        research.finish(self.root, packet["batch_id"], "attempt_budget", "One unplanned actual call is explicitly disclosed")
        self.assertEqual(master, (self.root / "execution/master.json").read_bytes())

    def test_deviation_evidence_and_times_are_guarded_and_immutable(self):
        packet = self.packet()
        self.entry(packet)
        deviation = self.deviation()
        for changed in [dict(deviation, ended_at="2099-01-01T00:00:00+00:00"),
                        dict(deviation, started_at="2000-01-01T00:00:00+00:00"),
                        dict(deviation, discovered_lead_ids=["TEST-001"]),
                        dict(deviation, evidence={"path": "../outside.json", "sha256": "bad"}),
                        dict(deviation, evidence={"path": deviation["evidence"]["path"], "sha256": "bad"})]:
            with self.assertRaises(kit.LeadkitError):
                research.record_deviation(self.root, packet["batch_id"], changed)
        link = self.root / "link.json"
        link.symlink_to(self.root / deviation["evidence"]["path"])
        with self.assertRaisesRegex(kit.LeadkitError, "symlinks"):
            research.record_deviation(self.root, packet["batch_id"], dict(deviation, evidence=dict(deviation["evidence"], path="link.json")))
        research.record_deviation(self.root, packet["batch_id"], deviation)
        with self.assertRaisesRegex(kit.LeadkitError, "immutable"):
            research.record_deviation(self.root, packet["batch_id"], dict(deviation, detail="rewritten"))
        with self.assertRaisesRegex(kit.LeadkitError, "already recorded"):
            research.record_deviation(self.root, packet["batch_id"], dict(deviation, deviation_id="duplicate"))

    def test_distinct_deviations_with_identical_response_bodies_both_consume_budget(self):
        packet = self.packet(work_budget={"max_attempts": 2, "max_minutes": 45})
        self.entry(packet)
        first = self.deviation()
        research.record_deviation(self.root, packet["batch_id"], first)
        path = self.root / "second-call.json"
        path.write_bytes((self.root / first["evidence"]["path"]).read_bytes())
        stamp = kit.now()
        second = dict(first, deviation_id="different-call", started_at=stamp, ended_at=stamp,
                      evidence=dict(first["evidence"], path=path.name))
        research.record_deviation(self.root, packet["batch_id"], second)
        report = research.status(self.root, packet["batch_id"])
        self.assertEqual((report["attempts"], report["unplanned_attempts"], report["ledger_attempts"]), (2, 2, 0))
        self.assertEqual(report["stop_reason"], "attempt_budget")
        with self.assertRaisesRegex(kit.LeadkitError, "already recorded"):
            research.record_deviation(self.root, packet["batch_id"], dict(second, deviation_id="renamed-replay"))

    def test_deviation_cannot_be_replayed_as_a_later_registered_attempt(self):
        packet = self.systematic(work_budget={"max_attempts": 2, "max_minutes": 45})
        research.collection_plan(self.root, packet["batch_id"], self.plan(packet))
        deviation = self.deviation()
        research.record_deviation(self.root, packet["batch_id"], deviation)
        self.route_entry(packet, "large", registered_at=deviation["started_at"])
        registered = self.log()["entries"]["entry-large"]["registered_at"]
        self.assertGreater(research._time(registered), research._time(deviation["ended_at"]))
        replay = self.attempt(packet, eid="entry-large", started_at=deviation["started_at"], ended_at=deviation["ended_at"])
        with self.assertRaisesRegex(kit.LeadkitError, "predates execution registration"):
            research.record_attempt(self.root, packet["batch_id"], replay)
        self.assertEqual(self.log()["attempts"], {})
        self.assertEqual(research.status(self.root, packet["batch_id"])["attempts"], 1)
        # A genuinely later, separately registered call can still be recorded.
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, aid="later-call", eid="entry-large"))
        self.assertEqual(research.status(self.root, packet["batch_id"])["attempts"], 2)

    def test_deferred_activation_refreshes_registration_and_rejects_earlier_call(self):
        packet = self.packet()
        research.register_channel(self.root, self.channel())
        entry = {"entry_id": "queued", "channel_id": "synthetic_directory", "kind": "page", "url": "https://example.invalid/queued", "products": ["智能眼镜"], "next_action": "仅保存待读入口"}
        research.plan_entry(self.root, packet["batch_id"], entry, defer=True)
        queued_registration = self.log()["entries"]["queued"]["registered_at"]
        unplanned = self.attempt(packet, eid="queued")
        enabled = research.plan_entry(self.root, packet["batch_id"], dict(entry, registered_at=queued_registration))
        self.assertGreater(research._time(enabled["registered_at"]), research._time(unplanned["ended_at"]))
        with self.assertRaisesRegex(kit.LeadkitError, "predates execution registration"):
            research.record_attempt(self.root, packet["batch_id"], unplanned)
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="queued"))

    def test_legacy_registration_is_readable_but_new_attempt_requires_fresh_plan(self):
        packet = self.packet()
        entry = self.entry(packet)
        attempt = self.attempt(packet)
        research.record_attempt(self.root, packet["batch_id"], attempt)
        log = self.log()
        log["entries"][entry["entry_id"]].pop("registered_at")
        kit.safe_write(self.root / "research/round-1/research-log.json", log)
        before = (self.root / "research/round-1/research-log.json").read_bytes()
        self.assertEqual(research.status(self.root, packet["batch_id"])["attempts"], 1)
        self.assertTrue(research.record_attempt(self.root, packet["batch_id"], attempt)["already_recorded"])
        self.assertEqual(before, (self.root / "research/round-1/research-log.json").read_bytes())
        retry = self.attempt(packet, aid="retry", retry_condition="Synthetic new evidence is available")
        with self.assertRaisesRegex(kit.LeadkitError, "fresh plan-entry"):
            research.record_attempt(self.root, packet["batch_id"], retry)
        ready = research.plan_entry(self.root, packet["batch_id"], dict(entry, retry_condition=retry["retry_condition"], registered_at=log["started_at"]))
        self.assertIn("registered_at", ready)
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, aid="retry", retry_condition=retry["retry_condition"]))

    def test_deviation_cannot_supply_qualified_merchant_trace(self):
        packet = self.systematic(work_budget={"max_attempts": 1, "max_minutes": 45})
        research.collection_plan(self.root, packet["batch_id"], self.plan(packet))
        self.route_entry(packet, "large")
        research.record_deviation(self.root, packet["batch_id"], self.deviation())
        research.finish(self.root, packet["batch_id"], "attempt_budget", "Synthetic unplanned call only")
        with self.assertRaisesRegex(kit.LeadkitError, "source-linked"):
            fixtures.LeadkitTests.commit(self, packet)

    def test_deviation_raw_evidence_change_blocks_commit(self):
        packet = self.packet(work_budget={"max_attempts": 1, "max_minutes": 45})
        self.entry(packet)
        deviation = self.deviation()
        research.record_deviation(self.root, packet["batch_id"], deviation)
        research.finish(self.root, packet["batch_id"], "attempt_budget", "Synthetic audited stop")
        master = (self.root / "execution/master.json").read_bytes()
        (self.root / deviation["evidence"]["path"]).write_text("changed raw evidence")
        with self.assertRaisesRegex(kit.LeadkitError, "hash mismatch"):
            fixtures.LeadkitTests.commit(self, packet)
        self.assertEqual(master, (self.root / "execution/master.json").read_bytes())

    def test_deviation_history_carries_without_charging_new_round(self):
        packet = self.packet(work_budget={"max_attempts": 1, "max_minutes": 45})
        packet["leads"][0]["status"] = "candidate"
        self.save(packet)
        self.entry(packet)
        original_registration = self.log()["entries"]["page-1"]["registered_at"]
        research.record_deviation(self.root, packet["batch_id"], self.deviation())
        research.finish(self.root, packet["batch_id"], "attempt_budget", "Synthetic deviation is retained")
        fixtures.LeadkitTests.commit(self, packet)
        self.packet(bid="round-2", sid="SRC-002")
        research.carry_forward(self.root, "round-1", "round-2")
        self.assertEqual(research.status(self.root, "round-2")["attempts"], 0)
        self.assertEqual(self.log("round-2")["deviation_history"][0]["batch_id"], "round-1")
        self.assertEqual(self.log("round-2")["entries"]["page-1"]["registered_at"], original_registration)

    def test_policy_defaults_and_invalid_limits(self):
        policy = research.validate_policy({})
        self.assertEqual((policy["target_count"], policy["target_metric"], policy["work_budget"]), (20, "new_qualified", {"max_attempts": 80, "max_minutes": 45}))
        self.assertEqual(research.validate_policy({"mode": "enrich"})["target_metric"], "updated")
        for task in ({"mode": "sales"}, {"target_count": True}, {"target_count": 0}, {"target_metric": "contacts"}, {"work_budget": {"max_attempts": 0}}, {"work_budget": {"max_minutes": float("nan")}}, {"work_budget": {"max_minutes": True}}, {"work_budget": {"unlimited": True}}):
            with self.subTest(task=task), self.assertRaises(kit.LeadkitError):
                research.validate_policy(task)

    def test_readiness_is_observation_only_and_preserves_master(self):
        original = (self.root / "execution/master.json").read_bytes()
        channel = self.channel(status="needs_auth")
        self.assertFalse(research.register_channel(self.root, channel)["network_probe_performed_by_script"])
        self.assertTrue(research.register_channel(self.root, channel)["already_recorded"])
        changed = copy.deepcopy(channel)
        changed["readiness"]["status"] = "blocked"
        research.register_channel(self.root, changed)
        observations = {key: {"status": "untested", "checked_at": kit.now(), "detail": "没有执行探测"} for key in ("search", "web_read", "local_python")}
        research.record_environment(self.root, observations)
        registry = kit.read_json(self.root / "execution/channel-registry.json")
        self.assertEqual(len(registry["history"]), 1)
        self.assertEqual(registry["environment"], observations)
        self.assertEqual(original, (self.root / "execution/master.json").read_bytes())
        changed["kind"] = "social"
        with self.assertRaises(kit.LeadkitError):
            research.register_channel(self.root, changed)

    def test_status_does_not_start_or_reset_budget(self):
        packet = self.packet()
        self.assertFalse(research.status(self.root, packet["batch_id"])["recorded"])
        self.assertFalse((self.root / "research/round-1/research-log.json").exists())
        self.entry(packet)
        before = (self.root / "research/round-1/research-log.json").read_bytes()
        research.status(self.root, packet["batch_id"])
        research.status(self.root, packet["batch_id"])
        self.assertEqual(before, (self.root / "research/round-1/research-log.json").read_bytes())

    def test_entry_idempotency_scope_and_pending_channel(self):
        packet = self.packet()
        entry = self.entry(packet)
        self.assertTrue(research.plan_entry(self.root, packet["batch_id"], entry)["already_planned"])
        entry["next_action"] = "different work"
        with self.assertRaises(kit.LeadkitError):
            research.plan_entry(self.root, packet["batch_id"], entry)
        self.assertEqual(self.log()["entries"]["page-1"]["scope"], [{"country": "测试国"}])
        summary = research.status(self.root, packet["batch_id"])
        self.assertEqual(summary["channels"]["synthetic_directory"]["pending_entries"], ["page-1"])

    def test_attempt_idempotency_and_source_guard(self):
        packet = self.packet()
        self.entry(packet)
        attempt = self.attempt(packet)
        missing = dict(attempt, source_ids=[])
        with self.assertRaisesRegex(kit.LeadkitError, "actual recorded source"):
            research.record_attempt(self.root, packet["batch_id"], missing)
        missing["source_ids"] = ["NOT-RECORDED"]
        with self.assertRaisesRegex(kit.LeadkitError, "missing source"):
            research.record_attempt(self.root, packet["batch_id"], missing)
        research.record_attempt(self.root, packet["batch_id"], attempt)
        self.assertTrue(research.record_attempt(self.root, packet["batch_id"], attempt)["already_recorded"])
        attempt["detail"] = "changed"
        with self.assertRaisesRegex(kit.LeadkitError, "immutable"):
            research.record_attempt(self.root, packet["batch_id"], attempt)

    def test_failed_read_cannot_count_as_read(self):
        packet = self.packet()
        packet["sources"][0]["level"] = "failed_read"
        self.save(packet)
        self.entry(packet)
        with self.assertRaisesRegex(kit.LeadkitError, "Failed reading"):
            research.record_attempt(self.root, packet["batch_id"], self.attempt(packet))

    def link_entry(self, packet, **overrides):
        research.register_channel(self.root, self.channel())
        entry = {"entry_id": "link-80", "channel_id": "synthetic_directory", "kind": "link", "parent_url": packet["sources"][0]["url"], "parent_source_id": packet["sources"][0]["source_id"], "link_id": 80, "products": ["智能眼镜"], "next_action": "Click the actually observed link ID; target URL not exposed"}
        entry.update(overrides)
        return entry

    def test_link_uses_observed_parent_and_click_result_without_guessing_target(self):
        packet = self.packet()
        entry = self.link_entry(packet)
        research.plan_entry(self.root, packet["batch_id"], entry)
        self.assertNotIn("url", self.log()["entries"]["link-80"])
        self.assertEqual(self.log()["entries"]["link-80"]["link_id"], "80")
        self.assertTrue(research.plan_entry(self.root, packet["batch_id"], dict(entry, link_id="80"))["already_planned"])
        with self.assertRaisesRegex(kit.LeadkitError, "click result source"):
            research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="link-80"))
        destination = dict(packet["sources"][0], source_id="CLICK-80", url="https://example.invalid/observed-destination", excerpt="Synthetic actual click response, not a guessed URL")
        packet["sources"].append(destination)
        self.save(packet)
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="link-80", source_ids=["CLICK-80"]))
        self.assertEqual(research.status(self.root, packet["batch_id"])["attempts"], 1)
        self.assertEqual(self.log()["entries"]["link-80"]["parent_source_id"], "SRC-001")

    def test_link_rejects_bad_parent_source_and_invalid_tool_id(self):
        packet = self.packet()
        entry = self.link_entry(packet)
        for value in (None, "", " ", True, False, -1, 1.5, [], {}):
            with self.subTest(link_id=value), self.assertRaisesRegex(kit.LeadkitError, "link_id"):
                research.plan_entry(self.root, packet["batch_id"], dict(entry, link_id=value))
        for change in ({"parent_url": "file:///tmp/fake"}, {"parent_url": "https://example.invalid/other"}, {"parent_source_id": "MISSING"}):
            with self.subTest(change=change), self.assertRaises(kit.LeadkitError):
                research.plan_entry(self.root, packet["batch_id"], dict(entry, **change))
        for level in ("failed_read", "search_snippet"):
            packet["sources"][0]["level"] = level
            self.save(packet)
            with self.subTest(level=level), self.assertRaises(kit.LeadkitError):
                research.plan_entry(self.root, packet["batch_id"], entry)

    def test_link_identity_cannot_be_changed_with_new_context_labels(self):
        packet = self.packet()
        packet["sources"].append(dict(packet["sources"][0], source_id="PARENT-ALIAS"))
        self.save(packet)
        entry = self.link_entry(packet)
        research.plan_entry(self.root, packet["batch_id"], entry)
        research.register_channel(self.root, self.channel("other_directory"))
        disguised = dict(entry, entry_id="link-again", parent_source_id="PARENT-ALIAS", channel_id="other_directory", purpose="channel_research", cursor="next-page", link_id="80")
        with self.assertRaisesRegex(kit.LeadkitError, "Same entry"):
            research.plan_entry(self.root, packet["batch_id"], disguised)
        research.plan_entry(self.root, packet["batch_id"], dict(entry, entry_id="link-81", link_id=81))
        self.assertEqual(len(self.log()["entries"]), 2)

    def test_link_preserves_route_gate_parent_recheck_and_attempt_budget(self):
        packet = self.systematic(work_budget={"max_attempts": 1, "max_minutes": 45})
        entry = self.link_entry(packet, route_id="route-large")
        with self.assertRaisesRegex(kit.LeadkitError, "preflight"):
            research.plan_entry(self.root, packet["batch_id"], entry)
        research.collection_plan(self.root, packet["batch_id"], self.plan(packet))
        with self.assertRaisesRegex(kit.LeadkitError, "larger routes first"):
            research.plan_entry(self.root, packet["batch_id"], dict(entry, route_id="route-small"))
        research.plan_entry(self.root, packet["batch_id"], entry)
        packet["sources"][0]["level"] = "failed_read"
        self.save(packet)
        with self.assertRaisesRegex(kit.LeadkitError, "failed_read"):
            research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="link-80", state="blocked"))
        packet["sources"][0]["level"] = "current_original"
        self.save(packet)
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="link-80", state="blocked"))
        with self.assertRaisesRegex(kit.LeadkitError, "budget exhausted"):
            research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, aid="retry", eid="link-80", state="blocked", retry_condition="Observed access restored"))

    def test_processed_link_requires_changed_retry_even_with_new_parent_source(self):
        packet = self.packet()
        entry = self.link_entry(packet)
        research.plan_entry(self.root, packet["batch_id"], entry)
        packet["sources"].append(dict(packet["sources"][0], source_id="CLICK-80", url="https://example.invalid/observed-destination"))
        self.save(packet)
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="link-80", source_ids=["CLICK-80"]))
        research.finish(self.root, packet["batch_id"], "batch_complete", "Synthetic link checkpoint")
        fixtures.LeadkitTests.commit(self, packet)
        packet = self.packet("round-2", sid="SRC-002")
        research.carry_forward(self.root, "round-1", "round-2")
        entry = self.link_entry(packet, entry_id="link-new-name", link_id="80")
        with self.assertRaisesRegex(kit.LeadkitError, "retry_condition"):
            research.plan_entry(self.root, packet["batch_id"], entry)
        entry["retry_condition"] = "Observed parent link content changed"
        research.plan_entry(self.root, packet["batch_id"], entry)
        with self.assertRaisesRegex(kit.LeadkitError, "retry_condition"):
            research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="link-new-name", state="blocked"))
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="link-new-name", state="blocked", retry_condition=entry["retry_condition"]))
        self.assertEqual(research.status(self.root, packet["batch_id"])["attempts"], 1)

    def test_retry_needs_new_condition_and_failure_trigger(self):
        packet = self.packet()
        self.entry(packet)
        failed = self.attempt(packet, state="blocked")
        missing = copy.deepcopy(failed)
        missing.pop("next_trigger")
        with self.assertRaisesRegex(kit.LeadkitError, "next_trigger"):
            research.record_attempt(self.root, packet["batch_id"], missing)
        research.record_attempt(self.root, packet["batch_id"], failed)
        retry = self.attempt(packet, "attempt-2", state="blocked")
        with self.assertRaisesRegex(kit.LeadkitError, "retry_condition"):
            research.record_attempt(self.root, packet["batch_id"], retry)
        retry["retry_condition"] = "已观察到公开访问入口恢复"
        research.record_attempt(self.root, packet["batch_id"], retry)
        with self.assertRaisesRegex(kit.LeadkitError, "not changed"):
            research.record_attempt(self.root, packet["batch_id"], dict(retry, attempt_id="attempt-3"))

    def test_plan_checks_blocked_and_done_retry_before_claiming_executable(self):
        packet = self.packet()
        entry = self.entry(packet)
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, state="blocked"))
        with self.assertRaisesRegex(kit.LeadkitError, "retry_condition"):
            research.plan_entry(self.root, packet["batch_id"], entry)
        condition = "Observed public access restored"
        retry = dict(entry, retry_condition=condition)
        with self.assertRaisesRegex(kit.LeadkitError, "different work"):
            research.plan_entry(self.root, packet["batch_id"], dict(retry, url="https://example.invalid/changed-target"))
        ready = research.plan_entry(self.root, packet["batch_id"], retry)
        self.assertTrue(ready["executable"])
        self.assertEqual(len(self.log()["attempts"]), 1)
        with self.assertRaisesRegex(kit.LeadkitError, "planned changed retry condition"):
            research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, aid="retry", retry_condition="An unplanned different condition"))
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, aid="retry", retry_condition=condition))
        for repeated in (entry, retry):
            with self.subTest(repeated=repeated), self.assertRaisesRegex(kit.LeadkitError, "not changed"):
                research.plan_entry(self.root, packet["batch_id"], repeated)
        ready = research.plan_entry(self.root, packet["batch_id"], dict(entry, retry_condition="Observed a later product update"))
        self.assertTrue(ready["executable"])
        self.assertEqual(len(self.log()["attempts"]), 2)

    def test_attempt_budget_stops_new_attempts(self):
        packet = self.packet(target_count=20, work_budget={"max_attempts": 1, "max_minutes": 45})
        self.entry(packet)
        self.entry(packet, "page-2")
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet))
        self.assertEqual(research.status(self.root, packet["batch_id"])["stop_reason"], "attempt_budget")
        with self.assertRaisesRegex(kit.LeadkitError, "budget exhausted"):
            research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, "attempt-2", "page-2"))
        research.finish(self.root, packet["batch_id"], "attempt_budget", "本轮次数用完")

    def test_time_budget_is_fixed_and_not_reset_by_status(self):
        packet = self.packet()
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with patch.object(kit, "now", return_value=old):
            self.entry(packet)
        self.assertEqual(research.status(self.root, packet["batch_id"])["stop_reason"], "time_budget")
        with self.assertRaisesRegex(kit.LeadkitError, "after the time budget"):
            research.record_attempt(self.root, packet["batch_id"], self.attempt(packet))
        self.assertEqual(self.log()["started_at"], old)

    def test_plan_time_gate_includes_idempotent_entries_and_deferred_queue_resumes(self):
        packet = self.packet()
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with patch.object(kit, "now", return_value=old):
            first = self.entry(packet)
        queued = dict(first, entry_id="later-page", url="https://example.invalid/later-page")
        before = (self.root / "research/round-1/research-log.json").read_bytes()
        for entry in (first, queued):
            with self.subTest(entry_id=entry["entry_id"]), self.assertRaisesRegex(kit.LeadkitError, "Time budget exhausted before execution"):
                research.plan_entry(self.root, "round-1", entry)
        self.assertEqual(before, (self.root / "research/round-1/research-log.json").read_bytes())
        deferred = research.plan_entry(self.root, "round-1", queued, defer=True)
        self.assertFalse(deferred["executable"])
        self.assertTrue(deferred["deferred"])
        self.assertEqual((self.log()["entries"]["later-page"]["state"], len(self.log()["attempts"])), ("pending", 0))
        research.finish(self.root, "round-1", "time_budget", "Synthetic elapsed deadline; later page was only queued")
        fixtures.LeadkitTests.commit(self, packet)
        next_packet = self.packet("round-2", sid="SRC-002")
        research.carry_forward(self.root, "round-1", "round-2")
        with self.assertRaisesRegex(kit.LeadkitError, "Deferred entry is not executable"):
            research.record_attempt(self.root, "round-2", self.attempt(next_packet, eid="later-page", state="empty"))
        ready = research.plan_entry(self.root, "round-2", queued)
        self.assertTrue(ready["executable"])
        self.assertFalse(ready["deferred"])
        self.assertTrue(ready["already_planned"])
        self.assertEqual(research.status(self.root, "round-2")["attempts"], 0)
        self.assertNotIn("deferred", self.log("round-2")["entries"]["later-page"])
        research.record_attempt(self.root, "round-2", self.attempt(next_packet, eid="later-page", state="empty"))
        self.assertEqual(research.status(self.root, "round-2")["attempts"], 1)

    def test_plan_attempt_gate_allows_only_explicit_deferred_queue_at_limit(self):
        packet = self.packet(work_budget={"max_attempts": 1, "max_minutes": 45})
        first = self.entry(packet)
        pending = self.entry(packet, "page-2")
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, state="empty"))
        new = dict(first, entry_id="page-3", url="https://example.invalid/page-3")
        for entry in (first, pending, new):
            with self.subTest(entry_id=entry["entry_id"]), self.assertRaisesRegex(kit.LeadkitError, "Attempt budget exhausted before execution"):
                research.plan_entry(self.root, packet["batch_id"], entry)
        for entry in (pending, new):
            report = research.plan_entry(self.root, packet["batch_id"], entry, defer=True)
            self.assertFalse(report["executable"])
            self.assertEqual(report["state"], "pending")
        self.assertEqual(len(self.log()["attempts"]), 1)
        self.assertNotIn("page-3", self.log()["attempts"])

    def test_deferred_entry_needs_fresh_preflight_and_route_gate_before_execution(self):
        packet = self.systematic()
        entry = self.link_entry(packet, route_id="route-small")
        queued = research.plan_entry(self.root, packet["batch_id"], entry, defer=True)
        self.assertFalse(queued["executable"])
        with self.assertRaisesRegex(kit.LeadkitError, "preflight"):
            research.plan_entry(self.root, packet["batch_id"], entry)
        with self.assertRaisesRegex(kit.LeadkitError, "Deferred entry is not executable"):
            research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="link-80", state="blocked"))
        research.collection_plan(self.root, packet["batch_id"], self.plan(packet))
        with self.assertRaisesRegex(kit.LeadkitError, "larger routes first"):
            research.plan_entry(self.root, packet["batch_id"], entry)
        self.assertTrue(self.log()["entries"]["link-80"]["deferred"])
        self.assertEqual(len(self.log()["attempts"]), 0)

    def test_policy_cannot_change_after_work_starts(self):
        packet = self.packet()
        self.entry(packet)
        packet["task"]["target_count"] = 999
        self.save(packet)
        with self.assertRaisesRegex(kit.LeadkitError, "policy changed"):
            research.status(self.root, packet["batch_id"])

    def test_new_qualified_excludes_existing_and_counts_channels_nonadditively(self):
        fixtures.LeadkitTests.commit(self, self.packet())
        packet = self.packet("round-2", sid="SRC-002")
        fresh = copy.deepcopy(packet["leads"][0])
        fresh.update(lead_id="TEST-NEW", name="独立新主体")
        packet["leads"].append(fresh)
        candidate = copy.deepcopy(fresh)
        candidate.update(lead_id="TEST-CANDIDATE", status="candidate")
        packet["leads"].append(candidate)
        self.save(packet)
        self.entry(packet)
        self.entry(packet, "page-2", "other_directory")
        for aid, eid in (("a-1", "page-1"), ("a-2", "page-2")):
            research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, aid, eid, discovered_lead_ids=["TEST-001", "TEST-NEW", "TEST-CANDIDATE"]))
        summary = research.status(self.root, packet["batch_id"])
        self.assertEqual(summary["counts"]["new_qualified"], 1)
        self.assertEqual(summary["counts"]["updated"], 1)
        self.assertEqual(summary["counts"]["qualified"], 2)
        self.assertEqual(summary["counts"]["new_scope_qualified"], 0)
        self.assertEqual(sum(c["new_qualified"] for c in summary["channels"].values()), 2)
        self.assertTrue(summary["channel_counts_are_nonadditive"])

    def test_alias_and_old_scope_promotion_are_not_new_entities(self):
        first = self.packet()
        first["leads"][0]["status"] = "candidate"
        fixtures.LeadkitTests.commit(self, first)
        packet = self.packet("round-2", sid="SRC-002")
        alias = copy.deepcopy(packet["leads"][0])
        alias.update(lead_id="ALIAS-1", canonical_id="TEST-001", status="candidate")
        packet["leads"].append(alias)
        self.save(packet)
        self.entry(packet)
        summary = research.status(self.root, packet["batch_id"])
        self.assertEqual(summary["counts"]["new_qualified"], 0)
        self.assertEqual(summary["counts"]["new_scope_qualified"], 1)
        self.assertEqual(summary["counts"]["updated"], 1)

    def test_finish_truthful_and_revision_after_prepare(self):
        packet = self.packet(target_count=1)
        self.entry(packet)
        research.finish(self.root, packet["batch_id"], "target_reached", "初稿有1个主表")
        prepared = copy.deepcopy(packet)
        prepared["leads"] = []
        master, _ = kit.load_master(self.root)
        with self.assertRaisesRegex(kit.LeadkitError, "Prepared subset"):
            research.summary_for_commit(self.root, prepared, master)
        with self.assertRaisesRegex(kit.LeadkitError, "revision_detail"):
            research.finish(self.root, packet["batch_id"], "batch_complete", "有效子集不足", prepared)
        research.finish(self.root, packet["batch_id"], "batch_complete", "有效子集不足", prepared, "prepare隔离了唯一记录")
        summary = research.summary_for_commit(self.root, prepared, master)
        self.assertFalse(summary["target_reached"])
        self.assertEqual(summary["stop_reason"], "batch_complete")
        self.assertEqual(len(self.log()["finish_history"]), 1)
        self.assertEqual(self.log()["finish"]["finished_at"], self.log()["finish_history"][0]["previous"]["finished_at"])

    def test_carry_preserves_retry_history_and_fresh_budget(self):
        packet = self.packet()
        for eid in ("page-1", "page-2", "page-3"):
            self.entry(packet, eid)
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, state="empty"))
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, "attempt-2", "page-2", "blocked"))
        research.finish(self.root, packet["batch_id"], "batch_complete", "阶段记录")
        fixtures.LeadkitTests.commit(self, packet)
        next_packet = self.packet("round-2", sid="SRC-002")
        result = research.carry_forward(self.root, "round-1", "round-2")
        self.assertEqual(result["pending_or_blocked_entries"], 2)
        self.assertEqual(result["previously_read_entries"], 1)
        self.assertEqual(self.log("round-2")["attempts"], {})
        blocked = self.attempt(next_packet, "next-attempt", "page-2", "blocked")
        with self.assertRaisesRegex(kit.LeadkitError, "retry_condition"):
            research.record_attempt(self.root, "round-2", blocked)
        blocked["retry_condition"] = "已确认另一入口恢复"
        research.record_attempt(self.root, "round-2", blocked)
        prior_read = {"entry_id": "new-page-1", "channel_id": "synthetic_directory", "kind": "page", "url": "https://example.invalid/page-1", "products": ["智能眼镜"], "next_action": "复查"}
        with self.assertRaisesRegex(kit.LeadkitError, "retry_condition"):
            research.plan_entry(self.root, "round-2", prior_read)
        prior_read["retry_condition"] = "观察到新产品更新"
        research.plan_entry(self.root, "round-2", prior_read)
        research.record_attempt(self.root, "round-2", self.attempt(next_packet, "new-read", "new-page-1", "empty", retry_condition="观察到新产品更新"))

    def test_commit_records_summary_and_freezes_log(self):
        packet = self.packet(target_count=1)
        self.entry(packet)
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet))
        with self.assertRaisesRegex(kit.LeadkitError, "finish reason"):
            fixtures.LeadkitTests.commit(self, packet)
        research.finish(self.root, packet["batch_id"], "target_reached", "本次1个新增合格")
        receipt = fixtures.LeadkitTests.commit(self, packet)
        master, _ = kit.load_master(self.root)
        summary = master["batches"][packet["batch_id"]]["research_summary"]
        self.assertEqual(summary["counts"]["new_qualified"], 1)
        self.assertEqual(receipt["counts"]["new_qualified_entities"], 1)
        self.assertEqual(research.status(self.root, packet["batch_id"])["counts"], summary["counts"])
        with self.assertRaisesRegex(kit.LeadkitError, "frozen"):
            research.finish(self.root, packet["batch_id"], "batch_complete", "修改")

    def test_failure_without_queue_survives_three_continuations_without_fake_reads(self):
        packet = self.packet()
        entry = self.entry(packet)
        failure = self.attempt(packet, state="needs_auth", source_ids=["SRC-001"], detail="Synthetic actual authentication wall", next_trigger="New public mirror or granted access")
        research.record_attempt(self.root, "round-1", failure)
        expected = dict({key: failure[key] for key in ("attempt_id", "status", "source_ids", "detail", "next_trigger", "started_at", "ended_at")}, batch_id="round-1")
        self.assertEqual(self.log()["entries"]["page-1"]["last_failure"], expected)
        with self.assertRaisesRegex(kit.LeadkitError, "retry_condition"):
            research.plan_entry(self.root, "round-1", entry)
        self.assertTrue(research.plan_entry(self.root, "round-1", dict(entry, retry_condition="Synthetic new mirror observed"))["already_planned"])
        self.assertFalse(packet.get("queue"))
        for n in range(1, 4):
            previous, current = "round-%d" % n, "round-%d" % (n + 1)
            research.finish(self.root, previous, "batch_complete", "Synthetic blocked checkpoint")
            fixtures.LeadkitTests.commit(self, packet)
            packet = self.packet(current, sid="SRC-%03d" % (n + 1))
            research.carry_forward(self.root, previous, current)
            summary = research.status(self.root, current)
            self.assertEqual(summary["attempts"], 0)
            self.assertEqual(summary["blocked_conditions"], {"page-1": expected})
            self.assertEqual(summary["channels"]["synthetic_directory"]["read_entries"], [])
            self.assertEqual(self.log(current)["attempts"], {})
            self.assertEqual(self.log(current)["read_history"], [])
            self.assertEqual(self.log(current)["entries"]["page-1"]["last_failure"], expected)
            with self.assertRaisesRegex(kit.LeadkitError, "retry_condition"):
                research.record_attempt(self.root, current, self.attempt(packet, aid="retry"))
        research.record_attempt(self.root, current, self.attempt(packet, aid="retry", retry_condition="Observed new publicly readable mirror"))
        summary = research.status(self.root, current)
        self.assertEqual(summary["attempts"], 1)
        self.assertEqual(summary["blocked_conditions"], {})
        self.assertEqual(summary["channels"]["synthetic_directory"]["read_entries"], ["page-1"])

    def test_carry_recovers_old_failure_metadata_then_keeps_latest_failure_origin(self):
        packet = self.packet()
        self.entry(packet)
        research.record_attempt(self.root, "round-1", self.attempt(packet, state="failed"))
        log = self.log()
        del log["entries"]["page-1"]["last_failure"]
        kit.safe_write(self.root / "research/round-1/research-log.json", log)
        research.finish(self.root, "round-1", "batch_complete", "Synthetic old-version checkpoint")
        fixtures.LeadkitTests.commit(self, packet)
        packet = self.packet("round-2", sid="SRC-002")
        research.carry_forward(self.root, "round-1", "round-2")
        self.assertEqual(self.log("round-2")["entries"]["page-1"]["last_failure"]["batch_id"], "round-1")
        latest = self.attempt(packet, aid="retry-blocked", state="blocked", detail="Synthetic mirror is now rate limited", next_trigger="Mirror rate limit reset observed", retry_condition="Synthetic new mirror observed")
        research.record_attempt(self.root, "round-2", latest)
        research.finish(self.root, "round-2", "batch_complete", "Synthetic latest failure checkpoint")
        fixtures.LeadkitTests.commit(self, packet)
        self.packet("round-3", sid="SRC-003")
        research.carry_forward(self.root, "round-2", "round-3")
        carried = research.status(self.root, "round-3")["blocked_conditions"]["page-1"]
        self.assertEqual((carried["batch_id"], carried["attempt_id"], carried["next_trigger"]), ("round-2", "retry-blocked", latest["next_trigger"]))
        self.assertEqual(research.status(self.root, "round-3")["attempts"], 0)

    def test_legacy_no_log_hook_is_optional(self):
        packet = self.packet()
        master, _ = kit.load_master(self.root)
        self.assertIsNone(research.summary_for_commit(self.root, packet, master))

    def systematic(self, **policy):
        return self.packet(collection_strategy="region_size_ladder", completion_policy="coverage", **policy)

    def plan(self, packet, sizes=("large", "medium", "small")):
        research.register_channel(self.root, self.channel())
        sid = packet["sources"][0]["source_id"]
        channels = [{"category": c, "status": "applicable" if c == "offline_markets" else "not_applicable", "source_ids": [sid], "detail": "虚构测试地图的实际观察范围"} for c in sorted(research.MAP_CATEGORIES)]
        routes = [{"route_id": "route-" + size, "channel_id": "synthetic_directory", "category": "offline_markets", "size": size, "source_ids": [sid], "size_source_ids": [] if size == "unknown" else [sid], "order": n + 1, "entrypoint": "https://example.invalid/" + size, "detail": "虚构目录规模及边界", "next_action": "按目录边界枚举商户"} for n, size in enumerate(sizes)]
        return {"checked_at": kit.now(), "channels": channels, "contact_ecosystem": [{"kind": "phone", "source_ids": [sid], "detail": "虚构地区企业目录提供业务电话"}], "routes": routes}

    def route_entry(self, packet, size, **extra):
        entry = {"entry_id": "entry-" + size, "channel_id": "synthetic_directory", "kind": "page", "url": "https://example.invalid/" + size, "products": ["智能眼镜"], "next_action": "读取已发现目录", "route_id": "route-" + size}
        entry.update(extra)
        research.plan_entry(self.root, packet["batch_id"], entry)
        return entry

    def finish_route(self, packet, size, state="empty"):
        attempt = self.attempt(packet, "attempt-" + size, "entry-" + size, state)
        research.record_attempt(self.root, packet["batch_id"], attempt)
        update = {"state": "complete" if state == "read" else "empty" if state == "empty" else "blocked", "detail": "已读取虚构目录最终页，无更多入口" if state in {"read", "empty"} else "实际访问被拒", "cursor": "page=1,end" if state in {"read", "empty"} else "unavailable", "attempt_ids": [attempt["attempt_id"]], "directory_exhausted": state in {"read", "empty"}}
        if state not in {"read", "empty"}:
            update["next_trigger"] = "实际访问权限变化后重试"
        return research.route_progress(self.root, packet["batch_id"], "route-" + size, update)

    def test_systematic_preflight_gates_merchants_but_allows_reconnaissance(self):
        packet = self.systematic()
        research.register_channel(self.root, self.channel())
        with self.assertRaisesRegex(kit.LeadkitError, "preflight"):
            self.route_entry(packet, "large")
        self.route_entry(packet, "preflight", purpose="channel_research")
        bad_plan = self.plan(packet)
        bad_plan["contact_ecosystem"] = []
        self.assertFalse(research.collection_plan(self.root, packet["batch_id"], bad_plan)["preflight_ready"])
        with self.assertRaisesRegex(kit.LeadkitError, "preflight"):
            self.route_entry(packet, "large")
        plan = self.plan(packet)
        plan["contact_ecosystem"][0]["source_ids"] = []
        with self.assertRaises(kit.LeadkitError):
            research.collection_plan(self.root, packet["batch_id"], plan)
        plan = self.plan(packet)
        plan["contact_ecosystem"][0]["kind"] = "unknown"
        with self.assertRaisesRegex(kit.LeadkitError, "Unknown contact ecology"):
            research.collection_plan(self.root, packet["batch_id"], plan)
        research.collection_plan(self.root, packet["batch_id"], self.plan(packet))
        self.route_entry(packet, "large")

    def test_systematic_size_order_requires_real_large_route_attempt(self):
        packet = self.systematic()
        research.collection_plan(self.root, packet["batch_id"], self.plan(packet))
        with self.assertRaisesRegex(kit.LeadkitError, "larger routes first"):
            self.route_entry(packet, "small")
        with self.assertRaisesRegex(kit.LeadkitError, "actual attempt evidence"):
            research.route_progress(self.root, packet["batch_id"], "route-large", {"state": "blocked", "detail": "没有实际访问的假受阻", "cursor": "unavailable", "attempt_ids": [], "next_trigger": "new access"})
        self.route_entry(packet, "large")
        self.finish_route(packet, "large", "blocked")
        self.route_entry(packet, "medium")
        self.finish_route(packet, "medium")
        self.route_entry(packet, "small")
        self.assertEqual(research.status(self.root, packet["batch_id"])["scope_completion"], "partial")

    def test_systematic_stage_count_does_not_finish_city(self):
        packet = self.systematic(target_count=1)
        research.collection_plan(self.root, packet["batch_id"], self.plan(packet))
        summary = research.status(self.root, packet["batch_id"])
        self.assertTrue(summary["target_reached"])
        self.assertFalse(summary["should_stop"])
        self.assertEqual(summary["scope_completion"], "partial")
        with self.assertRaisesRegex(kit.LeadkitError, "stage count"):
            research.finish(self.root, packet["batch_id"], "target_reached", "数量到了")
        for reason in ("batch_complete", "paths_exhausted", "coverage_complete"):
            with self.subTest(reason=reason), self.assertRaisesRegex(kit.LeadkitError, "partial"):
                research.finish(self.root, packet["batch_id"], reason, "尚有未完成入口")

    def test_systematic_completed_discovery_boundary_can_finish(self):
        packet = self.systematic()
        research.collection_plan(self.root, packet["batch_id"], self.plan(packet))
        for size in ("large", "medium", "small"):
            self.route_entry(packet, size)
            self.finish_route(packet, size, "read" if size == "large" else "empty")
        summary = research.status(self.root, packet["batch_id"])
        self.assertTrue(summary["collection_complete"])
        self.assertEqual(summary["stop_reason"], "coverage_complete")
        research.finish(self.root, packet["batch_id"], "coverage_complete", "本次实际发现的目录边界已读完，不声称全市场穷尽")
        fixtures.LeadkitTests.commit(self, packet)

    def test_systematic_unresearched_category_never_counts_complete(self):
        packet = self.systematic()
        plan = self.plan(packet, ("large",))
        pending = next(c for c in plan["channels"] if c["category"] == "social")
        pending.update(status="needs_research", source_ids=[], detail="尚未查询")
        research.collection_plan(self.root, packet["batch_id"], plan)
        with self.assertRaisesRegex(kit.LeadkitError, "preflight"):
            self.route_entry(packet, "large")
        summary = research.status(self.root, packet["batch_id"])
        self.assertFalse(summary["preflight_ready"])
        self.assertFalse(summary["collection_complete"])
        self.assertIn("social", summary["unresolved_categories"])

    def test_eight_preflight_attempts_cannot_skip_city_map_but_can_save_zero_lead_partial(self):
        packet = self.systematic(work_budget={"max_attempts": 8, "max_minutes": 45})
        packet["leads"] = []
        packet["coverage"] = [{"product": "智能眼镜", "channel": "synthetic_directory", "status": "unsearched", "attempts": 0, "source_ids": [], "limitations": ["Only local channel reconnaissance; merchant enumeration has not begun"], "discovered_lead_ids": []}]
        self.save(packet)
        plan = self.plan(packet, ("unknown",))
        plan["routes"][0]["category"] = "wholesale_import_distributors"
        for channel in plan["channels"]:
            if channel["category"] == "wholesale_import_distributors":
                channel["status"] = "applicable"
            elif channel["category"] in {"offline_markets", "retail_malls_supermarkets", "trade_associations"}:
                channel.update(status="needs_research", source_ids=[], detail="Local channel not yet researched")
        for n in range(1, 9):
            eid = "recon-%d" % n
            self.route_entry(packet, eid, purpose="channel_research", category="wholesale_import_distributors")
            research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, aid="A%02d" % n, eid="entry-" + eid, discovered_lead_ids=[]))
            if n == 3:
                # Search -> one directory -> size unknown must not unlock merchants.
                report = research.collection_plan(self.root, packet["batch_id"], plan)
                self.assertFalse(report["preflight_ready"])
                self.assertEqual(report["preflight_pending_categories"], ["offline_markets", "retail_malls_supermarkets", "trade_associations"])
                with self.assertRaisesRegex(kit.LeadkitError, "preflight"):
                    self.route_entry(packet, "unknown")
        research.finish(self.root, packet["batch_id"], "attempt_budget", "Eight reconnaissance attempts; city map incomplete, no merchant collection")
        kit.commit(self.root, packet)
        committed = research.status(self.root, packet["batch_id"])
        self.assertEqual((committed["attempts"], committed["counts"]["qualified"], committed["scope_completion"]), (8, 0, "partial"))
        master, _ = kit.load_master(self.root)
        self.assertEqual(master["leads"], {})
        self.packet("round-2", sid="SRC-002", collection_strategy="region_size_ladder", completion_policy="coverage")
        research.carry_forward(self.root, "round-1", "round-2")
        continued = research.status(self.root, "round-2")
        self.assertEqual(continued["attempts"], 0)
        self.assertFalse(continued["preflight_ready"])
        self.assertEqual(len(self.log("round-2")["read_history"]), 8)

    def test_blocked_category_needs_own_failure_and_preserves_evidence_on_continue(self):
        packet = self.systematic(work_budget={"max_attempts": 2, "max_minutes": 45})
        packet["leads"] = []
        packet["coverage"] = [{"product": "智能眼镜", "channel": "synthetic_directory", "status": "unsearched", "attempts": 0, "source_ids": [], "limitations": ["Synthetic reconnaissance only"]}]
        self.save(packet)
        plan = self.plan(packet, ("large",))
        blocked = next(c for c in plan["channels"] if c["category"] == "social")
        blocked.update(status="blocked", source_ids=[], checked_at=kit.now(), next_trigger="Public social access restored")
        with self.assertRaisesRegex(kit.LeadkitError, "actual failed preflight attempt_ids"):
            research.collection_plan(self.root, packet["batch_id"], plan)
        for category in ("offline_markets", "social"):
            self.route_entry(packet, category, purpose="channel_research", category=category)
            research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, aid="failed-" + category, eid="entry-" + category, state="blocked"))
        blocked["attempt_ids"] = ["failed-offline_markets"]
        with self.assertRaisesRegex(kit.LeadkitError, "own actual failed preflight"):
            research.collection_plan(self.root, packet["batch_id"], plan)
        blocked["attempt_ids"] = ["failed-social"]
        ready = research.collection_plan(self.root, packet["batch_id"], plan)
        self.assertTrue(ready["preflight_ready"])
        self.assertFalse(ready["collection_complete"])
        self.assertIn("social", ready["unresolved_categories"])
        research.finish(self.root, packet["batch_id"], "attempt_budget", "Synthetic preflight checkpoint")
        kit.commit(self.root, packet)
        self.packet("round-2", sid="SRC-002", collection_strategy="region_size_ladder", completion_policy="coverage")
        research.carry_forward(self.root, "round-1", "round-2")
        ready = research.collection_plan(self.root, "round-2", plan)
        self.assertTrue(ready["preflight_ready"])
        carried = next(c for c in self.log("round-2")["collection_plan"]["channels"] if c["category"] == "social")
        self.assertEqual(carried["blocked_attempts"][0]["batch_id"], "round-1")
        self.assertEqual(research.status(self.root, "round-2")["attempts"], 0)

    def test_systematic_unknown_size_needs_no_fake_size_source(self):
        packet = self.systematic()
        plan = self.plan(packet, ("unknown",))
        research.collection_plan(self.root, packet["batch_id"], plan)
        self.assertEqual(self.log()["collection_plan"]["routes"]["route-unknown"]["size"], "unknown")
        plan["routes"][0]["size"] = "small"
        with self.assertRaises(kit.LeadkitError):
            research.collection_plan(self.root, packet["batch_id"], plan)

    def test_systematic_budget_delivers_partial_and_carries_plan(self):
        packet = self.systematic(work_budget={"max_attempts": 1, "max_minutes": 45})
        packet["leads"][0]["status"] = "candidate"
        self.save(packet)
        research.collection_plan(self.root, packet["batch_id"], self.plan(packet))
        self.route_entry(packet, "large")
        self.finish_route(packet, "large")
        research.finish(self.root, packet["batch_id"], "attempt_budget", "达到本轮检查点，余下中小路线未完成")
        fixtures.LeadkitTests.commit(self, packet)
        next_packet = self.packet("round-2", sid="SRC-002", collection_strategy="region_size_ladder", completion_policy="coverage")
        research.carry_forward(self.root, "round-1", "round-2")
        summary = research.status(self.root, "round-2")
        self.assertEqual(summary["next_route_id"], "route-medium")
        self.assertEqual(summary["attempts"], 0)
        self.assertEqual(summary["scope_completion"], "partial")
        self.assertEqual(self.log("round-2")["collection_plan"]["routes"]["route-large"]["state"], "empty")
        self.route_entry(next_packet, "medium")

    def test_systematic_commit_cannot_bypass_new_required_log(self):
        packet = self.systematic()
        with self.assertRaisesRegex(kit.LeadkitError, "research log"):
            fixtures.LeadkitTests.commit(self, packet)

    def test_systematic_main_requires_merchant_attempt_and_matching_source(self):
        for n, case in enumerate(("background_only", "unrelated_source", "snippet_only", "no_merchant_id", "matched")):
            with self.subTest(case=case):
                packet = self.systematic(bid="trace-%d" % n, work_budget={"max_attempts": 1, "max_minutes": 45})
                research.collection_plan(self.root, packet["batch_id"], self.plan(packet, ("large",)))
                self.route_entry(packet, "large", purpose="channel_research" if case == "background_only" else "merchant_collection")
                attempt = self.attempt(packet, eid="entry-large")
                if case == "unrelated_source":
                    attempt["source_ids"] = ["REG-SRC-001"]
                if case == "snippet_only":
                    snippet = dict(packet["sources"][0], source_id="SNIPPET-ONLY", level="search_snippet")
                    packet["sources"].append(snippet)
                    packet["leads"][0]["discovered_by"] = [{"channel": "synthetic_directory", "source_ids": ["SNIPPET-ONLY"]}]
                    self.save(packet)
                    attempt["source_ids"] = ["SNIPPET-ONLY"]
                if case == "no_merchant_id":
                    attempt["discovered_lead_ids"] = []
                research.record_attempt(self.root, packet["batch_id"], attempt)
                research.finish(self.root, packet["batch_id"], "attempt_budget", "Synthetic actual attempt budget reached")
                master, _ = kit.load_master(self.root)
                if case == "matched":
                    self.assertEqual(research.summary_for_commit(self.root, packet, master)["counts"]["new_qualified"], 1)
                else:
                    with self.assertRaisesRegex(kit.LeadkitError, "source-linked merchant_collection"):
                        research.summary_for_commit(self.root, packet, master)

    def test_snippet_candidate_can_qualify_via_subject_without_false_rediscovery(self):
        # Mirrors the real integration failure: initial search discovers a
        # candidate; a later strong read names subject_ids, not a new discovery.
        packet = self.systematic(work_budget={"max_attempts": 2, "max_minutes": 45})
        complete_lead = copy.deepcopy(packet["leads"][0])
        snippet = dict(packet["sources"][0], source_id="DISCOVERY-SNIPPET", level="search_snippet")
        packet["sources"].append(snippet)
        discovered_by = [{"channel": "synthetic_directory", "source_ids": [snippet["source_id"]]}]
        packet["leads"] = [{"lead_id": complete_lead["lead_id"], "name": complete_lead["name"], "country": complete_lead["country"],
                            "status": "candidate", "reason": "Synthetic search discovery, facts still unverified", "discovered_by": discovered_by}]
        self.save(packet)
        research.collection_plan(self.root, packet["batch_id"], self.plan(packet, ("large",)))
        self.route_entry(packet, "large", entry_id="initial-discovery", url="https://example.invalid/discovery", work_kind="discovery")
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, aid="discovery", eid="initial-discovery", source_ids=[snippet["source_id"]]))
        complete_lead["discovered_by"] = discovered_by
        packet["leads"] = [complete_lead]
        self.save(packet)
        self.route_entry(packet, "large", entry_id="qualify-subject", url="https://example.invalid/qualify", work_kind="qualification", subject_ids=[complete_lead["lead_id"]])
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, aid="qualification", eid="qualify-subject", discovered_lead_ids=[]))
        research.finish(self.root, packet["batch_id"], "attempt_budget", "Synthetic discovery and qualification are separate actual reads")
        fixtures.LeadkitTests.commit(self, packet)
        report = research.status(self.root, packet["batch_id"])
        self.assertTrue(report["committed"])
        self.assertEqual(report["counts"]["new_qualified"], 1)
        self.assertEqual(report["research_effort"]["overall"]["unique_discovered"], 1)
        self.assertEqual(report["research_effort"]["by_work_kind"]["qualification"]["unique_discovered"], 0)

    def test_existing_candidate_subject_read_promotes_without_counting_new_discovery(self):
        original = self.packet()
        snippet = dict(original["sources"][0], source_id="OLD-DISCOVERY", level="search_snippet")
        original["sources"].append(snippet)
        original["leads"] = [{"lead_id": "TEST-001", "name": "虚构测试商户", "country": "测试国", "status": "candidate",
                              "reason": "Earlier search only", "manual_notes": "保留人工备注", "discovered_by": [{"channel": "synthetic_directory", "source_ids": ["OLD-DISCOVERY"]}]}]
        fixtures.LeadkitTests.commit(self, original)
        packet = self.systematic(bid="promote-existing", sid="SRC-PROMOTION", work_budget={"max_attempts": 1, "max_minutes": 45})
        packet["leads"][0]["discovered_by"] = copy.deepcopy(original["leads"][0]["discovered_by"])
        self.save(packet)
        research.collection_plan(self.root, packet["batch_id"], self.plan(packet, ("large",)))
        self.route_entry(packet, "large", work_kind="contact", subject_ids=["TEST-001"])
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="entry-large", discovered_lead_ids=[]))
        research.finish(self.root, packet["batch_id"], "attempt_budget", "Synthetic current original confirms the existing candidate")
        fixtures.LeadkitTests.commit(self, packet)
        report = research.status(self.root, packet["batch_id"])
        self.assertEqual((report["counts"]["new_qualified"], report["counts"]["new_scope_qualified"]), (0, 1))
        effort = report["research_effort"]
        self.assertEqual(effort["overall"]["unique_discovered"], 0)
        self.assertEqual(effort["by_subject"]["TEST-001"]["read"], 1)
        self.assertEqual(effort["saved_records"]["entities_in_batch_without_read_attribution"], [])
        self.assertEqual(effort["saved_records"]["entities_in_batch_without_discovery_attribution"], ["TEST-001"])
        self.assertEqual(kit.load_master(self.root)[0]["leads"]["TEST-001"]["manual_notes"], "保留人工备注")

    def test_subject_trace_still_requires_correct_merchant_and_its_strong_source(self):
        for case in ("wrong_subject", "unrelated_source", "snippet_only"):
            with self.subTest(case=case):
                packet = self.systematic(bid="subject-" + case.replace("_", "-"), work_budget={"max_attempts": 1, "max_minutes": 45})
                packet["leads"].append({"lead_id": "OTHER", "name": "Other synthetic candidate", "country": "测试国", "status": "candidate", "reason": "Different entity", "discovered_by": [{"channel": "synthetic_directory", "source_ids": ["SRC-001"]}]})
                snippet = dict(packet["sources"][0], source_id="SUBJECT-SNIPPET", level="search_snippet")
                packet["sources"].append(snippet)
                packet["leads"][0]["discovered_by"] = [{"channel": "synthetic_directory", "source_ids": [snippet["source_id"]]}]
                self.save(packet)
                research.collection_plan(self.root, packet["batch_id"], self.plan(packet, ("large",)))
                self.route_entry(packet, "large", work_kind="qualification", subject_ids=["OTHER" if case == "wrong_subject" else "TEST-001"])
                source = "REG-SRC-001" if case == "unrelated_source" else snippet["source_id"] if case == "snippet_only" else "SRC-001"
                research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="entry-large", source_ids=[source], discovered_lead_ids=[]))
                research.finish(self.root, packet["batch_id"], "attempt_budget", "Synthetic failed attribution test")
                with self.assertRaisesRegex(kit.LeadkitError, "source-linked merchant_collection"):
                    research.summary_for_commit(self.root, packet, kit.load_master(self.root)[0])

    def test_plan_cannot_delete_or_relabel_large_route_without_new_evidence(self):
        packet = self.systematic()
        plan = self.plan(packet)
        research.collection_plan(self.root, packet["batch_id"], plan)
        removed = copy.deepcopy(plan)
        removed["routes"].pop(0)
        with self.assertRaisesRegex(kit.LeadkitError, "delete observed routes"):
            research.collection_plan(self.root, packet["batch_id"], removed)
        changed = copy.deepcopy(plan)
        changed["routes"][0]["size"] = "small"
        with self.assertRaisesRegex(kit.LeadkitError, "revision_detail"):
            research.collection_plan(self.root, packet["batch_id"], changed)
        changed["revision_detail"] = "New official directory corrects the old scope"
        with self.assertRaisesRegex(kit.LeadkitError, "new evidence"):
            research.collection_plan(self.root, packet["batch_id"], changed)
        new_source = dict(packet["sources"][0], source_id="SIZE-CORRECTION", excerpt="Synthetic new directory boundary")
        packet["sources"].append(new_source)
        self.save(packet)
        changed["routes"][0]["size_source_ids"] = ["SIZE-CORRECTION"]
        research.collection_plan(self.root, packet["batch_id"], changed)
        self.assertEqual(self.log()["collection_plan_history"][0]["routes"]["route-large"]["size"], "large")

    def test_unknown_route_does_not_jump_known_large_route(self):
        packet = self.systematic()
        research.collection_plan(self.root, packet["batch_id"], self.plan(packet, ("large", "unknown")))
        self.assertEqual(research.status(self.root, packet["batch_id"])["next_route_id"], "route-large")
        with self.assertRaisesRegex(kit.LeadkitError, "larger routes first"):
            self.route_entry(packet, "unknown")
        self.route_entry(packet, "unknown", purpose="route_reconnaissance")

    def test_route_background_read_cannot_claim_merchant_coverage(self):
        packet = self.systematic()
        research.collection_plan(self.root, packet["batch_id"], self.plan(packet, ("large",)))
        self.route_entry(packet, "large", purpose="route_reconnaissance")
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="entry-large", state="empty"))
        update = {"state": "empty", "detail": "Synthetic observed boundary", "cursor": "page=1,end", "attempt_ids": ["attempt-1"]}
        with self.assertRaisesRegex(kit.LeadkitError, "directory_exhausted"):
            research.route_progress(self.root, packet["batch_id"], "route-large", update)
        update["directory_exhausted"] = True
        with self.assertRaisesRegex(kit.LeadkitError, "merchant discovery"):
            research.route_progress(self.root, packet["batch_id"], "route-large", update)

    def test_empty_attempt_and_route_do_not_hide_discovered_merchants(self):
        packet = self.systematic()
        research.collection_plan(self.root, packet["batch_id"], self.plan(packet, ("large",)))
        self.route_entry(packet, "large")
        bad = self.attempt(packet, eid="entry-large", state="empty", discovered_lead_ids=["TEST-001"])
        with self.assertRaisesRegex(kit.LeadkitError, "empty attempt"):
            research.record_attempt(self.root, packet["batch_id"], bad)
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="entry-large"))
        with self.assertRaisesRegex(kit.LeadkitError, "complete, not empty"):
            research.route_progress(self.root, packet["batch_id"], "route-large", {"state": "empty", "detail": "Synthetic directory end", "cursor": "page=1,end", "directory_exhausted": True, "attempt_ids": ["attempt-1"]})

    def test_one_failed_source_cannot_end_other_executable_routes(self):
        packet = self.systematic()
        research.collection_plan(self.root, packet["batch_id"], self.plan(packet))
        with self.assertRaisesRegex(kit.LeadkitError, "actual failed access"):
            research.finish(self.root, packet["batch_id"], "access_blocked", "No attempt yet")
        self.route_entry(packet, "large")
        self.finish_route(packet, "large", "blocked")
        with self.assertRaisesRegex(kit.LeadkitError, "Executable routes"):
            research.finish(self.root, packet["batch_id"], "access_blocked", "One route failed")

    def test_preflight_only_failure_can_deliver_honest_partial(self):
        packet = self.systematic()
        packet["leads"] = []
        self.save(packet)
        research.register_channel(self.root, self.channel())
        self.route_entry(packet, "preflight", purpose="channel_research")
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="entry-preflight", state="blocked"))
        research.finish(self.root, packet["batch_id"], "access_blocked", "Only observed preflight access is blocked; no merchant coverage claimed")
        master, _ = kit.load_master(self.root)
        self.assertEqual(research.summary_for_commit(self.root, packet, master)["scope_completion"], "partial")

    def test_started_scope_and_continuation_scope_cannot_change_silently(self):
        packet = self.systematic()
        research.collection_plan(self.root, packet["batch_id"], self.plan(packet))
        changed = copy.deepcopy(packet)
        changed["task"]["geo_scope"] = [{"country": packet["task"]["countries"][0], "city": "OtherCity"}]
        self.save(changed)
        with self.assertRaisesRegex(kit.LeadkitError, "scope changed"):
            research.status(self.root, packet["batch_id"])
        self.save(packet)
        second = self.packet("other-scope", collection_strategy="region_size_ladder", completion_policy="coverage")
        second["task"]["geo_scope"] = [{"country": second["task"]["countries"][0], "city": "OtherCity"}]
        self.save(second)
        with self.assertRaisesRegex(kit.LeadkitError, "same task scope"):
            research.carry_forward(self.root, packet["batch_id"], second["batch_id"])

    def test_work_metadata_validates_subjects_without_reclassifying_legacy_entries(self):
        packet = self.packet()
        entry = self.entry(packet)
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet))
        before = (self.root / "research/round-1/research-log.json").read_bytes()
        summary = research.status(self.root, packet["batch_id"])
        self.assertEqual(summary["research_effort"]["by_work_kind"]["unclassified"]["attempts"], 1)
        self.assertNotIn("work_kind", self.log()["entries"]["page-1"])
        self.assertEqual(before, (self.root / "research/round-1/research-log.json").read_bytes())
        next_entry = dict(entry, entry_id="page-2", url="https://example.invalid/page-2")
        for changes in ({"work_kind": "enrichment"}, {"work_kind": []}, {"subject_ids": ["MISSING"]}, {"subject_ids": ["TEST-001", "TEST-001"]}, {"subject_ids": "TEST-001"}):
            with self.subTest(changes=changes), self.assertRaises(kit.LeadkitError):
                research.plan_entry(self.root, packet["batch_id"], dict(next_entry, **changes))
        research.plan_entry(self.root, packet["batch_id"], dict(next_entry, work_kind="qualification", subject_ids=["TEST-001"]))
        packet["leads"] = []
        self.save(packet)
        with self.assertRaisesRegex(kit.LeadkitError, "subject_ids references missing"):
            research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, aid="missing-subject", eid="page-2", discovered_lead_ids=[]))

    def test_effort_deduplicates_entities_and_separates_discovery_from_saved_records(self):
        fixtures.LeadkitTests.commit(self, self.packet())
        packet = self.systematic(bid="round-2", sid="SRC-002")
        fresh = dict(copy.deepcopy(packet["leads"][0]), lead_id="TEST-NEW", name="Synthetic new entity")
        alias = dict(copy.deepcopy(fresh), lead_id="TEST-ALIAS", canonical_id="TEST-NEW", status="candidate")
        untouched = dict(copy.deepcopy(fresh), lead_id="TEST-UNREAD", status="candidate")
        packet["leads"].extend([fresh, alias, untouched])
        self.save(packet)
        research.collection_plan(self.root, packet["batch_id"], self.plan(packet, ("large", "medium")))
        for eid, kind, subjects, state, found in (
            ("discover", "discovery", [], "read", ["TEST-001", "TEST-NEW", "TEST-ALIAS"]),
            ("qualify", "qualification", ["TEST-NEW", "TEST-ALIAS"], "read", ["TEST-NEW"]),
            ("contact", "contact", ["TEST-001"], "failed", []),
        ):
            self.route_entry(packet, "large", entry_id=eid, url="https://example.invalid/" + eid, work_kind=kind, subject_ids=subjects)
            research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, aid="a-" + eid, eid=eid, state=state, discovered_lead_ids=found))
        self.route_entry(packet, "preflight", purpose="channel_research")
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, aid="a-preflight", eid="entry-preflight", state="empty", discovered_lead_ids=[]))
        summary = research.status(self.root, packet["batch_id"])
        effort, review = summary["research_effort"], summary["allocation_review"]
        self.assertEqual((effort["overall"]["attempts"], effort["overall"]["read"], effort["overall"]["empty"], effort["overall"]["failed_access"]), (4, 2, 1, 1))
        self.assertEqual((effort["overall"]["unique_discovered"], effort["overall"]["new_discovered"], effort["overall"]["existing_discovered"], effort["overall"]["new_qualified_discovered"]), (2, 1, 1, 1))
        self.assertEqual(effort["saved_records"]["unique_entities_in_batch"], 3)
        self.assertEqual(effort["saved_records"]["entities_in_batch_without_read_attribution"], ["TEST-UNREAD"])
        self.assertEqual(effort["by_subject"]["TEST-NEW"]["attempts"], 1)
        self.assertNotIn("TEST-ALIAS", effort["by_subject"])
        self.assertEqual(effort["by_subject"]["TEST-001"]["failed_access"], 1)
        self.assertEqual(effort["by_subject"]["TEST-001"]["unique_discovered"], 0)
        self.assertEqual(review["largest_route"], {"route_id": "route-large", "attempts": 3, "share_of_all_attempts": 0.75})
        self.assertEqual(review["routes_without_merchant_attempts_this_round"], ["route-medium"])
        self.assertTrue(review["advisory_only"])
        self.assertEqual(self.log("round-2")["collection_plan"]["routes"]["route-large"]["state"], "active")
        prepared = copy.deepcopy(packet)
        prepared["leads"] = [l for l in prepared["leads"] if l["lead_id"] not in {"TEST-NEW", "TEST-ALIAS"}]
        narrowed = research.status(self.root, packet["batch_id"], prepared)["research_effort"]
        self.assertEqual(narrowed["overall"]["unique_discovered"], 1)
        self.assertEqual(narrowed["overall"]["new_qualified_discovered"], 0)
        self.assertEqual(narrowed["unresolved_record_ids"], ["TEST-ALIAS", "TEST-NEW"])
        self.assertNotIn("TEST-NEW", narrowed["by_subject"])

    def test_failed_access_ids_do_not_count_as_channel_or_effort_discovery(self):
        packet = self.packet()
        master_before = (self.root / "execution/master.json").read_bytes()
        # Older logs may associate a known merchant with a failed read. Keep
        # that audit record, but do not turn it into successful discovery.
        for state in ("failed", "blocked", "needs_auth"):
            self.entry(packet, eid="page-" + state)
            research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, aid="a-" + state, eid="page-" + state, state=state, discovered_lead_ids=["TEST-001"]))
        before = (self.root / "research/round-1/research-log.json").read_bytes()
        summary = research.status(self.root, packet["batch_id"])
        channel = summary["channels"]["synthetic_directory"]
        self.assertEqual(channel["attempts"], 3)
        self.assertEqual((channel["unique_discovered"], channel["qualified"], channel["new_qualified"]), (0, 0, 0))
        self.assertEqual(summary["research_effort"]["overall"]["unique_discovered"], 0)
        self.assertEqual(summary["research_effort"]["overall"]["failed_access"], 3)
        self.assertEqual(before, (self.root / "research/round-1/research-log.json").read_bytes())
        self.entry(packet, eid="page-readable")
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, aid="a-readable", eid="page-readable"))
        summary = research.status(self.root, packet["batch_id"])
        self.assertEqual(summary["channels"]["synthetic_directory"]["unique_discovered"], 1)
        self.assertEqual(summary["research_effort"]["overall"]["unique_discovered"], 1)
        self.assertEqual(master_before, (self.root / "execution/master.json").read_bytes())

    def defer_update(self, aid="attempt-1", alternative="route-medium"):
        return {"state": "deferred", "detail": "Synthetic directory remains unread beyond this page", "cursor": "observed-page=1", "attempt_ids": [aid], "next_trigger": "Review after expanding the alternative directory", "next_action": "Continue the observed next directory page", "alternative_route_id": alternative, "allocation_reason": "Current merchant has an unresolved ownership gap; the observed alternative has unread relevant merchants"}

    def test_deferral_requires_observed_work_and_executable_alternative(self):
        packet = self.systematic()
        research.collection_plan(self.root, packet["batch_id"], self.plan(packet))
        update = self.defer_update()
        with self.assertRaisesRegex(kit.LeadkitError, "own actual attempts"):
            research.route_progress(self.root, packet["batch_id"], "route-large", update)
        self.route_entry(packet, "large", work_kind="discovery")
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="entry-large"))
        for field in ("next_trigger", "next_action", "allocation_reason"):
            invalid = dict(update)
            del invalid[field]
            with self.subTest(field=field), self.assertRaises(kit.LeadkitError):
                research.route_progress(self.root, packet["batch_id"], "route-large", invalid)
        for alternative in ("route-large", "MISSING"):
            with self.subTest(alternative=alternative), self.assertRaisesRegex(kit.LeadkitError, "alternative_route_id"):
                research.route_progress(self.root, packet["batch_id"], "route-large", dict(update, alternative_route_id=alternative))
        with self.assertRaisesRegex(kit.LeadkitError, "remains behind"):
            research.route_progress(self.root, packet["batch_id"], "route-large", dict(update, alternative_route_id="route-small"))
        with self.assertRaisesRegex(kit.LeadkitError, "not exhausted"):
            research.route_progress(self.root, packet["batch_id"], "route-large", dict(update, directory_exhausted=True))
        report = research.route_progress(self.root, packet["batch_id"], "route-large", update)
        self.assertEqual(report["next_route_id"], "route-medium")
        self.assertEqual(report["scope_completion"], "partial")
        self.assertIn("route-large", report["unfinished_routes"])
        with self.assertRaisesRegex(kit.LeadkitError, "not executable"):
            self.route_entry(packet, "large", entry_id="large-new", url="https://example.invalid/large-new")
        self.route_entry(packet, "medium", work_kind="discovery")
        research.route_progress(self.root, packet["batch_id"], "route-large", {"state": "active", "detail": "Alternative pass reviewed; resume the saved next page", "cursor": "observed-page=1", "attempt_ids": []})
        saved = self.log()["collection_plan"]["routes"]["route-large"]
        self.assertEqual(saved["state"], "active")
        self.assertEqual(saved["progress_history"][0]["state"], "deferred")
        with self.assertRaisesRegex(kit.LeadkitError, "larger routes first"):
            research.plan_entry(self.root, packet["batch_id"], self.log()["entries"]["entry-medium"])

    def test_deferred_route_is_carried_without_fake_attempts_or_completion(self):
        packet = self.systematic(work_budget={"max_attempts": 1, "max_minutes": 45})
        packet["leads"][0]["status"] = "candidate"
        self.save(packet)
        research.collection_plan(self.root, packet["batch_id"], self.plan(packet, ("large", "medium")))
        self.route_entry(packet, "large", work_kind="qualification", subject_ids=["TEST-001"])
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="entry-large"))
        research.route_progress(self.root, packet["batch_id"], "route-large", self.defer_update())
        research.finish(self.root, packet["batch_id"], "attempt_budget", "Synthetic checkpoint with allocation decision preserved")
        fixtures.LeadkitTests.commit(self, packet)
        self.systematic(bid="round-2", sid="SRC-002")
        research.carry_forward(self.root, "round-1", "round-2")
        summary = research.status(self.root, "round-2")
        self.assertEqual(summary["attempts"], 0)
        self.assertEqual(summary["scope_completion"], "partial")
        self.assertEqual(summary["next_route_id"], "route-medium")
        self.assertEqual(summary["allocation_review"]["deferred_route_ids"], ["route-large"])
        carried = self.log("round-2")["collection_plan"]["routes"]["route-large"]
        self.assertEqual(carried["state"], "deferred")
        self.assertEqual(carried["progress"]["allocation_reason"], self.defer_update()["allocation_reason"])
        self.assertEqual(summary["research_effort"]["overall"]["unique_discovered"], 0)

    def test_deferred_readable_route_prevents_false_all_access_blocked_finish(self):
        packet = self.systematic()
        research.collection_plan(self.root, packet["batch_id"], self.plan(packet, ("large", "medium")))
        self.route_entry(packet, "large")
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="entry-large"))
        research.route_progress(self.root, packet["batch_id"], "route-large", self.defer_update())
        self.route_entry(packet, "medium")
        self.finish_route(packet, "medium", "blocked")
        with self.assertRaisesRegex(kit.LeadkitError, "deferred routes"):
            research.finish(self.root, packet["batch_id"], "access_blocked", "Only the alternative is blocked")
        self.assertIsNone(self.log()["finish"])

    def market_packet(self, **policy):
        packet = self.packet(collection_strategy="market_sequence", completion_policy="coverage", **policy)
        packet["leads"][0]["market_assignment"] = {"market_id": "market-a", "relation": "within", "detail": "Synthetic merchant directory places this merchant inside market A", "source_ids": [packet["sources"][0]["source_id"]]}
        self.save(packet)
        return packet

    def market_plan(self, packet):
        plan = self.plan(packet)
        sid = packet["sources"][0]["source_id"]
        plan["preflight_city_id"] = "city-a"
        plan["market_directory"] = {
            "countries": [{"country_id": "country-test", "name": "测试国", "order": 1}],
            "cities": [{"city_id": "city-a", "country_id": "country-test", "name": "测试城", "order": 1, "source_ids": [sid]},
                       {"city_id": "city-b", "country_id": "country-test", "name": "乙城", "order": 2, "source_ids": [sid]}],
            "markets": [{"market_id": mid, "city_id": city, "name": name, "order": order, "priority_basis": "Synthetic source-supported market priority", "source_ids": [sid], "size_source_ids": [sid], "boundary": "Observed synthetic directory and its actual links"}
                        for mid, city, name, order in (("market-a", "city-a", "甲批发城", 1), ("market-b", "city-a", "乙批发城", 2), ("market-c", "city-b", "丙批发城", 1))],
            "coverage": {"status": "partial", "detail": "Only these observed cities and markets; national completeness not established", "source_ids": []}}
        for route, mid in zip(plan["routes"], ("market-a", "market-b", "market-c")):
            route["market_id"] = mid
        return plan

    def complete_market(self, packet, size="large", mid="market-a"):
        self.route_entry(packet, size, work_kind="discovery")
        self.finish_route(packet, size, "read")
        return research.market_progress(self.root, packet["batch_id"], mid, {
            "state": "complete", "detail": "Synthetic observed directory boundary has been read", "boundary": "Observed page 1 and its disclosed merchant links only", "cursor": "page=1,end", "attempt_ids": ["attempt-" + size], "source_ids": [packet["sources"][0]["source_id"]], "directory_exhausted": True})

    def scope_review_update(self, packet, **changes):
        update = {"state": "review_required", "detail": "Synthetic observed entries are component sellers; target-product fit remains unproven and needs a scope decision",
                  "boundary": "Only the actually read synthetic directory page; not the whole market", "cursor": "page=1,remaining=unknown",
                  "attempt_ids": ["attempt-1"], "source_ids": [packet["sources"][0]["source_id"]], "directory_exhausted": False}
        update.update(changes)
        return update

    def test_unknown_market_product_fit_does_not_block_named_discovery(self):
        packet = self.market_packet()
        packet["leads"][0]["status"] = "candidate"
        self.save(packet)
        plan = self.market_plan(packet)
        plan["market_directory"]["markets"][0]["priority_basis"] = "Electronics market observed; wearable-product fit unknown, no density claim"
        research.collection_plan(self.root, packet["batch_id"], plan)
        self.route_entry(packet, "large", work_kind="discovery")
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="entry-large"))
        report = research.status(self.root, packet["batch_id"])
        self.assertEqual(report["research_effort"]["overall"]["unique_discovered"], 1)
        self.assertEqual(report["counts"]["new_qualified"], 0)
        self.assertFalse(report["market_sequence"]["needs_scope_review"])
        self.assertFalse(report["should_stop"])

    def test_scope_review_preserves_unfinished_market_and_allows_more_investigation(self):
        packet = self.market_packet()
        research.collection_plan(self.root, packet["batch_id"], self.market_plan(packet))
        self.route_entry(packet, "large", purpose="route_reconnaissance")
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="entry-large", discovered_lead_ids=[]))
        before = self.log()
        master_hash = kit.load_master(self.root)[1]
        result = research.market_progress(self.root, packet["batch_id"], "market-a", self.scope_review_update(packet, review_batch_id="forged"))
        sequence = result["market_sequence"]
        self.assertTrue(sequence["needs_scope_review"])
        self.assertFalse(sequence["active_market_complete"])
        self.assertIsNone(sequence["next_market_id"])
        self.assertEqual(sequence["active_market"]["progress"]["review_batch_id"], packet["batch_id"])
        self.assertEqual(sequence["active_market"]["progress"]["completion_scope"], "partial")
        self.assertEqual(result["scope_completion"], "partial")
        self.assertEqual(kit.load_master(self.root)[1], master_hash)
        after = self.log()
        for field in ("scope_id", "started_at", "policy", "attempts", "entries", "read_history"):
            self.assertEqual(after[field], before[field])
        report = research.status(self.root, packet["batch_id"])
        self.assertFalse(report["should_stop"])
        self.assertIn("范围复核", report["next_action"])
        self.route_entry(packet, "large", entry_id="further-discovery", url="https://example.invalid/further", work_kind="discovery")
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, aid="attempt-further", eid="further-discovery"))
        self.assertEqual(research.status(self.root, packet["batch_id"])["attempts"], 2)
        with self.assertRaisesRegex(kit.LeadkitError, "restricted to the active market"):
            self.route_entry(packet, "medium")

    def test_scope_review_rejects_unread_failed_unlinked_and_other_market_evidence(self):
        packet = self.market_packet()
        packet["sources"].append(dict(packet["sources"][0], source_id="SRC-UNREAD", url="https://example.invalid/unread"))
        self.save(packet)
        research.collection_plan(self.root, packet["batch_id"], self.market_plan(packet))
        self.route_entry(packet, "large", entry_id="failed", url="https://example.invalid/failed", purpose="route_reconnaissance")
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, aid="attempt-failed", eid="failed", state="failed"))
        self.route_entry(packet, "medium", purpose="route_reconnaissance")
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, aid="attempt-other", eid="entry-medium", discovered_lead_ids=[]))
        self.route_entry(packet, "large", purpose="route_reconnaissance")
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="entry-large", discovered_lead_ids=[]))
        self.route_entry(packet, "large", entry_id="city-background", url="https://example.invalid/background", purpose="channel_research")
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, aid="attempt-background", eid="city-background", discovered_lead_ids=[]))
        cases = [({"attempt_ids": []}, "own real boundary attempts"),
                 ({"attempt_ids": ["not-recorded"]}, "own real boundary attempts"),
                 ({"attempt_ids": ["attempt-failed"]}, "actual readable attempts"),
                 ({"attempt_ids": ["attempt-other"]}, "own real boundary attempts"),
                 ({"source_ids": ["SRC-001", "SRC-UNREAD"]}, "actual readable attempts"),
                 ({"attempt_ids": ["attempt-background"]}, "reconnaissance or merchant"),
                 ({"directory_exhausted": True}, "directory_exhausted=false")]
        before = self.log()
        for changes, message in cases:
            with self.subTest(changes=changes), self.assertRaisesRegex(kit.LeadkitError, message):
                research.market_progress(self.root, packet["batch_id"], "market-a", self.scope_review_update(packet, **changes))
            self.assertEqual(self.log(), before)

    def test_scope_review_can_record_reconnaissance_before_city_preflight_is_complete(self):
        packet = self.market_packet()
        plan = self.market_plan(packet)
        plan["contact_ecosystem"] = []
        research.collection_plan(self.root, packet["batch_id"], plan)
        self.route_entry(packet, "large", purpose="route_reconnaissance")
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="entry-large", discovered_lead_ids=[]))
        report = research.market_progress(self.root, packet["batch_id"], "market-a", self.scope_review_update(packet))
        self.assertFalse(report["preflight_ready"])
        self.assertTrue(report["market_sequence"]["needs_scope_review"])
        with self.assertRaisesRegex(kit.LeadkitError, "preflight"):
            self.route_entry(packet, "large", entry_id="merchant", url="https://example.invalid/merchant-new")

    def test_scope_review_finish_before_budget_continues_same_market_without_reusing_old_review(self):
        import leadkit_workflow as flow
        packet = self.market_packet()
        packet["leads"][0]["status"] = "candidate"
        self.save(packet)
        research.collection_plan(self.root, packet["batch_id"], self.market_plan(packet))
        self.route_entry(packet, "large", work_kind="discovery")
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="entry-large"))
        self.route_entry(packet, "large", entry_id="pending", url="https://example.invalid/pending", work_kind="discovery")
        with self.assertRaisesRegex(kit.LeadkitError, "evidenced review_required"):
            research.finish(self.root, packet["batch_id"], "scope_review_required", "No review record yet")
        research.market_progress(self.root, packet["batch_id"], "market-a", self.scope_review_update(packet))
        research.finish(self.root, packet["batch_id"], "scope_review_required", "One actual read supports an early scope review; budget is not exhausted")
        report = research.status(self.root, packet["batch_id"])
        self.assertEqual(report["attempts"], 1)
        self.assertLess(report["attempts"], report["policy"]["work_budget"]["max_attempts"])
        self.assertEqual(report["stop_reason"], "scope_review_required")
        self.assertTrue(report["should_stop"])
        self.assertFalse(report["collection_complete"])
        fixtures.LeadkitTests.commit(self, packet)
        result = flow.continue_task(self.root, packet["batch_id"], "review-continued")
        carried = result["carried_research"]["market_sequence"]
        self.assertEqual(carried["active_market_id"], "market-a")
        self.assertTrue(carried["needs_scope_review"])
        self.assertIsNone(carried["next_market_id"])
        report = research.status(self.root, "review-continued")
        self.assertFalse(report["should_stop"])
        self.assertIn("pending", report["pending_entry_ids"])
        with self.assertRaisesRegex(kit.LeadkitError, "from this round"):
            research.finish(self.root, "review-continued", "scope_review_required", "Cannot recycle the previous round's review without current evidence")
        self.route_entry(result["packet"], "large", entry_id="new-evidence", url="https://example.invalid/new-evidence", purpose="route_reconnaissance")
        research.record_attempt(self.root, "review-continued", self.attempt(packet, aid="new-attempt", eid="new-evidence", discovered_lead_ids=[]))
        self.assertEqual(research.status(self.root, "review-continued")["attempts"], 1)
        review = self.scope_review_update(packet, attempt_ids=["new-attempt"])
        research.market_progress(self.root, "review-continued", "market-a", review)
        research.finish(self.root, "review-continued", "scope_review_required", "A new actually read source supports this round's review")

    def test_reviewed_market_can_complete_after_real_boundary_work_and_cannot_be_downgraded(self):
        packet = self.market_packet()
        research.collection_plan(self.root, packet["batch_id"], self.market_plan(packet))
        self.route_entry(packet, "large")
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="entry-large"))
        research.market_progress(self.root, packet["batch_id"], "market-a", self.scope_review_update(packet))
        research.route_progress(self.root, packet["batch_id"], "route-large", {"state": "complete", "detail": "Synthetic actual observed page now processed", "cursor": "end", "attempt_ids": ["attempt-1"], "directory_exhausted": True})
        result = research.market_progress(self.root, packet["batch_id"], "market-a", self.scope_review_update(packet, state="complete", directory_exhausted=True))
        self.assertTrue(result["market_sequence"]["active_market_complete"])
        self.assertFalse(result["market_sequence"]["needs_scope_review"])
        self.assertEqual(result["market_sequence"]["next_market_id"], "market-b")
        self.assertEqual(result["market_sequence"]["active_market"]["progress_history"][0]["state"], "review_required")
        with self.assertRaisesRegex(kit.LeadkitError, "completed market cannot"):
            research.market_progress(self.root, packet["batch_id"], "market-a", self.scope_review_update(packet))
        with self.assertRaisesRegex(kit.LeadkitError, "evidenced review_required"):
            research.finish(self.root, packet["batch_id"], "scope_review_required", "The market has since completed")

    def test_market_sequence_restricts_merchants_but_allows_cross_market_reconnaissance(self):
        packet = self.market_packet()
        report = research.collection_plan(self.root, packet["batch_id"], self.market_plan(packet))
        active = report["market_sequence"]["active_market"]
        self.assertEqual((active["country"], active["city"], active["market"]), ("测试国", "测试城", "甲批发城"))
        self.assertEqual(report["market_sequence"]["active_market_id"], "market-a")
        self.assertIsNone(report["market_sequence"]["next_market_id"])
        with self.assertRaisesRegex(kit.LeadkitError, "restricted to the active market"):
            self.route_entry(packet, "medium")
        self.route_entry(packet, "medium", entry_id="recon-other-market", purpose="route_reconnaissance")
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, aid="recon", eid="recon-other-market", discovered_lead_ids=[]))
        self.assertEqual(research.status(self.root, packet["batch_id"])["market_sequence"]["active_market_id"], "market-a")
        self.route_entry(packet, "large")
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="entry-large"))
        with self.assertRaisesRegex(kit.LeadkitError, "never jump to another market"):
            research.route_progress(self.root, packet["batch_id"], "route-large", self.defer_update())

    def test_market_completion_rejects_unread_routes_and_entries(self):
        packet = self.market_packet()
        research.collection_plan(self.root, packet["batch_id"], self.market_plan(packet))
        update = {"state": "complete", "detail": "Attempted synthetic close", "boundary": "Observed page 1", "cursor": "page=1", "attempt_ids": [], "source_ids": ["SRC-001"], "directory_exhausted": True}
        with self.assertRaisesRegex(kit.LeadkitError, "uncompleted"):
            research.market_progress(self.root, packet["batch_id"], "market-a", update)
        self.route_entry(packet, "large")
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="entry-large"))
        pending = {"entry_id": "unread-card", "channel_id": "synthetic_directory", "kind": "page", "url": "https://example.invalid/observed-next", "products": ["智能眼镜"], "route_id": "route-large", "next_action": "Read actual remaining card"}
        research.plan_entry(self.root, packet["batch_id"], pending, defer=True)
        with self.assertRaisesRegex(kit.LeadkitError, "unread or blocked"):
            research.route_progress(self.root, packet["batch_id"], "route-large", {"state": "complete", "detail": "Cannot hide pending card", "cursor": "page=1", "attempt_ids": ["attempt-1"], "directory_exhausted": True})
        with self.assertRaisesRegex(kit.LeadkitError, "evidenced market-progress"):
            research.finish(self.root, packet["batch_id"], "market_complete", "Cannot close before the market boundary")

    def test_completed_market_waits_for_new_batch_and_new_city_requires_preflight(self):
        packet = self.market_packet()
        research.collection_plan(self.root, packet["batch_id"], self.market_plan(packet))
        report = self.complete_market(packet)
        self.assertEqual(report["market_sequence"]["next_market_id"], "market-b")
        expanded = self.market_plan(packet)
        expanded["routes"].append(dict(expanded["routes"][0], route_id="late-unread", entrypoint="https://example.invalid/late-unread"))
        with self.assertRaisesRegex(kit.LeadkitError, "completed market boundary"):
            research.collection_plan(self.root, packet["batch_id"], expanded)
        with self.assertRaisesRegex(kit.LeadkitError, "market is complete"):
            self.route_entry(packet, "medium")
        research.finish(self.root, packet["batch_id"], "market_complete", "First market observed boundary complete")
        fixtures.LeadkitTests.commit(self, packet)
        second = self.market_packet(bid="round-2", sid="SRC-002")
        second["leads"][0]["market_assignment"]["market_id"] = "market-b"
        self.save(second)
        research.carry_forward(self.root, "round-1", "round-2")
        next_report = research.status(self.root, "round-2")
        self.assertEqual(next_report["market_sequence"]["active_market_id"], "market-b")
        self.assertTrue(next_report["preflight_ready"])
        self.complete_market(second, "medium", "market-b")
        research.finish(self.root, "round-2", "market_complete", "Second market observed boundary complete")
        fixtures.LeadkitTests.commit(self, second)
        third = self.market_packet(bid="round-3", sid="SRC-003")
        research.carry_forward(self.root, "round-2", "round-3")
        third_report = research.status(self.root, "round-3")
        self.assertEqual(third_report["market_sequence"]["active_market_id"], "market-c")
        self.assertFalse(third_report["city_preflight_ready"])
        self.assertFalse(third_report["preflight_ready"])
        with self.assertRaisesRegex(kit.LeadkitError, "preflight"):
            self.route_entry(third, "small")
        self.assertEqual(third_report["attempts"], 0)

    def test_budget_continue_keeps_same_market_and_workflow_returns_market_context(self):
        import leadkit_workflow as flow
        packet = self.market_packet(work_budget={"max_attempts": 1, "max_minutes": 45})
        packet["leads"][0]["status"] = "candidate"
        self.save(packet)
        research.collection_plan(self.root, packet["batch_id"], self.market_plan(packet))
        self.route_entry(packet, "large", work_kind="qualification", subject_ids=["TEST-001"])
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="entry-large"))
        research.finish(self.root, packet["batch_id"], "attempt_budget", "Market still has unread entries")
        fixtures.LeadkitTests.commit(self, packet)
        result = flow.continue_task(self.root, packet["batch_id"], "next-market-round")
        report = research.status(self.root, "next-market-round")
        self.assertEqual(report["market_sequence"]["active_market_id"], "market-a")
        self.assertFalse(report["market_sequence"]["active_market_complete"])
        self.assertEqual(report["policy"]["work_budget"], {"max_attempts": 1, "max_minutes": 45})
        self.assertEqual(report["attempts"], 0)
        self.assertEqual(result["carried_research"]["market_sequence"]["active_market_id"], "market-a")

    def test_market_deferral_can_change_internal_route_but_cannot_complete_market(self):
        packet = self.market_packet()
        plan = self.market_plan(packet)
        plan["routes"].append(dict(plan["routes"][0], route_id="route-inner", size="medium", order=4, entrypoint="https://example.invalid/inner-directory"))
        research.collection_plan(self.root, packet["batch_id"], plan)
        self.route_entry(packet, "large")
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="entry-large"))
        report = research.route_progress(self.root, packet["batch_id"], "route-large", self.defer_update(alternative="route-inner"))
        self.assertEqual(report["next_route_id"], "route-inner")
        self.assertEqual(report["market_sequence"]["active_market_id"], "market-a")
        self.route_entry(packet, "inner")
        with self.assertRaisesRegex(kit.LeadkitError, "uncompleted"):
            research.market_progress(self.root, packet["batch_id"], "market-a", {"state": "complete", "detail": "Cannot skip the deferred route", "boundary": "Synthetic page 1", "cursor": "page=1", "attempt_ids": ["attempt-1"], "source_ids": ["SRC-001"], "directory_exhausted": True})

    def test_market_assignment_is_required_for_main_but_unknown_position_can_be_candidate(self):
        packet = self.market_packet(work_budget={"max_attempts": 1, "max_minutes": 45})
        research.collection_plan(self.root, packet["batch_id"], self.market_plan(packet))
        self.route_entry(packet, "large")
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet, eid="entry-large"))
        research.finish(self.root, packet["batch_id"], "attempt_budget", "Partial market stage")
        master, _ = kit.load_master(self.root)
        self.assertEqual(research.summary_for_commit(self.root, packet, master)["counts"]["qualified"], 1)
        for change in (None, {"market_id": "market-b", "relation": "within", "detail": "Wrong market", "source_ids": ["SRC-001"]}, {"market_id": "market-a", "relation": "nearby", "detail": "Nearby is not within", "source_ids": ["SRC-001"]}, {"market_id": "market-a", "relation": "within", "detail": "No readable proof", "source_ids": []}):
            invalid = copy.deepcopy(packet)
            if change is None:
                invalid["leads"][0].pop("market_assignment")
            else:
                invalid["leads"][0]["market_assignment"] = change
            with self.subTest(change=change), self.assertRaises(kit.LeadkitError):
                research.summary_for_commit(self.root, invalid, master)
        candidate = copy.deepcopy(packet)
        candidate["leads"][0].pop("market_assignment")
        candidate["leads"][0]["status"] = "candidate"
        self.assertEqual(research.summary_for_commit(self.root, candidate, master)["counts"]["qualified"], 0)

    def test_market_directory_cannot_be_reordered_or_claim_unexplored_hierarchy_complete(self):
        packet = self.market_packet()
        plan = self.market_plan(packet)
        research.collection_plan(self.root, packet["batch_id"], plan)
        changed = copy.deepcopy(plan)
        changed["market_directory"]["markets"][0]["order"] = 2
        changed["market_directory"]["markets"][1]["order"] = 1
        with self.assertRaisesRegex(kit.LeadkitError, "sequence are fixed"):
            research.collection_plan(self.root, packet["batch_id"], changed)
        changed = copy.deepcopy(plan)
        changed["batch_market_id"] = "market-b"
        with self.assertRaisesRegex(kit.LeadkitError, "fixed to one market"):
            research.collection_plan(self.root, packet["batch_id"], changed)
        changed = copy.deepcopy(plan)
        changed["market_directory"]["cities"].append({"city_id": "city-unexplored", "country_id": "country-test", "name": "未研究城市", "order": 3, "source_ids": ["SRC-001"]})
        changed["market_directory"]["coverage"] = {"status": "complete", "detail": "Cannot call this complete", "source_ids": ["SRC-001"]}
        with self.assertRaisesRegex(kit.LeadkitError, "unexplored"):
            research.collection_plan(self.root, packet["batch_id"], changed)

    def test_brand_stores_are_reported_but_never_meet_independent_main_target(self):
        packet = self.packet(target_count=1)
        packet["task"]["customer_profile"].update(brand_policy="independent_product", collect_brand_stores=True)
        packet["leads"][0]["brand_profile"] = "official_store"
        self.save(packet)
        self.entry(packet)
        research.record_attempt(self.root, packet["batch_id"], self.attempt(packet))
        report = research.status(self.root, packet["batch_id"])
        self.assertFalse(report["target_reached"])
        self.assertEqual(report["counts"]["brand_store_main"], 1)
        self.assertEqual(report["counts"]["independent_main"], 0)
        self.assertEqual(report["counts"]["new_qualified"], 0)


if __name__ == "__main__":
    unittest.main()
