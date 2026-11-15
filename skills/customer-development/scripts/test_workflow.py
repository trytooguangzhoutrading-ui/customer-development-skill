#!/usr/bin/env python3
"""Synthetic, offline regression checks for research continuity and corrections."""
import copy
import json
import tempfile
import unittest
import zipfile
from unittest import mock
from pathlib import Path
from xml.etree import ElementTree as ET

import leadkit as core
import leadkit_research as research
import leadkit_workflow as flow
import test_leadkit as fixtures


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "workspace"
        core.init_workspace(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def packet(self, *args, **kwargs):
        packet = fixtures.LeadkitTests.packet(self, *args, **kwargs)
        # These fixtures isolate record/correction behavior. Dedicated tests
        # exercise the systematic collection plan, rather than fabricate one.
        packet["task"].update(collection_strategy="legacy", completion_policy="target_or_coverage")
        lead = packet["leads"][0]
        sid = packet["sources"][0]["source_id"]
        lead.setdefault("discovered_by", [{"channel": "synthetic_directory", "source_ids": [sid]}])
        if not packet.get("coverage"):
            packet["coverage"] = [{"product": "智能眼镜", "channel": "synthetic_directory", "status": "searched", "attempts": 1, "source_ids": [sid], "limitations": [], "discovered_lead_ids": [lead["lead_id"]]}]
        return packet

    def edit_workbook(self, path, edits, shared=False):
        with zipfile.ZipFile(path) as book:
            parts = {name: book.read(name) for name in book.namelist()}
        sheet = ET.fromstring(parts["xl/worksheets/sheet1.xml"])
        for address, value in edits.items():
            cells = [c for c in sheet.iter(core.tag("c")) if c.get("r") == address]
            self.assertEqual(len(cells), 1)
            cell = cells[0]
            for child in list(cell):
                cell.remove(child)
            cell.set("t", "inlineStr")
            ET.SubElement(ET.SubElement(cell, core.tag("is")), core.tag("t")).text = value
        if shared:
            strings = ET.Element(core.tag("sst"))
            for cell in sheet.iter(core.tag("c")):
                value = "".join(n.text or "" for n in cell.iter(core.tag("t")))
                index = len(strings)
                ET.SubElement(ET.SubElement(strings, core.tag("si")), core.tag("t")).text = value
                for child in list(cell):
                    cell.remove(child)
                cell.set("t", "s")
                ET.SubElement(cell, core.tag("v")).text = str(index)
            parts["xl/sharedStrings.xml"] = core.xml_bytes(strings)
        parts["xl/worksheets/sheet1.xml"] = core.xml_bytes(sheet)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as book:
            for name, value in parts.items():
                book.writestr(name, value)

    def committed_template(self):
        core.commit(self.root, self.packet())
        path = Path(self.temp.name) / "纠错.xlsx"
        flow.correction_template(self.root, path)
        return path

    def test_company_defaults_and_task_override(self):
        config = core.read_json(self.root / "config.json")
        config.update(products=["智能眼镜"], default_buyer_types=["批发商"], default_region={"country": "甲国", "city": "甲城", "market": "甲市场"}, supplier_profile={"supply_mode": "wholesale"}, activity_max_age_days=90)
        core.safe_write(self.root / "config.json", config)
        first = flow.snapshot(self.root, "default-test")
        task = core.read_json(first["batch_path"])["task"]
        self.assertEqual(task["products"], ["智能眼镜"])
        self.assertEqual(task["countries"], ["甲国"])
        self.assertEqual(task["city"], "甲城")
        self.assertEqual(task["customer_profile"]["buyer_types"], ["批发商"])
        self.assertEqual(task["customer_profile"]["supplier_constraints"], {"supply_mode": "wholesale"})
        second = flow.snapshot(self.root, "override-test", {"countries": ["乙国"], "products": ["蓝牙耳机"], "request": "指定任务"})
        task = core.read_json(second["batch_path"])["task"]
        self.assertEqual(task["countries"], ["乙国"])
        self.assertNotIn("city", task)
        self.assertNotIn("market", task)
        self.assertEqual(task["products"], ["蓝牙耳机"])
        self.assertEqual(core.read_json(self.root / "config.json"), config)

    def test_narrow_geo_override_updates_effective_scope_and_clears_children(self):
        config = core.read_json(self.root / "config.json")
        config["default_region"] = [{"country": "甲国", "city": "甲城", "market": "旧市场", "address": "旧地址"}]
        core.safe_write(self.root / "config.json", config)
        market = flow.snapshot(self.root, "new-market", {"market": "新市场"})
        task = core.read_json(market["batch_path"])["task"]
        self.assertEqual(core.task_scopes(task), [{"country": "甲国", "city": "甲城", "market": "新市场"}])
        self.assertNotIn("address", task)
        address = flow.snapshot(self.root, "new-address", {"address": "新地址"})
        self.assertEqual(core.task_scopes(core.read_json(address["batch_path"])["task"])[0]["address"], "新地址")
        city = flow.snapshot(self.root, "new-city", {"city": "乙城"})
        self.assertEqual(core.task_scopes(core.read_json(city["batch_path"])["task"]), [{"country": "甲国", "city": "乙城"}])
        with self.assertRaisesRegex(core.LeadkitError, "Contradictory"):
            flow.snapshot(self.root, "contradiction", {"countries": ["甲国"], "geo_scope": [{"country": "乙国"}]})
        with self.assertRaisesRegex(core.LeadkitError, "Contradictory"):
            flow.snapshot(self.root, "contradiction-city", {"city": "乙城", "geo_scope": [{"country": "甲国", "city": "甲城"}]})
        self.assertFalse((self.root / "research/contradiction").exists())

    def test_policy_company_defaults_and_explicit_override(self):
        config = core.read_json(self.root / "config.json")
        config.update(mode="enrich", target_count=8, target_metric="updated", work_budget={"max_attempts": 22, "max_minutes": 12}, business_priorities=["supplier_fit"], preferred_buyer_types=["批发商"])
        core.safe_write(self.root / "config.json", config)
        result = flow.snapshot(self.root, "policy", {"target_count": 3, "work_budget": {"max_minutes": 5}})
        task = core.read_json(result["batch_path"])["task"]
        self.assertEqual(task["target_count"], 3)
        self.assertEqual(task["target_metric"], "updated")
        self.assertEqual(task["work_budget"], {"max_attempts": 22, "max_minutes": 5})
        self.assertEqual(task["preferred_buyer_types"], ["批发商"])
        with self.assertRaises(core.LeadkitError):
            flow.snapshot(self.root, "bad-policy", {"target_count": 0})

    def test_new_snapshot_defaults_to_coverage_not_automatic_twenty_customer_completion(self):
        result = flow.snapshot(self.root, "coverage-default")
        task = core.read_json(result["batch_path"])["task"]
        self.assertEqual(task["collection_strategy"], "market_sequence")
        self.assertEqual(task["completion_policy"], "coverage")
        self.assertEqual(task["target_count"], 20)
        result = flow.snapshot(self.root, "explicit-count", {"target_count": 60})
        self.assertEqual(core.read_json(result["batch_path"])["task"]["completion_policy"], "target_or_coverage")
        result = flow.snapshot(self.root, "explicit-coverage", {"target_count": 60, "completion_policy": "coverage"})
        self.assertEqual(core.read_json(result["batch_path"])["task"]["completion_policy"], "coverage")
        result = flow.snapshot(self.root, "explicit-old-strategy", {"collection_strategy": "region_size_ladder"})
        self.assertEqual(core.read_json(result["batch_path"])["task"]["collection_strategy"], "region_size_ladder")

    def test_single_sheet_default_and_old_continuation_keep_separate_layouts(self):
        result = flow.snapshot(self.root, "single-default")
        self.assertEqual(core.read_json(result["batch_path"])["task"]["output_layout"], "single_sheet")
        packet = self.packet("historical-layout")
        self.assertNotIn("output_layout", packet["task"])
        core.commit(self.root, packet)
        frozen = copy.deepcopy(core.load_master(self.root)[0]["batches"]["historical-layout"])
        continued = flow.continue_task(self.root, "historical-layout", "legacy-continued")
        task = continued["packet"]["task"]
        self.assertEqual(task["output_layout"], "legacy")
        self.assertEqual(core.scope_key(task), core.scope_key(packet["task"]))
        self.assertEqual(core.load_master(self.root)[0]["batches"]["historical-layout"], frozen)
        with self.assertRaisesRegex(core.LeadkitError, "output_layout"):
            flow.snapshot(self.root, "bad-layout", {"output_layout":"many_new_tables"})
        self.assertFalse((self.root / "research/bad-layout").exists())

    def test_policy_review_detects_new_brand_and_market_defaults_without_changing_old_task(self):
        task = self.packet()["task"]
        config = core.read_json(self.root / "config.json")
        config.update(products=task["products"], supplementary_products=[], brand_policy="independent_product", collect_brand_stores=True, collection_strategy="market_sequence")
        core.safe_write(self.root / "config.json", config)
        before = copy.deepcopy(task)
        report = flow._configuration_review(self.root, task)
        self.assertFalse(report["product_scope_differs"])
        self.assertTrue(report["policy_scope_differs"])
        self.assertEqual(set(report["policy_differences"]), {"brand_policy", "collect_brand_stores", "collection_strategy"})
        self.assertEqual(task, before)
        result = flow.snapshot(self.root, "new-policies")
        current = core.read_json(result["batch_path"])["task"]
        self.assertEqual(current["customer_profile"]["brand_policy"], "independent_product")
        self.assertTrue(current["customer_profile"]["collect_brand_stores"])
        self.assertEqual(current["collection_strategy"], "market_sequence")

    def test_prepare_coverage_uses_current_policy_and_keeps_brand_store_separate(self):
        packet = self.packet()
        packet["task"]["customer_profile"].update(brand_policy="independent_product", collect_brand_stores=True)
        packet["leads"][0].update(brand_profile="independent", white_label_evidence=[])
        result = flow._subset(packet, packet["leads"])
        self.assertEqual(result["coverage"][0]["status"], "searched")
        packet["leads"][0]["brand_profile"] = "official_store"
        result = flow._subset(packet, packet["leads"])
        self.assertEqual(result["coverage"][0]["status"], "searched_zero")
        self.assertEqual(len(result["leads"]), 1)

    def test_enrich_defaults_to_provided_leads_without_rescanning_city(self):
        result = flow.snapshot(self.root, "enrich-known", {"mode": "enrich"})
        task = core.read_json(result["batch_path"])["task"]
        self.assertEqual(task["collection_strategy"], "provided_leads")
        self.assertEqual(task["completion_policy"], "target_or_coverage")
        self.assertEqual(task["target_metric"], "updated")
        result = flow.snapshot(self.root, "enrich-explicit-map", {"mode": "enrich", "collection_strategy": "region_size_ladder", "completion_policy": "coverage"})
        self.assertEqual(core.read_json(result["batch_path"])["task"]["collection_strategy"], "region_size_ladder")

    def test_active_pointer_resume_and_ambiguity_never_guess(self):
        flow.snapshot(self.root, "first")
        second = flow.snapshot(self.root, "second")
        self.assertEqual(flow.status(self.root)["batch_id"], "second")
        result = flow.continue_task(self.root)
        self.assertFalse(result["created"])
        self.assertEqual(result["batch_id"], "second")
        index_path = self.root / "execution/task-index.json"
        index = core.read_json(index_path)
        index.pop("active_batch_id")
        core.safe_write(index_path, index)
        result = flow.continue_task(self.root)
        self.assertTrue(result["selection_required"])
        self.assertEqual({x["batch_id"] for x in result["candidates"]}, {"first", "second"})
        packet = core.read_json(second["batch_path"])
        flow.checkpoint(self.root, packet)
        self.assertEqual(flow.status(self.root)["batch_id"], "second")

    def test_pending_configuration_review_preserves_scope_budget_and_records(self):
        core.commit(self.root, self.packet())
        config = core.read_json(self.root / "config.json")
        config.update(products=["智能眼镜", "蓝牙耳机"], supplementary_products=[])
        core.safe_write(self.root / "config.json", config)
        created = flow.snapshot(self.root, "pending-old-scope", {"countries": ["甲国"], "city": "甲城", "collection_strategy": "legacy"})
        packet = core.read_json(created["batch_path"])
        flow.checkpoint(self.root, packet)
        research.register_channel(self.root, {"channel_id": "READINESS", "name": "离线测试", "kind": "website", "readiness": {"status": "untested", "checked_at": core.now(), "detail": "不联网"}})
        research.plan_entry(self.root, packet["batch_id"], {"entry_id": "NEXT", "channel_id": "READINESS", "kind": "page", "url": "https://example.invalid/next", "products": ["智能眼镜"], "next_action": "未执行的测试入口"})
        index_path = self.root / "execution/task-index.json"
        index = core.read_json(index_path)
        index["tasks"][packet["batch_id"]]["employee_marker"] = "保留索引扩展"
        core.safe_write(index_path, index)
        paths = [self.root / "execution/master.json", Path(created["batch_path"]), self.root / "research/pending-old-scope/research-log.json"]
        before = {p: p.read_bytes() for p in paths}
        index_before = index_path.read_bytes()
        config.update(products=core.PRIMARY_PRODUCTS, supplementary_products=core.SUPPLEMENTARY_PRODUCTS)
        core.safe_write(self.root / "config.json", config)
        review = flow.status(self.root)["configuration_review"]
        self.assertTrue(review["product_scope_differs"])
        self.assertEqual(review["differences"]["products"]["only_in_task"], ["蓝牙耳机"])
        self.assertIn("智能手表表带", review["differences"]["products"]["only_in_defaults"])
        self.assertTrue(review["configuration_file_changed_since_task"])
        self.assertEqual(index_before, index_path.read_bytes())
        continued = flow.continue_task(self.root)
        self.assertFalse(continued["created"])
        self.assertTrue(continued["configuration_review"]["product_scope_differs"])
        self.assertEqual(continued["packet"]["task"], packet["task"])
        for p, contents in before.items():
            self.assertEqual(p.read_bytes(), contents)
        index_after = core.read_json(index_path)
        self.assertEqual(index_after["active_batch_id"], packet["batch_id"])
        self.assertEqual(index_after["tasks"][packet["batch_id"]]["employee_marker"], "保留索引扩展")

    def test_committed_configuration_review_keeps_scope_and_new_snapshot_uses_defaults(self):
        packet = self.packet()
        core.commit(self.root, packet)
        before = (self.root / "execution/master.json").read_bytes()
        config = core.read_json(self.root / "config.json")
        config.update(products=["智能手表", "智能手表表带"], supplementary_products=["蓝牙音响"])
        core.safe_write(self.root / "config.json", config)
        selected = flow.status(self.root)
        self.assertTrue(selected["committed"])
        self.assertEqual(selected["configuration_review"]["task_scope"]["products"], ["智能眼镜"])
        continued = flow.continue_task(self.root, new_batch_id="same-scope")
        self.assertEqual(continued["configuration_review"], selected["configuration_review"])
        self.assertEqual(continued["packet"]["task"]["products"], ["智能眼镜"])
        self.assertEqual(continued["packet"]["task"]["supplementary_products"], [])
        self.assertEqual(core.scope_key(continued["packet"]["task"]), core.scope_key(packet["task"]))
        self.assertEqual(before, (self.root / "execution/master.json").read_bytes())
        self.assertEqual(core.load_master(self.root)[0]["leads"]["TEST-001"]["manual_notes"], "人工初始记录")
        fresh = flow.snapshot(self.root, "new-positioning")
        fresh_task = core.read_json(fresh["batch_path"])["task"]
        self.assertEqual(fresh_task["products"], config["products"])
        self.assertEqual(fresh_task["supplementary_products"], config["supplementary_products"])
        self.assertEqual(fresh_task["countries"], [])
        self.assertNotIn("geo_scope", fresh_task)
        self.assertNotIn("city", fresh_task)

    def test_specialist_difference_is_not_reported_as_configuration_change(self):
        info = flow.snapshot(self.root, "specialist", {"countries": ["甲国"], "products": ["蓝牙耳机"]})
        review = flow.status(self.root)["configuration_review"]
        self.assertTrue(review["product_scope_differs"])
        self.assertFalse(review["configuration_file_changed_since_task"])
        self.assertFalse(review["scope_changed_automatically"])
        self.assertEqual(review["task_scope"]["supplementary_products"], [])
        self.assertEqual(core.read_json(info["batch_path"])["task"]["products"], ["蓝牙耳机"])

    def test_configuration_hash_and_version_differences_do_not_imply_product_change(self):
        flow.snapshot(self.root, "same-products", {"countries": ["甲国"]})
        config = core.read_json(self.root / "config.json")
        config["products"] = list(reversed(config["products"]))
        config["employee_note"] = "元数据变化，不改变业务范围"
        core.safe_write(self.root / "config.json", config)
        with mock.patch.object(core, "VERSION", "9.9.9"):
            review = flow.status(self.root)["configuration_review"]
        self.assertFalse(review["product_scope_differs"])
        self.assertEqual(review["differences"], {})
        self.assertTrue(review["configuration_file_changed_since_task"])
        self.assertEqual(review["rules_versions"]["runtime"], "9.9.9")
        self.assertEqual(review["rules_versions"]["binding"], core.VERSION)
        self.assertEqual(review["versions_differ_from_runtime"]["task_runtime"], core.VERSION)

    def test_legacy_supplementary_absence_and_effective_task_defaults(self):
        config = core.read_json(self.root / "config.json")
        config.pop("supplementary_products")
        config["products"] = ["智能眼镜"]
        core.safe_write(self.root / "config.json", config)
        info = flow.snapshot(self.root, "legacy-supplementary")
        packet = core.read_json(info["batch_path"])
        packet["task"].pop("supplementary_products")
        flow.checkpoint(self.root, packet)
        review = flow.status(self.root)["configuration_review"]
        self.assertFalse(review["product_scope_differs"])
        self.assertEqual(review["current_defaults"]["supplementary_products"], [])
        config.update(products=core.PRIMARY_PRODUCTS, supplementary_products=core.SUPPLEMENTARY_PRODUCTS,
                      task_defaults={"products": ["蓝牙耳机"]})
        core.safe_write(self.root / "config.json", config)
        scoped = flow.snapshot(self.root, "default-specialist")
        actual = core.read_json(scoped["batch_path"])["task"]
        review = flow.status(self.root)["configuration_review"]
        self.assertFalse(review["product_scope_differs"])
        self.assertEqual(review["current_defaults"], {"products": actual["products"], "supplementary_products": actual["supplementary_products"]})

    def test_continue_committed_inherits_remaining_and_never_inserts_customers(self):
        packet = self.packet()
        packet["task"]["target_count"] = 3
        packet["queue"] = [{"issue_id": "FOLLOW", "lead_id": "TEST-001", "kind": "review", "detail": "待补采购入口", "state": "open", "next_action": "查看下一入口", "next_trigger": "next_collection", "attempts": 1, "source_ids": []}]
        core.commit(self.root, packet)
        before = (self.root / "execution/master.json").read_bytes()
        result = flow.continue_task(self.root, new_batch_id="next")
        self.assertTrue(result["created"])
        continued = result["packet"]
        self.assertEqual(continued["task"]["target_count"], 2)
        self.assertEqual(continued["task"]["mode"], "continue")
        self.assertEqual(continued["task"]["work_budget"], packet["task"]["work_budget"])
        self.assertEqual(continued["queue"], packet["queue"])
        self.assertEqual(continued["leads"], [])
        self.assertEqual(continued["region_research"], packet["region_research"])
        self.assertEqual(before, (self.root / "execution/master.json").read_bytes())
        self.assertEqual(flow.status(self.root)["batch_id"], "next")

    def test_continue_after_target_met_starts_another_round(self):
        packet = self.packet()
        packet["task"]["target_count"] = 1
        core.commit(self.root, packet)
        result = flow.continue_task(self.root, new_batch_id="another")
        self.assertEqual(result["packet"]["task"]["target_count"], 1)
        self.assertFalse(result["committed"])

    def test_continue_old_task_does_not_silently_upgrade_collection_gate(self):
        packet = self.packet()
        packet["task"].pop("collection_strategy", None)
        packet["task"].pop("completion_policy", None)
        core.commit(self.root, packet)
        result = flow.continue_task(self.root, new_batch_id="legacy-next")
        self.assertEqual(result["packet"]["task"]["collection_strategy"], "legacy")
        self.assertEqual(result["packet"]["task"]["completion_policy"], "target_or_coverage")
        self.assertIn("strategy_note", result)

    def test_continue_carries_pending_ledger_and_read_history_with_fresh_budget(self):
        packet = self.packet()
        packet["task"]["target_count"] = 3
        flow.checkpoint(self.root, packet)
        channel = {"channel_id": "DIRECTORY", "name": "虚构目录", "kind": "website", "readiness": {"status": "readable", "checked_at": core.now(), "detail": "仅离线测试"}}
        research.register_channel(self.root, channel)
        entry = {"entry_id": "READ", "channel_id": "DIRECTORY", "kind": "page", "url": "https://example.invalid/read", "products": ["智能眼镜"], "next_action": "读取该页"}
        research.plan_entry(self.root, packet["batch_id"], entry)
        research.plan_entry(self.root, packet["batch_id"], dict(entry, entry_id="UNREAD", url="https://example.invalid/next"))
        research.record_attempt(self.root, packet["batch_id"], {"attempt_id": "ATTEMPT", "entry_id": "READ", "status": "read", "started_at": core.now(), "ended_at": core.now(), "detail": "仅离线测试读取记录", "source_ids": ["SRC-001"], "discovered_lead_ids": ["TEST-001"]})
        research.finish(self.root, packet["batch_id"], "batch_complete", "本批交付后继续其余入口")
        core.commit(self.root, packet)
        result = flow.continue_task(self.root, new_batch_id="carry-next")
        self.assertEqual(result["packet"]["task"]["target_count"], 2)
        log = core.read_json(self.root / "research/carry-next/research-log.json")
        self.assertEqual(set(log["entries"]), {"UNREAD"})
        self.assertEqual(log["attempts"], {})
        self.assertEqual(log["read_history"][0]["entry_id"], "READ")
        self.assertFalse(research.status(self.root, "carry-next")["should_stop"])

    def test_delivery_index_and_project_instructions_preserve_existing_rules(self):
        packet = self.packet()
        core.commit(self.root, packet)
        output = Path(self.temp.name) / "delivery.xlsx"
        core.export_batch(self.root, packet["batch_id"], output)
        delivery = flow.record_delivery(self.root, packet["batch_id"], output)
        self.assertEqual(flow.status(self.root)["latest_delivery"], delivery)
        path = self.root / "AGENTS.md"
        original = "# 本地项目规则\n保留已有规则。\n"
        path.write_text(original, encoding="utf-8")
        with self.assertRaisesRegex(core.LeadkitError, "dedicated"):
            flow.setup_project(self.root)
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        result = flow.setup_project(self.root, dedicated=True)
        self.assertEqual(Path(result["backup_path"]).read_text(encoding="utf-8"), original)
        self.assertTrue(path.read_text(encoding="utf-8").startswith(original))
        self.assertFalse(flow.setup_project(self.root, dedicated=True)["changed"])
        self.assertEqual(path.read_text(encoding="utf-8").count(flow.MANAGED_START), 1)

    def test_setup_project_upgrades_market_sequence_instructions_without_rewriting_local_rules(self):
        path = self.root / "AGENTS.md"
        old = "\n".join(["# 本地保留规则", flow.MANAGED_START,
                         "先完成当地渠道与联系生态预研，再按有据的大型、中型、小型路线逐层建立商户池。",
                         flow.MANAGED_END, "# 附加规则\n本地限制保持不变。\n"])
        path.write_text(old, encoding="utf-8")
        result = flow.setup_project(self.root, dedicated=True)
        updated = path.read_text(encoding="utf-8")
        self.assertEqual(Path(result["backup_path"]).read_text(encoding="utf-8"), old)
        self.assertTrue(updated.startswith("# 本地保留规则\n"))
        self.assertTrue(updated.endswith("# 附加规则\n本地限制保持不变。\n"))
        self.assertIn("国家→城市／地区→批发市场／批发城", updated)
        self.assertIn("预算用尽继续当前市场", updated)
        self.assertIn("一个 Excel、一个客户总表", updated)
        self.assertIn("不拆文件或工作表", updated)
        self.assertIn("历史任务保留其已冻结策略", updated)
        self.assertNotIn("再按有据的大型、中型、小型路线逐层建立商户池", updated)
        self.assertFalse(flow.setup_project(self.root, dedicated=True)["changed"])

    def test_checkpoint_saves_incomplete_draft_without_master_mutation(self):
        info = flow.snapshot(self.root, "draft")
        draft = core.read_json(info["batch_path"])
        before = (self.root / "execution/master.json").read_bytes()
        draft["task"]["request"] = "未完成采集"
        saved = flow.checkpoint(self.root, draft)
        self.assertFalse(saved["committed"])
        result = flow.resume(self.root, "draft")
        self.assertEqual(result["packet"], draft)
        self.assertEqual(before, (self.root / "execution/master.json").read_bytes())
        changed = copy.deepcopy(draft)
        changed["limitations"].append("访问受阻")
        flow.checkpoint(self.root, changed)
        self.assertTrue(Path(saved["revision_path"]).exists())
        self.assertEqual(flow.resume(self.root, "draft")["packet"], changed)

    def test_checkpoint_corruption_and_changed_baseline(self):
        a = self.packet()
        info = flow.snapshot(self.root, "draft")
        draft = core.read_json(info["batch_path"])
        saved = flow.checkpoint(self.root, draft)
        core.commit(self.root, a)
        self.assertTrue(flow.resume(self.root, "draft")["requires_reconciliation"])
        Path(saved["revision_path"]).write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(core.LeadkitError, "integrity"):
            flow.resume(self.root, "draft")

    def test_prepare_isolates_bad_record_and_keeps_original(self):
        packet = self.packet()
        bad = copy.deepcopy(packet["leads"][0])
        bad.update(lead_id="BAD-002", name="坏数据测试商户")
        bad["identity_source_ids"] = ["MISSING-SOURCE"]
        packet["leads"].append(bad)
        for cell in packet.get("coverage", []):
            cell["discovered_lead_ids"].append(bad["lead_id"])
        before = (self.root / "execution/master.json").read_bytes()
        output = Path(self.temp.name) / "prepared"
        report = flow.prepare(self.root, packet, output)
        self.assertEqual((report["accepted_count"], report["rejected_count"]), (1, 1))
        self.assertFalse(report["complete"])
        self.assertTrue(report["ready_to_commit"])
        self.assertEqual(before, (self.root / "execution/master.json").read_bytes())
        self.assertEqual(core.read_json(output / "original.json"), packet)
        prepared = core.read_json(output / "prepared.json")
        self.assertEqual([x["lead_id"] for x in prepared["leads"]], [packet["leads"][0]["lead_id"]])
        core.commit(self.root, prepared)
        self.assertEqual(len(core.load_master(self.root)[0]["leads"]), 1)
        self.assertEqual(core.read_json(output / "report.json")["rejected"][0]["record"], bad)

    def test_prepare_bad_shared_source_blocks_all(self):
        packet = self.packet()
        packet["sources"][0]["url"] = "broken-url"
        output = Path(self.temp.name) / "blocked"
        report = flow.prepare(self.root, packet, output)
        self.assertFalse(report["ready_to_commit"])
        self.assertTrue(report["shared_errors"])
        self.assertFalse((output / "prepared.json").exists())
        self.assertEqual(core.load_master(self.root)[0]["leads"], {})

    def test_prepare_duplicate_ids_isolates_both(self):
        packet = self.packet()
        packet["leads"].append(copy.deepcopy(packet["leads"][0]))
        report = flow.prepare(self.root, packet, Path(self.temp.name) / "duplicate")
        self.assertEqual(report["accepted_count"], 0)
        self.assertEqual(report["rejected_count"], 2)
        self.assertFalse(report["ready_to_commit"])

    def test_prepare_preserves_master_only_queue_resolution(self):
        first = self.packet()
        issue = {"issue_id": "OLD-QUEUE", "lead_id": "TEST-001", "kind": "review", "detail": "待补采购入口", "state": "open", "next_action": "核对入口", "next_trigger": "next_collection", "attempts": 0, "source_ids": []}
        first["queue"] = [issue]
        core.commit(self.root, first)
        second = self.packet("queue-close", "TEST-NEW", "SRC-NEW")
        second["leads"][0]["contacts"][0]["value"] = "+00987654321"
        second["queue"] = [dict(issue, state="resolved", attempts=1, detail="已经核对公开入口", source_ids=["SRC-NEW"], resolved_at=core.now())]
        result = flow.prepare(self.root, second, Path(self.temp.name) / "master-queue")
        prepared = core.read_json(result["prepared_path"])
        self.assertEqual(prepared["queue"], second["queue"])
        core.commit(self.root, prepared)
        self.assertEqual(core.load_master(self.root)[0]["queue"][0]["state"], "resolved")

    def test_prepare_queue_only_close_has_no_customers_and_is_committable(self):
        first = self.packet()
        first["queue"] = [{"issue_id": "QUEUE-ONLY", "lead_id": "TEST-001", "kind": "review", "detail": "待复核", "state": "open", "next_action": "复核公开记录", "next_trigger": "next_collection", "attempts": 0, "source_ids": []}]
        core.commit(self.root, first)
        packet = self.packet("queue-only", sid="QUEUE-SOURCE")
        packet["leads"] = []
        packet["coverage"][0].update(status="searched_zero", discovered_lead_ids=[])
        packet["queue"] = [dict(first["queue"][0], state="resolved", attempts=1, source_ids=["QUEUE-SOURCE"], resolved_at=core.now())]
        result = flow.prepare(self.root, packet, Path(self.temp.name) / "queue-only")
        self.assertTrue(result["ready_to_commit"])
        prepared = core.read_json(result["prepared_path"])
        self.assertEqual(prepared["queue"], packet["queue"])
        core.commit(self.root, prepared)
        self.assertEqual(len(core.load_master(self.root)[0]["leads"]), 1)

    def duplicate_master(self):
        packet = self.packet()
        first = packet["leads"][0]
        first["status"] = "candidate"
        alias = copy.deepcopy(first)
        alias.update(lead_id="TEST-ALIAS", name="待归并旧主体")
        packet["leads"].append(alias)
        packet["coverage"][0].update(status="searched_zero", discovered_lead_ids=[first["lead_id"], alias["lead_id"]])
        core.commit(self.root, packet)

    def alias_packet(self, batch_id):
        packet = self.packet(batch_id, sid="ALIAS-SOURCE")
        alias = copy.deepcopy(packet["leads"][0])
        alias.update(lead_id="TEST-ALIAS", name="待归并旧主体", status="excluded", canonical_id="TEST-001", exclusion={"code": "duplicate", "origin": "evidence", "detail": "证实为同一主体", "source_ids": ["ALIAS-SOURCE"]}, relationship_evidence={"detail": "原文明示同一主体", "source_ids": ["ALIAS-SOURCE"]})
        packet["leads"].append(alias)
        packet["coverage"][0]["discovered_lead_ids"].append("TEST-ALIAS")
        return packet

    def test_prepare_canonical_relationships_are_order_independent(self):
        self.duplicate_master()
        for i, reverse in enumerate((False, True)):
            with self.subTest(reverse=reverse):
                packet = self.alias_packet("alias-order-" + str(i))
                if reverse:
                    packet["leads"].reverse()
                core.prepare_validation(packet, core.load_master(self.root)[0])
                result = flow.prepare(self.root, packet, Path(self.temp.name) / ("order-" + str(i)))
                self.assertTrue(result["complete"])
                self.assertEqual(result["accepted_count"], 2)

    def test_prepare_group_survives_unrelated_bad_row_but_bad_alias_cannot_exempt_main(self):
        self.duplicate_master()
        packet = self.alias_packet("group-with-bad")
        bad = copy.deepcopy(packet["leads"][0])
        bad.update(lead_id="BAD-ROW", identity_source_ids=["MISSING"])
        packet["leads"].append(bad)
        result = flow.prepare(self.root, packet, Path(self.temp.name) / "group-with-bad")
        self.assertEqual((result["accepted_count"], result["rejected_count"]), (2, 1))
        packet = self.alias_packet("bad-alias")
        packet["leads"][1]["relationship_evidence"]["source_ids"] = ["MISSING"]
        result = flow.prepare(self.root, packet, Path(self.temp.name) / "bad-alias")
        self.assertEqual(result["accepted_count"], 0)

    def test_notes_only_correction_preserves_qualification_and_history(self):
        path = self.committed_template()
        master, _ = core.load_master(self.root)
        frozen = copy.deepcopy(master["batches"])
        self.edit_workbook(path, {"D2": "员工追加的采集备注"}, shared=True)
        preview = flow.corrections_preview(self.root, path)
        self.assertEqual(preview["changed_count"], 1)
        self.assertFalse(preview["applied"])
        self.assertNotEqual(core.load_master(self.root)[0]["leads"]["TEST-001"]["manual_notes"], "员工追加的采集备注")
        result = flow.corrections_apply(self.root, path, preview["preview_id"])
        saved, _ = core.load_master(self.root)
        self.assertEqual(saved["leads"]["TEST-001"]["manual_notes"], "员工追加的采集备注")
        self.assertEqual(saved["leads"]["TEST-001"]["status"], "main")
        self.assertEqual(saved["batches"], frozen)
        self.assertTrue(Path(result["backup_path"]).exists())
        raw = (self.root / "execution/master.json").read_bytes()
        self.assertTrue(flow.corrections_apply(self.root, path, preview["preview_id"])["already_applied"])
        self.assertEqual(raw, (self.root / "execution/master.json").read_bytes())

    def test_corrections_export_failure_reports_applied_without_rollback(self):
        path = self.committed_template()
        self.edit_workbook(path, {"D2": "已经应用"})
        preview = flow.corrections_preview(self.root, path)
        with mock.patch.object(core, "export_current", side_effect=core.LeadkitError("scope required"), create=True):
            result = flow.corrections_apply(self.root, path, preview["preview_id"], output=Path(self.temp.name) / "current.xlsx")
        self.assertTrue(result["applied"])
        self.assertFalse(result["export_succeeded"])
        self.assertEqual(core.load_master(self.root)[0]["leads"]["TEST-001"]["manual_notes"], "已经应用")

    def test_correction_exports_new_current_state_without_changing_frozen_batch(self):
        path = self.committed_template()
        master, _ = core.load_master(self.root)
        frozen = copy.deepcopy(master["batches"])
        self.edit_workbook(path, {"F2": flow._contact_id(master["leads"]["TEST-001"]["contacts"][0])})
        preview = flow.corrections_preview(self.root, path)
        output = Path(self.temp.name) / "current-corrected.xlsx"
        result = flow.corrections_apply(self.root, path, preview["preview_id"], output=output, batch_id="round-1")
        self.assertTrue(result["export_succeeded"])
        self.assertEqual(result["current_export"]["counts"]["main"], 0)
        self.assertEqual(result["current_export"]["counts"]["candidate"], 1)
        self.assertEqual(core.load_master(self.root)[0]["batches"], frozen)
        self.assertTrue(output.exists())

    def test_invalid_contact_requires_requalification(self):
        path = self.committed_template()
        master, _ = core.load_master(self.root)
        contact_id = flow._contact_id(master["leads"]["TEST-001"]["contacts"][0])
        self.edit_workbook(path, {"F2": contact_id})
        preview = flow.corrections_preview(self.root, path)
        flow.corrections_apply(self.root, path, preview["preview_id"])
        lead = core.load_master(self.root)[0]["leads"]["TEST-001"]
        self.assertEqual(lead["contacts"][0]["status"], "invalid")
        self.assertEqual(lead["status"], "candidate")
        self.assertTrue(lead["issues"])
        self.assertTrue(all(q["status"] == "candidate" for q in lead.get("qualifications", {}).values()))

    def test_unchanged_template_does_not_create_changes(self):
        path = self.committed_template()
        preview = flow.corrections_preview(self.root, path)
        self.assertEqual(preview["changed_count"], 0)

    def test_readonly_and_unknown_contact_rejected(self):
        path = self.committed_template()
        self.edit_workbook(path, {"B2": "改名"})
        with self.assertRaisesRegex(core.LeadkitError, "read-only"):
            flow.corrections_preview(self.root, path)
        self.edit_workbook(path, {"B2": "虚构测试商户", "F2": "UNKNOWN-ID"})
        with self.assertRaisesRegex(core.LeadkitError, "not owned"):
            flow.corrections_preview(self.root, path)

    def test_changed_file_after_preview_rejected(self):
        path = self.committed_template()
        self.edit_workbook(path, {"D2": "首次修改"})
        preview = flow.corrections_preview(self.root, path)
        before = (self.root / "execution/master.json").read_bytes()
        self.edit_workbook(path, {"D2": "再次修改"})
        with self.assertRaisesRegex(core.LeadkitError, "changed after preview"):
            flow.corrections_apply(self.root, path, preview["preview_id"])
        self.assertEqual(before, (self.root / "execution/master.json").read_bytes())

    def test_stale_template_rejected(self):
        path = self.committed_template()
        core.commit(self.root, self.packet("round-2", sid="SRC-002"))
        with self.assertRaisesRegex(core.LeadkitError, "stale"):
            flow.corrections_preview(self.root, path)

    def test_bad_shared_queue_not_silently_dropped(self):
        packet = self.packet()
        packet["queue"] = [{"lead_id": "TEST-001", "next_action": "缺少状态和稳定编号"}]
        report = flow.prepare(self.root, packet, Path(self.temp.name) / "bad-queue")
        self.assertTrue(report["shared_errors"])
        self.assertFalse(report["ready_to_commit"])
        self.assertFalse(Path(self.temp.name, "bad-queue", "prepared.json").exists())
        packet["queue"] = None
        report = flow.prepare(self.root, packet, Path(self.temp.name) / "bad-queue-type")
        self.assertTrue(report["shared_errors"])
        self.assertFalse(report["ready_to_commit"])

    def test_unknown_coverage_and_queue_references_are_not_dropped(self):
        packet = self.packet()
        packet["coverage"][0]["discovered_lead_ids"].append("NEVER-COLLECTED")
        report = flow.prepare(self.root, packet, Path(self.temp.name) / "unknown-coverage")
        self.assertTrue(report["shared_errors"])
        self.assertFalse(report["complete"])
        packet["coverage"][0]["discovered_lead_ids"].remove("NEVER-COLLECTED")
        packet["queue"] = [{"issue_id": "unknown-queue", "lead_id": "NEVER-COLLECTED", "kind": "review", "detail": "引用丢失", "state": "open", "next_action": "修复", "next_trigger": "next_collection", "attempts": 0, "source_ids": []}]
        report = flow.prepare(self.root, packet, Path(self.temp.name) / "unknown-queue")
        self.assertTrue(report["shared_errors"])
        self.assertFalse(report["complete"])

    def test_zero_result_batch_is_committable(self):
        packet = self.packet()
        packet["leads"] = []
        for cell in packet["coverage"]:
            cell.update(status="searched_zero", discovered_lead_ids=[])
        report = flow.prepare(self.root, packet, Path(self.temp.name) / "zero")
        self.assertTrue(report["ready_to_commit"])
        self.assertTrue(report["complete"])
        core.commit(self.root, core.read_json(report["prepared_path"]))
        self.assertEqual(core.load_master(self.root)[0]["batches"][packet["batch_id"]]["counts"]["main"], 0)

    def test_prepare_duplicate_gate_isolated_before_commit(self):
        packet = self.packet()
        another = copy.deepcopy(packet["leads"][0])
        another.update(lead_id="SECOND-ID", name="另一同号实体")
        packet["leads"].append(another)
        for cell in packet["coverage"]:
            cell["discovered_lead_ids"].append("SECOND-ID")
        report = flow.prepare(self.root, packet, Path(self.temp.name) / "gate")
        self.assertEqual((report["accepted_count"], report["rejected_count"]), (1, 1))
        prepared = core.read_json(report["prepared_path"])
        self.assertTrue(any(q["kind"] == "validation" for q in prepared["queue"]))
        core.commit(self.root, prepared)
        batch = core.load_master(self.root)[0]["batches"][packet["batch_id"]]
        self.assertTrue(any("SECOND-ID" in q["detail"] for q in batch["queue"]))

    def test_one_invalid_contact_keeps_other_qualified_entry(self):
        packet = self.packet()
        sid = packet["sources"][0]["source_id"]
        packet["leads"][0]["contacts"].append({"kind": "email", "value": "buyer@example.invalid", "role": "业务邮箱", "attribution": "虚构同页邮箱", "source_ids": [sid]})
        core.commit(self.root, packet)
        path = Path(self.temp.name) / "two-entries.xlsx"
        flow.correction_template(self.root, path)
        lead = core.load_master(self.root)[0]["leads"]["TEST-001"]
        self.edit_workbook(path, {"F2": flow._contact_id(lead["contacts"][0])})
        preview = flow.corrections_preview(self.root, path)
        flow.corrections_apply(self.root, path, preview["preview_id"])
        lead = core.load_master(self.root)[0]["leads"]["TEST-001"]
        self.assertEqual(lead["status"], "main")
        self.assertEqual(lead["contacts"][0]["status"], "invalid")
        self.assertEqual(lead["contacts"][1].get("status", "active"), "active")
        self.assertEqual(lead["issues"][-1]["kind"], "contact_correction")

    def test_formula_rejected(self):
        path = self.committed_template()
        with zipfile.ZipFile(path) as book:
            parts = {name: book.read(name) for name in book.namelist()}
        sheet = ET.fromstring(parts["xl/worksheets/sheet1.xml"])
        cell = next(c for c in sheet.iter(core.tag("c")) if c.get("r") == "D2")
        ET.SubElement(cell, core.tag("f")).text = "1+1"
        parts["xl/worksheets/sheet1.xml"] = core.xml_bytes(sheet)
        with zipfile.ZipFile(path, "w") as book:
            for name, content in parts.items():
                book.writestr(name, content)
        with self.assertRaisesRegex(core.LeadkitError, "Formulas"):
            flow.corrections_preview(self.root, path)


if __name__ == "__main__":
    unittest.main()
