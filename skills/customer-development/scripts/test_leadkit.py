#!/usr/bin/env python3
"""Offline behavior checks; all businesses and URLs below are synthetic."""
import copy
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import leadkit as kit


class LeadkitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "workspace"
        kit.init_workspace(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def packet(self, bid="round-1", lid="TEST-001", sid="SRC-001"):
        result = kit.snapshot(self.root, bid)
        packet = kit.read_json(result["batch_path"])
        packet["task"].update(request="仅测试：采集测试国智能眼镜商户", countries=["测试国"], products=["智能眼镜"])
        packet["sources"] = [{"source_id": sid, "url": "https://example.invalid/merchant", "title": "虚构测试原文", "read_at": "2026-11-13T10:00:00+08:00", "level": "current_original", "excerpt": "仅测试。虚构商户经营智能眼镜，测试国公开业务电话 +00123456789。"}]
        packet["leads"] = [{"lead_id": lid, "name": "虚构测试商户", "country": "测试国", "city": "测试城", "address": "仅测试地址", "status": "main", "identity_source_ids": [sid], "location_source_ids": [sid], "product_evidence": [{"product": "智能眼镜", "detail": "虚构测试型号", "source_ids": [sid]}], "contacts": [{"kind": "phone", "value": "+00123456789", "role": "商户公开业务电话", "attribution": "虚构商户原文同页", "source_ids": [sid]}], "reason": "仅用于离线测试", "unknowns": ["采购权限未核"], "manual_notes": "人工初始记录"}]
        return packet

    def test_init_no_overwrite(self):
        original = (self.root / "execution/master.json").read_bytes()
        with self.assertRaises(kit.LeadkitError):
            kit.init_workspace(self.root)
        self.assertEqual(original, (self.root / "execution/master.json").read_bytes())

    def test_commit_is_idempotent_and_stable(self):
        packet = self.packet()
        result = kit.commit(self.root, packet)
        before = (self.root / "execution/master.json").read_bytes()
        again = kit.commit(self.root, packet)
        self.assertFalse(result["already_committed"])
        self.assertTrue(again["already_committed"])
        self.assertEqual(before, (self.root / "execution/master.json").read_bytes())
        changed = copy.deepcopy(packet)
        changed["limitations"].append("不同内容")
        with self.assertRaises(kit.LeadkitError):
            kit.commit(self.root, changed)

    def test_stale_base_conflict(self):
        one = self.packet()
        two = self.packet("round-2", "TEST-002", "SRC-002")
        two["leads"][0]["status"] = "candidate"
        kit.commit(self.root, one)
        with self.assertRaisesRegex(kit.LeadkitError, "Master changed"):
            kit.commit(self.root, two)

    def test_manual_notes_and_prior_products_preserved(self):
        kit.commit(self.root, self.packet())
        packet = self.packet("round-2", sid="SRC-002")
        packet["leads"][0]["manual_notes"] = "AI不得覆盖"
        packet["leads"][0]["product_evidence"].append({"product": "智能手表", "detail": "新观察", "source_ids": ["SRC-002"]})
        kit.commit(self.root, packet)
        master, _ = kit.load_master(self.root)
        lead = master["leads"]["TEST-001"]
        self.assertEqual(lead["manual_notes"], "人工初始记录")
        self.assertEqual(len(lead["history"]), 1)
        self.assertIn("SRC-001", lead["identity_source_ids"])
        self.assertEqual({x["product"] for x in lead["product_evidence"]}, {"智能眼镜", "智能手表"})

    def test_optional_fields_survive_partial_update(self):
        first = self.packet()
        first["leads"][0].update(buyer_type="批发商", website="https://example.invalid", custom_note="保留原始扩展字段")
        kit.commit(self.root, first)
        second = self.packet("round-2", sid="SRC-002")
        second["leads"][0].pop("city")
        second["leads"][0].pop("address")
        second["leads"][0]["reason"] = "本轮新增理由"
        kit.commit(self.root, second)
        master, _ = kit.load_master(self.root)
        lead = master["leads"]["TEST-001"]
        for field in ("city", "address", "buyer_type", "website", "custom_note"):
            self.assertEqual(lead[field], first["leads"][0][field])
        self.assertEqual(lead["reason"], "本轮新增理由")
        self.assertNotIn("history", lead["history"][0])

    def test_backup_is_exact_and_restorable(self):
        packet = self.packet()
        original = (self.root / "execution/master.json").read_bytes()
        result = kit.commit(self.root, packet)
        backup = Path(result["backup_path"])
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(backup.name, kit.digest_bytes(original) + ".json")
        (self.root / "execution/master.json").write_bytes(backup.read_bytes())
        restored, sha = kit.load_master(self.root)
        self.assertEqual(sha, packet["base_sha256"])
        self.assertEqual(restored["leads"], {})
        again = kit.commit(self.root, packet)
        self.assertEqual(Path(again["backup_path"]).read_bytes(), original)
        self.assertEqual(len(list(backup.parent.glob("*.json"))), 1)

    def test_corrupted_existing_backup_blocks_replacement(self):
        packet = self.packet()
        original = (self.root / "execution/master.json").read_bytes()
        directory = self.root / "execution/backups"
        directory.mkdir()
        (directory / (kit.digest_bytes(original) + ".json")).write_bytes(b"invalid backup")
        with self.assertRaisesRegex(kit.LeadkitError, "backup content"):
            kit.commit(self.root, packet)
        self.assertEqual((self.root / "execution/master.json").read_bytes(), original)

    def test_dangling_reference(self):
        packet = self.packet()
        packet["leads"][0]["contacts"][0]["source_ids"] = ["MISSING"]
        with self.assertRaisesRegex(kit.LeadkitError, "missing source"):
            kit.commit(self.root, packet)

    def test_snippet_cannot_be_main(self):
        packet = self.packet()
        packet["sources"][0]["level"] = "search_snippet"
        with self.assertRaisesRegex(kit.LeadkitError, "snippet is insufficient"):
            kit.commit(self.root, packet)
        packet["leads"][0]["status"] = "candidate"
        self.assertEqual(kit.commit(self.root, packet)["counts"]["candidate"], 1)

    def test_whatsapp_requires_explicit_evidence(self):
        packet = self.packet()
        contact = packet["leads"][0]["contacts"][0]
        contact["kind"] = "whatsapp"
        with self.assertRaisesRegex(kit.LeadkitError, "whatsapp_source_ids"):
            kit.commit(self.root, packet)
        contact["whatsapp_source_ids"] = ["SRC-001"]
        contact["whatsapp_evidence"] = "仅虚构测试：原文明示 WhatsApp"
        kit.commit(self.root, packet)

    def test_requested_product_and_country(self):
        packet = self.packet()
        packet["task"]["products"] = ["蓝牙耳机"]
        with self.assertRaisesRegex(kit.LeadkitError, "requested product"):
            kit.commit(self.root, packet)
        packet["task"]["products"] = ["智能眼镜"]
        packet["task"]["countries"] = ["其他国"]
        with self.assertRaisesRegex(kit.LeadkitError, "outside task"):
            kit.commit(self.root, packet)

    def test_explicit_channel_exception(self):
        packet = self.packet()
        packet["leads"][0]["product_evidence"] = []
        with self.assertRaises(kit.LeadkitError):
            kit.commit(self.root, packet)
        packet["task"]["allow_channel_candidates"] = True
        packet["leads"][0]["channel_fit"] = "本次明确接受电子零售新品渠道"
        packet["leads"][0]["channel_source_ids"] = ["SRC-001"]
        kit.commit(self.root, packet)

    def test_new_duplicate_is_not_automatically_merged_or_qualified(self):
        kit.commit(self.root, self.packet())
        packet = self.packet("round-2", "TEST-002", "SRC-002")
        with self.assertRaisesRegex(kit.LeadkitError, "Potential duplicate"):
            kit.commit(self.root, packet)
        packet["leads"][0]["status"] = "candidate"
        receipt = kit.commit(self.root, packet)
        master, _ = kit.load_master(self.root)
        self.assertEqual(len(master["leads"]), 2)
        self.assertTrue(receipt["duplicate_hints"])

    def test_source_ids_immutable(self):
        kit.commit(self.root, self.packet())
        packet = self.packet("round-2")
        packet["sources"][0]["excerpt"] = "changed"
        with self.assertRaisesRegex(kit.LeadkitError, "immutable"):
            kit.commit(self.root, packet)

    def test_invalid_or_historical_contact_does_not_qualify_main(self):
        packet = self.packet()
        for status in ("invalid", "historical"):
            packet["leads"][0]["contacts"][0]["status"] = status
            with self.assertRaisesRegex(kit.LeadkitError, "business entry"):
                kit.commit(self.root, packet)

    def test_contact_correction_keeps_evidence(self):
        kit.commit(self.root, self.packet())
        packet = self.packet("round-2", sid="SRC-002")
        packet["leads"][0]["status"] = "candidate"
        packet["leads"][0]["contacts"][0]["status"] = "invalid"
        kit.commit(self.root, packet)
        master, _ = kit.load_master(self.root)
        contacts = master["leads"]["TEST-001"]["contacts"]
        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["status"], "invalid")
        self.assertEqual(contacts[0]["source_ids"], ["SRC-001", "SRC-002"])

    def test_lock_prevents_writes(self):
        packet = self.packet()
        with kit.locked(self.root):
            with self.assertRaisesRegex(kit.LeadkitError, "locked"):
                kit.commit(self.root, packet)

    def test_export_xml_and_text_numbers_no_formula_execution(self):
        packet = self.packet()
        packet["leads"][0]["name"] = '=HYPERLINK("https://example.invalid")'
        kit.commit(self.root, packet)
        output = self.root / "outputs/test.xlsx"
        kit.export_batch(self.root, "round-1", output)
        with zipfile.ZipFile(output) as archive:
            self.assertIsNone(archive.testzip())
            book = ET.fromstring(archive.read("xl/workbook.xml"))
            names = [node.attrib["name"] for node in book.find(kit.tag("sheets"))]
            self.assertEqual(names, ["可开发客户", "待核线索", "来源证据", "采集说明"])
            sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            self.assertIsNotNone(sheet.find(kit.tag("autoFilter")))
            self.assertFalse(sheet.findall(".//" + kit.tag("f")))
            values = [node.text for node in sheet.findall(".//" + kit.tag("t"))]
            self.assertIn(packet["leads"][0]["name"], values)
            self.assertTrue(any("+00123456789" in (v or "") for v in values))
        with self.assertRaises(kit.LeadkitError):
            kit.export_batch(self.root, "round-1", output)

    def test_export_uses_batch_snapshot(self):
        kit.commit(self.root, self.packet())
        packet = self.packet("round-2", sid="SRC-002")
        packet["leads"][0]["reason"] = "第二轮新理由"
        kit.commit(self.root, packet)
        output = self.root / "outputs/first.xlsx"
        kit.export_batch(self.root, "round-1", output)
        with zipfile.ZipFile(output) as archive:
            self.assertNotIn("第二轮新理由", archive.read("xl/worksheets/sheet1.xml").decode())

    def test_long_excel_text_refused_not_truncated(self):
        output = self.root / "outputs/long.xlsx"
        with self.assertRaisesRegex(kit.LeadkitError, "32767"):
            kit.write_xlsx(output, [("测试", [["A"], ["x" * 32768]], [20])])
        self.assertFalse(output.exists())

    def test_batch_id_traversal_rejected(self):
        with self.assertRaises(kit.LeadkitError):
            kit.snapshot(self.root, "../escape")


if __name__ == "__main__":
    unittest.main()
