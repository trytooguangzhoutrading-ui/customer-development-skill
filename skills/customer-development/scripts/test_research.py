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


if __name__ == "__main__":
    unittest.main()
