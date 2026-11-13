#!/usr/bin/env python3
"""Offline behavior checks; all businesses and URLs below are synthetic."""
import copy
import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
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
        packet["sources"].append({"source_id": "REG-" + sid, "url": "https://example.invalid/region", "title": "虚构地区研究", "read_at": "2026-11-13T10:00:00+08:00", "level": "current_original", "excerpt": "虚构测试国使用测试语言，有商户目录；全部虚构。"})
        packet["region_research"] = [{"country": "测试国", "area": "测试城", "status": "partial", "checked_at": "2026-11-13T10:00:00+08:00", "languages": ["测试语言"], "markets": [], "communication_channels": [], "ecommerce_platforms": [], "source_ids": ["REG-" + sid], "limitations": ["虚构案例未研究市场和平台"]}]
        packet["leads"][0].update(buyer_type="批发商", buyer_type_source_ids=[sid], brand_profile="white_label", white_label_evidence=[{"product": "智能眼镜", "detail": "虚构来源明确无品牌通用款", "source_ids": [sid]}])
        packet["leads"][0]["discovered_by"] = [{"channel": "synthetic_directory", "source_ids": [sid]}]
        return packet

    def commit(self, packet):
        packet["coverage"] = []
        for product in packet["task"]["products"]:
            qualified = any(x["status"] == "main" and any(p["product"] == product for p in x.get("product_evidence", [])) and any(p["product"] == product for p in x.get("white_label_evidence", [])) for x in packet["leads"])
            packet["coverage"].append({"product": product, "channel": "synthetic_directory", "status": "searched" if qualified else "searched_zero", "attempts": 1, "source_ids": [packet["sources"][0]["source_id"]], "limitations": [], "discovered_lead_ids": [x["lead_id"] for x in packet["leads"]]})
        return kit.commit(self.root, packet)

    def test_init_no_overwrite(self):
        original = (self.root / "execution/master.json").read_bytes()
        with self.assertRaises(kit.LeadkitError):
            kit.init_workspace(self.root)
        self.assertEqual(original, (self.root / "execution/master.json").read_bytes())

    def test_commit_is_idempotent_and_stable(self):
        packet = self.packet()
        result = self.commit(packet)
        before = (self.root / "execution/master.json").read_bytes()
        again = self.commit(packet)
        self.assertFalse(result["already_committed"])
        self.assertTrue(again["already_committed"])
        self.assertEqual(before, (self.root / "execution/master.json").read_bytes())
        changed = copy.deepcopy(packet)
        changed["limitations"].append("不同内容")
        with self.assertRaises(kit.LeadkitError):
            self.commit(changed)

    def test_stale_base_conflict(self):
        one = self.packet()
        two = self.packet("round-2", "TEST-002", "SRC-002")
        two["leads"][0]["status"] = "candidate"
        self.commit(one)
        with self.assertRaisesRegex(kit.LeadkitError, "Master changed"):
            self.commit(two)

    def test_manual_notes_and_prior_products_preserved(self):
        self.commit(self.packet())
        packet = self.packet("round-2", sid="SRC-002")
        packet["leads"][0]["manual_notes"] = "AI不得覆盖"
        packet["leads"][0]["product_evidence"].append({"product": "智能手表", "detail": "新观察", "source_ids": ["SRC-002"]})
        self.commit(packet)
        master, _ = kit.load_master(self.root)
        lead = master["leads"]["TEST-001"]
        self.assertEqual(lead["manual_notes"], "人工初始记录")
        self.assertEqual(len(lead["history"]), 1)
        self.assertIn("SRC-001", lead["identity_source_ids"])
        self.assertEqual({x["product"] for x in lead["product_evidence"]}, {"智能眼镜", "智能手表"})

    def test_optional_fields_survive_partial_update(self):
        first = self.packet()
        first["leads"][0].update(buyer_type="批发商", website="https://example.invalid", custom_note="保留原始扩展字段")
        self.commit(first)
        second = self.packet("round-2", sid="SRC-002")
        second["leads"][0].pop("city")
        second["leads"][0].pop("address")
        second["leads"][0]["reason"] = "本轮新增理由"
        self.commit(second)
        master, _ = kit.load_master(self.root)
        lead = master["leads"]["TEST-001"]
        for field in ("city", "address", "buyer_type", "website", "custom_note"):
            self.assertEqual(lead[field], first["leads"][0][field])
        self.assertEqual(lead["reason"], "本轮新增理由")
        self.assertNotIn("history", lead["history"][0])

    def test_backup_is_exact_and_restorable(self):
        packet = self.packet()
        original = (self.root / "execution/master.json").read_bytes()
        result = self.commit(packet)
        backup = Path(result["backup_path"])
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(backup.name, kit.digest_bytes(original) + ".json")
        (self.root / "execution/master.json").write_bytes(backup.read_bytes())
        restored, sha = kit.load_master(self.root)
        self.assertEqual(sha, packet["base_sha256"])
        self.assertEqual(restored["leads"], {})
        again = self.commit(packet)
        self.assertEqual(Path(again["backup_path"]).read_bytes(), original)
        self.assertEqual(len(list(backup.parent.glob("*.json"))), 1)

    def test_corrupted_existing_backup_blocks_replacement(self):
        packet = self.packet()
        original = (self.root / "execution/master.json").read_bytes()
        directory = self.root / "execution/backups"
        directory.mkdir()
        (directory / (kit.digest_bytes(original) + ".json")).write_bytes(b"invalid backup")
        with self.assertRaisesRegex(kit.LeadkitError, "backup content"):
            self.commit(packet)
        self.assertEqual((self.root / "execution/master.json").read_bytes(), original)

    def test_dangling_reference(self):
        packet = self.packet()
        packet["leads"][0]["contacts"][0]["source_ids"] = ["MISSING"]
        with self.assertRaisesRegex(kit.LeadkitError, "missing source"):
            self.commit(packet)

    def test_snippet_cannot_be_main(self):
        packet = self.packet()
        packet["sources"][0]["level"] = "search_snippet"
        with self.assertRaisesRegex(kit.LeadkitError, "snippet is insufficient"):
            self.commit(packet)
        packet["leads"][0]["status"] = "candidate"
        self.assertEqual(self.commit(packet)["counts"]["candidate"], 1)

    def test_whatsapp_requires_explicit_evidence(self):
        packet = self.packet()
        contact = packet["leads"][0]["contacts"][0]
        contact["kind"] = "whatsapp"
        with self.assertRaisesRegex(kit.LeadkitError, "whatsapp_source_ids"):
            self.commit(packet)
        contact["whatsapp_source_ids"] = ["SRC-001"]
        contact["whatsapp_evidence"] = "仅虚构测试：原文明示 WhatsApp"
        contact.update(action="message", action_evidence="虚构页面具有WA联系链接", action_source_ids=["SRC-001"])
        self.commit(packet)

    def test_requested_product_and_country(self):
        packet = self.packet()
        packet["task"]["products"] = ["蓝牙耳机"]
        with self.assertRaisesRegex(kit.LeadkitError, "requested product"):
            self.commit(packet)
        packet["task"]["products"] = ["智能眼镜"]
        packet["task"]["countries"] = ["其他国"]
        with self.assertRaisesRegex(kit.LeadkitError, "outside task"):
            self.commit(packet)

    def test_explicit_channel_exception(self):
        packet = self.packet()
        packet["leads"][0]["product_evidence"] = []
        with self.assertRaises(kit.LeadkitError):
            self.commit(packet)
        packet["task"]["allow_channel_candidates"] = True
        packet["leads"][0]["channel_fit"] = "本次明确接受电子零售新品渠道"
        packet["leads"][0]["channel_source_ids"] = ["SRC-001"]
        with self.assertRaisesRegex(kit.LeadkitError, "channel candidates"):
            self.commit(packet)
        packet["leads"][0]["status"] = "candidate"
        self.commit(packet)

    def test_new_duplicate_is_not_automatically_merged_or_qualified(self):
        self.commit(self.packet())
        packet = self.packet("round-2", "TEST-002", "SRC-002")
        with self.assertRaisesRegex(kit.LeadkitError, "Potential duplicate"):
            self.commit(packet)
        packet["leads"][0]["status"] = "candidate"
        receipt = self.commit(packet)
        master, _ = kit.load_master(self.root)
        self.assertEqual(len(master["leads"]), 2)
        self.assertTrue(receipt["duplicate_hints"])

    def test_source_ids_immutable(self):
        self.commit(self.packet())
        packet = self.packet("round-2")
        packet["sources"][0]["excerpt"] = "changed"
        with self.assertRaisesRegex(kit.LeadkitError, "immutable"):
            self.commit(packet)

    def test_invalid_or_historical_contact_does_not_qualify_main(self):
        packet = self.packet()
        for status in ("invalid", "historical"):
            packet["leads"][0]["contacts"][0]["status"] = status
            with self.assertRaisesRegex(kit.LeadkitError, "business entry"):
                self.commit(packet)

    def test_contact_correction_keeps_evidence(self):
        self.commit(self.packet())
        packet = self.packet("round-2", sid="SRC-002")
        packet["leads"][0]["status"] = "candidate"
        packet["leads"][0]["contacts"][0]["status"] = "invalid"
        self.commit(packet)
        master, _ = kit.load_master(self.root)
        contacts = master["leads"]["TEST-001"]["contacts"]
        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["status"], "invalid")
        self.assertEqual(contacts[0]["source_ids"], ["SRC-001", "SRC-002"])

    def test_lock_prevents_writes(self):
        packet = self.packet()
        with kit.locked(self.root):
            with self.assertRaisesRegex(kit.LeadkitError, "locked"):
                self.commit(packet)

    def test_export_xml_and_text_numbers_no_formula_execution(self):
        packet = self.packet()
        packet["leads"][0]["name"] = '=HYPERLINK("https://example.invalid")'
        self.commit(packet)
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
        self.commit(self.packet())
        packet = self.packet("round-2", sid="SRC-002")
        packet["leads"][0]["reason"] = "第二轮新理由"
        self.commit(packet)
        output = self.root / "outputs/first.xlsx"
        kit.export_batch(self.root, "round-1", output)
        with zipfile.ZipFile(output) as archive:
            self.assertNotIn("第二轮新理由", archive.read("xl/worksheets/sheet1.xml").decode())

    def test_long_excel_text_refused_not_truncated(self):
        output = self.root / "outputs/long.xlsx"
        with self.assertRaisesRegex(kit.LeadkitError, "32767"):
            kit.write_xlsx(output, [("测试", [["A"], ["x" * 32768]], [20])])
        self.assertFalse(output.exists())

    def test_mixed_shop_needs_white_label_evidence(self):
        packet = self.packet()
        lead = packet["leads"][0]
        lead["brand_profile"] = "mixed"
        lead["white_label_evidence"] = []
        with self.assertRaisesRegex(kit.LeadkitError, "white_label_evidence"):
            self.commit(packet)
        lead["white_label_evidence"] = [{"product": "智能眼镜", "detail": "具体无品牌通用款", "source_ids": ["SRC-001"]}]
        self.assertEqual(self.commit(packet)["counts"]["main"], 1)

    def test_white_evidence_must_match_same_requested_product(self):
        packet = self.packet()
        packet["task"]["products"] = ["智能眼镜", "智能手表"]
        packet["leads"][0]["white_label_evidence"][0]["product"] = "智能手表"
        with self.assertRaisesRegex(kit.LeadkitError, "same requested product"):
            self.commit(packet)

    def test_brand_entities_are_excluded_and_unknown_stays_candidate(self):
        packet = self.packet()
        for kind in ("brand_owner", "official_store", "brand_exclusive"):
            packet["leads"][0]["brand_profile"] = kind
            for status in ("main", "candidate"):
                packet["leads"][0]["status"] = status
                with self.assertRaisesRegex(kit.LeadkitError, "must be excluded"):
                    self.commit(packet)
        packet["leads"][0].update(brand_profile="unknown", status="main")
        with self.assertRaisesRegex(kit.LeadkitError, "brand_profile"):
            self.commit(packet)
        packet["leads"][0]["status"] = "candidate"
        self.assertEqual(self.commit(packet)["counts"]["candidate"], 1)

    def test_bare_website_and_display_only_social_do_not_qualify(self):
        packet = self.packet()
        contact = packet["leads"][0]["contacts"][0]
        for kind in ("website", "facebook", "instagram", "zalo", "chat", "form"):
            contact.update(kind=kind, value="https://example.invalid/shop")
            with self.assertRaisesRegex(kit.LeadkitError, "actionable"):
                self.commit(packet)
        contact.update(kind="form", action="form", action_evidence="实际询价表单", action_source_ids=["SRC-001"])
        self.assertEqual(self.commit(packet)["counts"]["main"], 1)

    def test_website_action_flag_cannot_bypass_contact_gate(self):
        packet = self.packet()
        packet["leads"][0]["contacts"][0].update(kind="website", value="https://example.invalid", action="message", action_evidence="凭空写明有联系", action_source_ids=["SRC-001"])
        with self.assertRaisesRegex(kit.LeadkitError, "website cannot become"):
            self.commit(packet)

    def test_action_requires_readable_evidence_and_matching_kind(self):
        packet = self.packet()
        packet["sources"].append(dict(packet["sources"][0], source_id="SNIPPET", level="search_snippet"))
        contact = packet["leads"][0]["contacts"][0]
        contact.update(kind="zalo", action="form", action_evidence="Zalo入口", action_source_ids=["SRC-001"])
        with self.assertRaisesRegex(kit.LeadkitError, "does not match"):
            self.commit(packet)
        contact.update(action="message", action_source_ids=["SNIPPET"])
        with self.assertRaisesRegex(kit.LeadkitError, "actionable"):
            self.commit(packet)
        contact["action_source_ids"] = ["SRC-001"]
        self.commit(packet)

    def test_region_and_buyer_profile_required(self):
        packet = self.packet()
        packet["region_research"] = []
        with self.assertRaisesRegex(kit.LeadkitError, "region_research"):
            self.commit(packet)
        packet["region_research"] = [{"country": "其他国", "area": "其他城", "status": "blocked", "checked_at": "2026-11-13T10:00:00+08:00", "languages": [], "markets": [], "communication_channels": [], "ecommerce_platforms": [], "source_ids": [], "limitations": ["无法联网"]}]
        with self.assertRaisesRegex(kit.LeadkitError, "non-blocked"):
            self.commit(packet)
        packet["leads"] = []
        self.assertEqual(self.commit(packet)["counts"]["main"], 0)

    def test_unknown_market_size_allowed_but_large_needs_evidence(self):
        packet = self.packet()
        market = {"name": "虚构市场", "kind": "电子市场", "size": "large", "source_ids": ["SRC-001"]}
        packet["region_research"][0]["markets"] = [market]
        with self.assertRaisesRegex(kit.LeadkitError, "size_source_ids"):
            self.commit(packet)
        market["size"] = "unknown"
        self.commit(packet)

    def test_main_buyer_type_must_have_matching_profile_and_evidence(self):
        packet = self.packet()
        packet["task"]["customer_profile"]["buyer_types"] = ["进口商"]
        with self.assertRaisesRegex(kit.LeadkitError, "buyer_type"):
            self.commit(packet)
        packet["task"]["customer_profile"]["buyer_types"] = ["批发商"]
        packet["leads"][0]["buyer_type_source_ids"] = []
        with self.assertRaisesRegex(kit.LeadkitError, "buyer_type_source_ids"):
            self.commit(packet)

    def test_priority_is_derived_and_does_not_trust_claimed_a(self):
        packet = self.packet()
        packet["leads"][0].update(priority="A", priority_reason="伪造高分")
        self.commit(packet)
        lead = kit.load_master(self.root)[0]["leads"]["TEST-001"]
        self.assertEqual(lead["priority"], "B")
        self.assertNotIn("伪造", lead["priority_reason"])

    def test_priority_a_and_export_order_and_downgrade(self):
        packet = self.packet()
        high = copy.deepcopy(packet["leads"][0])
        high.update(lead_id="TEST-002", name="第二虚构商户", capability_evidence=[{"kind": "wholesale", "detail": "有批发业务", "source_ids": ["SRC-001"]}], ongoing_activity={"detail": "本轮实际持续上新", "source_ids": ["SRC-001"], "observed_on": datetime.now(timezone.utc).date().isoformat()})
        high["contacts"][0]["value"] = "+00123456780"
        packet["leads"].append(high)
        self.commit(packet)
        output = self.root / "outputs/priority.xlsx"
        kit.export_batch(self.root, "round-1", output)
        with zipfile.ZipFile(output) as z:
            sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
            rows = sheet.find(kit.tag("sheetData"))
            self.assertEqual(rows[1][0].find('.//' + kit.tag('t')).text, "TEST-002")
            sources = z.read("xl/worksheets/sheet3.xml").decode()
            self.assertIn("地区预研", sources)
        second = self.packet("round-2", lid="TEST-002", sid="SRC-002")
        second["leads"][0].update(name="第二虚构商户", status="candidate")
        self.commit(second)
        saved = kit.load_master(self.root)[0]["leads"]["TEST-002"]
        self.assertEqual(saved["priority"], "")
        self.assertEqual(saved["history"][-1]["priority"], "A")

    def test_cached_activity_cannot_make_priority_a(self):
        packet = self.packet()
        packet["sources"].append(dict(packet["sources"][0], source_id="OLD", level="cached_original", limitations="历史缓存"))
        packet["leads"][0].update(capability_evidence=[{"kind": "distribution", "detail": "有分销业务", "source_ids": ["SRC-001"]}], ongoing_activity={"detail": "旧活动", "source_ids": ["OLD"]})
        self.commit(packet)
        self.assertEqual(kit.load_master(self.root)[0]["leads"]["TEST-001"]["priority"], "B")

    def test_legacy_schema_new_commit_rejected_old_batch_exportable(self):
        packet = self.packet()
        packet["schema_version"] = 1
        with self.assertRaisesRegex(kit.LeadkitError, "schema_version 3"):
            self.commit(packet)
        packet["schema_version"] = 3
        self.commit(packet)
        master, _ = kit.load_master(self.root)
        batch = master["batches"]["round-1"]
        for key in ("schema_version", "rules_version", "region_research"):
            batch.pop(key)
        for lead in batch["lead_snapshots"]:
            for key in ("brand_profile", "white_label_evidence", "priority", "priority_reason"):
                lead.pop(key, None)
        (self.root / "execution/master.json").write_bytes(kit.encoded(master))
        before = (self.root / "execution/master.json").read_bytes()
        output = self.root / "outputs/legacy.xlsx"
        kit.export_batch(self.root, "round-1", output)
        self.assertEqual(before, (self.root / "execution/master.json").read_bytes())
        with zipfile.ZipFile(output) as z:
            self.assertIn("旧版未评级", z.read("xl/worksheets/sheet1.xml").decode())

    def test_null_activity_rejected_without_crashing(self):
        packet = self.packet()
        packet["leads"][0]["ongoing_activity"] = None
        with self.assertRaisesRegex(kit.LeadkitError, "ongoing_activity must be an object"):
            self.commit(packet)

    def test_large_regional_research_exports_as_human_rows(self):
        packet = self.packet()
        packet["region_research"][0]["markets"] = [{"name": "虚构市场%d-" % i + "测试" * 50, "kind": "电子批发市场", "size": "unknown", "source_ids": ["SRC-001"]} for i in range(400)]
        packet["leads"][0]["ongoing_activity"] = {"detail": "虚构经营观察", "observed_on": "2026-11-13", "source_ids": ["SRC-001"]}
        self.commit(packet)
        output = self.root / "outputs/large-region.xlsx"
        kit.export_batch(self.root, "round-1", output)
        with zipfile.ZipFile(output) as z:
            notes = ET.fromstring(z.read("xl/worksheets/sheet4.xml"))
            self.assertGreater(len(notes.find(kit.tag("sheetData"))), 400)
            texts = [n.text or "" for n in notes.findall('.//' + kit.tag('t'))]
            self.assertFalse(any('"source_ids"' in x or '"brand_policy"' in x for x in texts))
            self.assertIn("日期：2026-11-13", z.read("xl/worksheets/sheet1.xml").decode())

    def test_omitted_brand_cannot_reopen_known_exclusion(self):
        initial = self.packet()
        initial["leads"][0].update(brand_profile="brand_owner", status="excluded", exclusion={"code":"brand_owner", "origin":"evidence", "detail":"明确为品牌所有方", "source_ids":["SRC-001"]})
        self.commit(initial)
        packet = self.packet("round-2", sid="SRC-002")
        packet["leads"][0].pop("brand_profile")
        packet["leads"][0]["status"] = "candidate"
        before = (self.root / "execution/master.json").read_bytes()
        with self.assertRaisesRegex(kit.LeadkitError, "Merged brand profile is excluded"):
            self.commit(packet)
        self.assertEqual(before, (self.root / "execution/master.json").read_bytes())
        packet["leads"][0].update(brand_profile="mixed", status="main", reason="本轮原文纠正此前品牌方误判；独立混合经营且有白牌商品")
        self.commit(packet)
        saved = kit.load_master(self.root)[0]["leads"]["TEST-001"]
        self.assertEqual(saved["brand_profile"], "mixed")
        self.assertEqual(saved["status"], "main")
        self.assertEqual(saved["history"][-1]["brand_profile"], "brand_owner")

    def test_exclusion_requires_evidence_or_explicit_user_instruction(self):
        packet = self.packet()
        lead = packet["leads"][0]
        lead.update(status="excluded", brand_profile="brand_owner")
        with self.assertRaisesRegex(kit.LeadkitError, "excluded needs"):
            self.commit(packet)
        lead["exclusion"] = {"code": "brand_owner", "origin": "evidence", "detail": "仅凭名字猜测", "source_ids": []}
        with self.assertRaisesRegex(kit.LeadkitError, "must contain evidence"):
            self.commit(packet)
        lead["exclusion"] = {"code": "user_excluded", "origin": "user", "detail": "用户不开发此客户", "user_instruction": "明确排除TEST-001"}
        self.assertEqual(self.commit(packet)["counts"]["excluded"], 1)

    def test_platform_store_domains_do_not_merge_independent_sellers(self):
        first = self.packet()
        first["leads"][0]["contacts"].append({"kind": "website", "value": "https://shopee.vn/store-alpha", "role": "店铺", "attribution": "独立店铺", "source_ids": ["SRC-001"], "identity_scope": "platform_store", "platform": "Shopee"})
        self.commit(first)
        second = self.packet("round-2", "TEST-002", "SRC-002")
        second["leads"][0].update(name="另一独立店铺")
        second["leads"][0]["contacts"][0]["value"] = "+00123456780"
        second["leads"][0]["contacts"].append(dict(first["leads"][0]["contacts"][-1], value="https://shopee.vn/store-beta", source_ids=["SRC-002"]))
        result = self.commit(second)
        self.assertEqual(result["counts"]["main"], 1)
        self.assertEqual(result["duplicate_hints"], [])

    def test_same_name_only_is_a_nonblocking_hint(self):
        self.commit(self.packet())
        second = self.packet("round-2", "TEST-002", "SRC-002")
        second["leads"][0]["contacts"][0]["value"] = "+00123456780"
        result = self.commit(second)
        self.assertTrue(all(x["strength"] == "weak" for x in result["duplicate_hints"]))

    def test_confirmed_duplicate_alias_does_not_block_canonical_update(self):
        self.commit(self.packet())
        alias = self.packet("round-2", "TEST-ALIAS", "SRC-002")
        alias["leads"][0].update(status="excluded", canonical_id="TEST-001", exclusion={"code": "duplicate", "origin": "evidence", "detail": "同一公司旧名称", "source_ids": ["SRC-002"]}, relationship_evidence={"detail": "原文明示旧名", "source_ids": ["SRC-002"]})
        self.commit(alias)
        result = self.commit(self.packet("round-3", sid="SRC-003"))
        self.assertEqual(result["counts"]["updated_entities"], 1)
        self.assertEqual(result["duplicate_hints"], [])

    def test_city_market_scope_and_applicable_preflight(self):
        packet = self.packet()
        packet["task"].update(city="目标城", market="目标市场")
        packet["leads"][0]["market"] = "其他市场"
        with self.assertRaisesRegex(kit.LeadkitError, "geo_scope"):
            self.commit(packet)
        packet["leads"][0].update(city="目标城", market="目标市场")
        with self.assertRaisesRegex(kit.LeadkitError, "region_research applicable"):
            self.commit(packet)
        packet["region_research"][0]["area"] = "目标城"
        self.assertEqual(self.commit(packet)["counts"]["main"], 1)

    def test_product_task_mismatch_does_not_erase_prior_qualification(self):
        self.commit(self.packet())
        packet = self.packet("round-2", sid="SRC-002")
        packet["task"]["products"] = ["蓝牙耳机"]
        packet["leads"][0].update(status="excluded", exclusion={"code": "product_mismatch", "origin": "evidence", "detail": "本次原文不经营耳机", "source_ids": ["SRC-002"]})
        self.commit(packet)
        master, _ = kit.load_master(self.root)
        lead = master["leads"]["TEST-001"]
        self.assertEqual(lead["status"], "main")
        self.assertEqual(lead["last_task_status"], "excluded")
        self.assertEqual({x["status"] for x in lead["qualifications"].values()}, {"main", "excluded"})
        self.assertEqual(master["batches"]["round-2"]["lead_snapshots"][0]["status"], "excluded")

    def test_contact_format_change_invalidates_same_entry_and_preserves_variants(self):
        self.commit(self.packet())
        packet = self.packet("round-2", sid="SRC-002")
        packet["leads"][0]["status"] = "candidate"
        packet["leads"][0]["contacts"][0].update(value="+00 1234 56789", status="invalid")
        self.commit(packet)
        contacts = kit.load_master(self.root)[0]["leads"]["TEST-001"]["contacts"]
        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["status"], "invalid")
        self.assertEqual(contacts[0]["value_variants"], ["+00123456789", "+00 1234 56789"])

    def test_inherited_contact_is_not_labelled_current_verification(self):
        self.commit(self.packet())
        second = self.packet("round-2", sid="SRC-002")
        second["leads"][0]["contacts"][0]["source_ids"] = ["SRC-001"]
        self.commit(second)
        lead = kit.load_master(self.root)[0]["leads"]["TEST-001"]
        self.assertIn("历史沿用未复核", kit.contact_display(lead["contacts"][0], "round-2"))

    def test_stale_activity_on_current_page_cannot_get_a(self):
        packet = self.packet()
        packet["leads"][0].update(capability_evidence=[{"kind":"wholesale", "detail":"批发能力", "source_ids":["SRC-001"]}], ongoing_activity={"detail":"2019年的旧促销", "observed_on":"2019-01-01", "source_ids":["SRC-001"]})
        self.commit(packet)
        self.assertEqual(kit.load_master(self.root)[0]["leads"]["TEST-001"]["priority"], "B")

    def test_resolved_unknown_not_reopened_or_exported(self):
        first = self.packet()
        first["leads"][0]["status"] = "candidate"
        self.commit(first)
        issue = kit.load_master(self.root)[0]["leads"]["TEST-001"]["issues"][0]
        second = self.packet("round-2", sid="SRC-002")
        second["leads"][0]["issues"] = [dict(issue, state="resolved", source_ids=["SRC-002"], resolved_at=kit.now(), attempts=1)]
        self.commit(second)
        third = self.packet("round-3", sid="SRC-003")
        third["leads"][0]["unknowns"] = []
        self.commit(third)
        lead = kit.load_master(self.root)[0]["leads"]["TEST-001"]
        self.assertEqual(lead["unknowns"], [])
        self.assertEqual(lead["issues"][0]["state"], "resolved")
        self.assertGreater(len(lead["history"]), 0)

    def test_counts_distinguish_new_update_and_requalified(self):
        first = self.packet()
        first["leads"][0]["status"] = "candidate"
        a = self.commit(first)["counts"]
        b = self.commit(self.packet("round-2", sid="SRC-002"))["counts"]
        self.assertEqual((a["new_entities"], a["updated_entities"]), (1, 0))
        self.assertEqual((b["new_entities"], b["updated_entities"], b["requalified"], b["new_contacts"]), (0, 1, 1, 0))

    def test_supply_conflict_and_required_checks_prevent_main(self):
        packet = self.packet()
        packet["task"]["requirements"] = ["必须有批发业务"]
        with self.assertRaisesRegex(kit.LeadkitError, "requirement_checks"):
            self.commit(packet)
        packet["leads"][0]["requirement_checks"] = [{"requirement": "必须有批发业务", "status": "matched", "detail":"原文明示", "source_ids":["SRC-001"]}]
        packet["task"]["customer_profile"]["supplier_constraints"] = {"MOQ":"100"}
        packet["leads"][0]["supplier_fit"] = [{"constraint":"MOQ", "status":"conflict", "detail":"客户明确只要一件", "source_ids":["SRC-001"]}]
        with self.assertRaisesRegex(kit.LeadkitError, "supplier_fit"):
            self.commit(packet)
        packet["leads"][0]["supplier_fit"][0].update(status="unknown", detail="客户采购量未知", source_ids=[])
        self.commit(packet)

    def test_requested_product_coverage_must_include_unsearched_classes(self):
        packet = self.packet()
        self.commit(packet)
        packet = self.packet("round-2", sid="SRC-002")
        packet["task"]["products"] = ["智能眼镜", "智能戒指"]
        packet["coverage"] = [{"product":"智能眼镜", "channel":"synthetic_directory", "status":"searched", "attempts":1, "source_ids":["SRC-002"], "limitations":[], "discovered_lead_ids":["TEST-001"]}]
        with self.assertRaisesRegex(kit.LeadkitError, "every requested product"):
            kit.commit(self.root, packet)
        packet["coverage"].append({"product":"智能戒指", "channel":"synthetic_directory", "status":"unsearched", "attempts":0, "source_ids":[], "limitations":["本轮尚未开始"], "discovered_lead_ids":[]})
        kit.commit(self.root, packet)
        batch = kit.load_master(self.root)[0]["batches"]["round-2"]
        self.assertEqual(batch["coverage"][1]["status"], "unsearched")
        self.assertEqual(batch["lead_snapshots"][0]["qualified_products"], ["智能眼镜"])

    def test_scope_key_ignores_request_wording_and_target_count(self):
        packet = self.packet()
        original = kit.scope_key(packet["task"])
        packet["task"].update(request="换了说法", target_count=200)
        self.assertEqual(original, kit.scope_key(packet["task"]))
        packet["task"]["city"] = "另一城"
        self.assertNotEqual(original, kit.scope_key(packet["task"]))

    def test_invalid_contact_cannot_reactivate_from_old_source(self):
        self.commit(self.packet())
        packet = self.packet("round-2", sid="SRC-002")
        packet["leads"][0]["status"] = "candidate"
        packet["leads"][0]["contacts"][0]["status"] = "invalid"
        self.commit(packet)
        third = self.packet("round-3", sid="SRC-003")
        third["leads"][0]["contacts"][0]["source_ids"] = ["SRC-001"]
        with self.assertRaisesRegex(kit.LeadkitError, "Reactivating invalid"):
            self.commit(third)

    def test_legacy_v2_commit_receipt_stays_idempotent(self):
        packet = self.packet()
        self.commit(packet)
        master, _ = kit.load_master(self.root)
        packet["schema_version"] = 2
        master["batches"]["round-1"]["schema_version"] = 2
        master["batches"]["round-1"]["packet_sha256"] = kit.digest_bytes(kit.encoded(packet))
        (self.root / "execution/master.json").write_bytes(kit.encoded(master))
        before = (self.root / "execution/master.json").read_bytes()
        self.assertTrue(kit.commit(self.root, packet)["already_committed"])
        self.assertEqual(before, (self.root / "execution/master.json").read_bytes())

    def test_reactivated_contact_defaults_agree_with_main_result(self):
        self.commit(self.packet())
        second = self.packet("round-2", sid="SRC-002")
        second["leads"][0]["status"] = "candidate"
        second["leads"][0]["contacts"][0]["status"] = "invalid"
        self.commit(second)
        third = self.packet("round-3", sid="SRC-003")
        third["sources"][0]["read_at"] = kit.now()
        self.commit(third)
        lead = kit.load_master(self.root)[0]["leads"]["TEST-001"]
        self.assertEqual(lead["contacts"][0]["status"], "active")
        self.assertEqual(lead["status"], "main")

    def test_optional_evidence_objects_cannot_hide_dangling_references(self):
        packet = self.packet()
        packet["leads"][0]["status"] = "candidate"
        for field in ("location_match", "relationship_evidence", "exclusion", "duplicate_review"):
            packet["leads"][0][field] = {"source_ids":["MISSING"]}
            with self.assertRaisesRegex(kit.LeadkitError, "missing source"):
                self.commit(packet)
            packet["leads"][0].pop(field)

    def test_queue_attempts_cannot_go_backwards(self):
        packet = self.packet()
        issue = {"issue_id":"ISSUE-1", "kind":"research", "detail":"待补充", "state":"open", "next_action":"补证", "attempts":2, "next_trigger":"next_collection", "source_ids":[]}
        packet["queue"] = [issue]
        self.commit(packet)
        second = self.packet("round-2", sid="SRC-002")
        second["queue"] = [dict(issue,attempts=1)]
        with self.assertRaisesRegex(kit.LeadkitError, "cannot decrease"):
            self.commit(second)

    def test_employee_correction_needs_explicit_evidence_resolution(self):
        first = self.packet()
        first["leads"][0]["status"] = "candidate"
        issue = {"issue_id":"EMP-1", "kind":"employee_correction", "detail":"员工要求复核白牌经营", "state":"open", "next_action":"补证", "attempts":0, "next_trigger":"next_collection", "source_ids":[]}
        first["leads"][0]["issues"] = [issue]
        self.commit(first)
        second = self.packet("round-2", sid="SRC-002")
        with self.assertRaisesRegex(kit.LeadkitError, "employee_correction"):
            self.commit(second)
        second["leads"][0]["issues"] = [dict(issue,state="resolved",source_ids=["SRC-002"],resolved_at=kit.now(),attempts=1)]
        self.assertEqual(self.commit(second)["counts"]["main"], 1)

    def test_bundled_example_commits_and_exports_as_candidate_only(self):
        result = kit.snapshot(self.root, "example-only")
        packet = kit.read_json(Path(__file__).parents[1] / "assets/batch-example.json")
        packet["base_sha256"] = result["base_sha256"]
        receipt = kit.commit(self.root, packet)
        self.assertEqual((receipt["counts"]["main"], receipt["counts"]["candidate"]), (0,1))
        output = self.root / "outputs/example.xlsx"
        kit.export_batch(self.root, "example-only", output)
        with zipfile.ZipFile(output) as archive:
            self.assertIsNone(archive.testzip())

    def test_employee_input_without_url_can_be_pending_not_main(self):
        result = kit.snapshot(self.root, "offline-import")
        packet = kit.read_json(result["batch_path"])
        packet["task"].update(request="补核员工提供的店名", countries=["测试国"], products=["智能眼镜"])
        reference = {"file":"employee-list.xlsx", "sheet":"线索", "row":2}
        packet["leads"] = [{"lead_id":"LOCAL-1", "name":"员工提供商户", "country":"测试国", "status":"candidate", "reason":"仅离线名单，未核验真实性", "discovered_by":[{"channel":"employee_import", "source_ids":[], "input_reference":reference}]}]
        packet["region_research"] = [{"country":"测试国", "area":"未核", "status":"blocked", "checked_at":kit.now(), "languages":[], "markets":[], "communication_channels":[], "ecommerce_platforms":[], "source_ids":[], "limitations":["离线名单待联网补核"]}]
        packet["coverage"] = [{"product":"智能眼镜", "channel":"employee_import", "status":"searched_zero", "attempts":1, "source_ids":[], "input_references":[reference], "discovered_lead_ids":["LOCAL-1"], "limitations":["仅导入线索"]}]
        self.assertEqual(kit.commit(self.root, packet)["counts"]["candidate"], 1)
        kit.export_batch(self.root, "offline-import", self.root / "outputs/offline.xlsx")
        self.assertEqual(kit.load_master(self.root)[0]["sources"], {})

    def test_structured_issue_resolution_does_not_remigrate_derived_unknowns(self):
        first = self.packet()
        first["leads"][0].update(status="candidate", issues=[{"issue_id":"MISSING-PRODUCT", "kind":"product", "detail":"目标产品缺少白牌经营证据", "state":"open", "next_action":"补证", "attempts":0, "next_trigger":"next_collection", "source_ids":[]}])
        first["leads"][0].pop("unknowns")
        self.commit(first)
        saved = kit.load_master(self.root)[0]["leads"]["TEST-001"]
        self.assertEqual(saved["unknowns"], ["目标产品缺少白牌经营证据"])
        self.assertEqual(len(saved["issues"]), 1)
        second = self.packet("round-2", sid="SRC-002")
        second["leads"][0].pop("unknowns")
        second["leads"][0]["issues"] = [dict(saved["issues"][0], state="resolved", source_ids=["SRC-002"], resolved_at=kit.now(), attempts=1)]
        self.commit(second)
        saved = kit.load_master(self.root)[0]["leads"]["TEST-001"]
        self.assertEqual(saved["unknowns"], [])
        self.assertEqual([(x["issue_id"], x["state"]) for x in saved["issues"]], [("MISSING-PRODUCT", "resolved")])
        output = self.root / "outputs/resolved.xlsx"
        kit.export_batch(self.root, "round-2", output)
        with zipfile.ZipFile(output) as archive:
            sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            cell = sheet.find('.//' + kit.tag('c') + '[@r="J2"]')
            self.assertFalse(cell.find('.//' + kit.tag('t')).text)

    def test_batch_id_traversal_rejected(self):
        with self.assertRaises(kit.LeadkitError):
            kit.snapshot(self.root, "../escape")


if __name__ == "__main__":
    unittest.main()
