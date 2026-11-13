#!/usr/bin/env python3
"""Synthetic, offline regression checks for research continuity and corrections."""
import copy
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import leadkit as core
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
