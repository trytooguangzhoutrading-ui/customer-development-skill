#!/usr/bin/env python3
"""Offline market delivery tests, using synthetic countries and merchants."""
import copy
import tempfile
import unittest
import zipfile
from pathlib import Path

import leadkit as core
import leadkit_market as market
import test_leadkit as fixtures


class MarketDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "workspace"
        core.init_workspace(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def directory(self):
        return {"countries": [{"country_id": "COUNTRY", "name": "测试国", "order": 1}],
                "cities": [{"city_id": "CITY", "country_id": "COUNTRY", "name": "测试城", "order": 1}],
                "markets": [{"market_id": "M1", "city_id": "CITY", "name": "甲市场", "order": 1, "state": "complete", "boundary": "仅虚构目录"},
                            {"market_id": "M2", "city_id": "CITY", "name": "乙市场", "order": 2, "state": "active", "boundary": "仅虚构目录"}],
                "coverage": {"status": "partial", "detail": "仅测试登记范围，非全国全部市场"}}

    def committed_fixture(self):
        packet = fixtures.LeadkitTests.packet(self, "one")
        packet["task"]["customer_profile"].update(brand_policy="independent_product", collect_brand_stores=True)
        packet["leads"][0].update(brand_profile="independent", white_label_evidence=[])
        fixtures.LeadkitTests.commit(self, packet)
        master = core.load_master(self.root)[0]
        first = master["batches"]["one"]
        first["task"]["collection_strategy"] = "market_sequence"
        first["research_summary"] = {"market_sequence": {"active_market_id": "M1", "directory": self.directory()}}
        first["lead_snapshots"][0]["market_assignment"] = {"market_id": "M1", "relation": "within", "source_ids": ["SRC-001"], "detail": "虚构场内归属"}
        first["committed_at"] = "2026-11-15T00:00:00+00:00"
        second = copy.deepcopy(first)
        second["committed_at"] = "2026-11-15T01:00:00+00:00"
        second["research_summary"]["market_sequence"]["active_market_id"] = "M2"
        # Same legal entity has a separately evidenced branch in market two.
        second["lead_snapshots"][0]["market_assignment"]["market_id"] = "M2"
        store = copy.deepcopy(second["lead_snapshots"][0])
        store.update(lead_id="STORE", name="虚构官方品牌店", brand_profile="official_store", collection_segment="brand_store")
        second["lead_snapshots"].append(store)
        unknown = copy.deepcopy(store)
        unknown.update(lead_id="UNKNOWN", name="未定位虚构商户", status="candidate", brand_profile="unknown")
        unknown.pop("market_assignment")
        second["lead_snapshots"].append(unknown)
        master["leads"].update({"STORE": copy.deepcopy(store), "UNKNOWN": copy.deepcopy(unknown)})
        master["batches"]["two"] = second
        return master

    def test_market_files_separate_and_country_deduplicates_same_entity(self):
        master = self.committed_fixture()
        directory, views, unassigned, grouped = market.bundle_views(master, "two")
        self.assertEqual(set(views), {"M1", "M2"})
        self.assertEqual(len(views["M1"]["lead_snapshots"]), 1)
        self.assertEqual(len(views["M2"]["lead_snapshots"]), 2)
        self.assertEqual([x["lead_id"] for x in unassigned["lead_snapshots"]], ["UNKNOWN"])
        rows = market.directory_tables(directory, grouped)
        self.assertEqual(rows[0][1][1][4:7], [1, 1, 0])
        self.assertEqual(rows[2][1][1][5], 1)
        self.assertEqual(rows[2][1][2][5], 1)

    def test_reference_batch_does_not_import_future_qualification(self):
        master = self.committed_fixture()
        _, views, unassigned, grouped = market.bundle_views(master, "one")
        self.assertEqual(set(views), {"M1"})
        self.assertFalse(unassigned["lead_snapshots"])
        self.assertFalse(grouped["M1"]["brand_store"])

    def test_formal_records_need_real_same_market_assignment(self):
        master = self.committed_fixture()
        for change in ({}, {"market_id": "M2", "relation": "within"}, {"market_id": "M1", "relation": "city_only"}):
            changed = copy.deepcopy(master)
            changed["batches"]["one"]["lead_snapshots"][0]["market_assignment"] = change
            with self.assertRaisesRegex(core.LeadkitError, "guessed or different"):
                market.bundle_views(changed, "one")

    def test_bundle_export_preserves_master_and_separates_brand_store_sheet(self):
        master = self.committed_fixture()
        core.safe_write(self.root / "execution/master.json", master)
        before = (self.root / "execution/master.json").read_bytes()
        destination = self.root / "outputs/delivery"
        result = market.export_bundle(self.root, "two", destination)
        self.assertEqual(result["market_workbooks"], 2)
        self.assertEqual(result["unassigned_records"], 1)
        self.assertEqual(before, (self.root / "execution/master.json").read_bytes())
        store_file = next(Path(p) for p in result["files"] if "M2-" in p)
        with zipfile.ZipFile(store_file) as archive:
            self.assertNotIn("STORE", archive.read("xl/worksheets/sheet1.xml").decode())
            self.assertIn("STORE", archive.read("xl/worksheets/sheet2.xml").decode())
        with self.assertRaisesRegex(core.LeadkitError, "new delivery directory"):
            market.export_bundle(self.root, "two", destination)

    def test_partial_directory_and_unknown_order_do_not_claim_completed_country(self):
        directory = self.directory()
        directory["markets"][0].update(order=None, state="needs_research")
        tables = market.directory_tables(directory)
        self.assertIn("非全国", tables[0][1][1][7])
        row = next(x for x in tables[2][1][1:] if x[2] == "甲市场")
        self.assertEqual(row[3:5], ["待规模核验", "待预研"])
        broken = copy.deepcopy(directory)
        broken["markets"][0]["city_id"] = "MISSING"
        with self.assertRaisesRegex(core.LeadkitError, "no registered city"):
            market.directory_tables(broken)

    def test_cutoff_compares_actual_instants_not_timestamp_text(self):
        master = self.committed_fixture()
        # The first is earlier despite its larger clock text; the third is later
        # despite its earlier local calendar date.
        master["batches"]["one"]["committed_at"] = "2026-11-15T09:00:00+10:00"
        future = copy.deepcopy(master["batches"]["two"])
        future["committed_at"] = "2026-11-14T22:30:00-03:00"
        future["lead_snapshots"][0]["name"] = "截止后才发现的未来内容"
        master["batches"]["future"] = future
        original = copy.deepcopy(master)
        _, views, _, _ = market.bundle_views(master, "two")
        self.assertEqual(set(views), {"M1", "M2"})
        self.assertNotIn("未来内容", str(views))
        self.assertEqual(master, original)
        master["batches"]["two"]["committed_at"] = "2026-11-15T01:00:00"
        with self.assertRaisesRegex(core.LeadkitError, "timezone"):
            market.bundle_views(master, "two")

    def alias_correction(self, master):
        correction = copy.deepcopy(master["batches"]["two"])
        correction["committed_at"] = "2026-11-15T02:00:00+00:00"
        alias = copy.deepcopy(next(x for x in correction["lead_snapshots"] if x["lead_id"] == "TEST-001"))
        alias.update(status="excluded", canonical_id="STORE", exclusion={"code":"duplicate", "origin":"evidence", "detail":"虚构同主体关系", "source_ids":["SRC-001"]})
        correction["lead_snapshots"] = [alias]
        master["batches"]["correction"] = correction
        master["leads"]["TEST-001"] = copy.deepcopy(alias)

    def test_future_canonical_relationship_does_not_leak_into_earlier_delivery(self):
        master = self.committed_fixture()
        self.alias_correction(master)
        _, views, _, grouped = market.bundle_views(master, "two")
        self.assertEqual(views["M1"]["lead_snapshots"][0]["lead_id"], "TEST-001")
        self.assertEqual(grouped["M2"]["independent"], {"TEST-001"})
        self.assertEqual(grouped["M2"]["brand_store"], {"STORE"})

    def test_duplicate_alias_preserves_qualified_record_and_uses_one_canonical_id(self):
        master = self.committed_fixture()
        self.alias_correction(master)
        directory, views, _, grouped = market.bundle_views(master, "correction")
        self.assertEqual([(x["lead_id"], x["status"]) for x in views["M2"]["lead_snapshots"]], [("STORE", "main")])
        self.assertEqual(views["M1"]["lead_snapshots"][0]["lead_id"], "STORE")
        self.assertEqual(views["M1"]["lead_snapshots"][0]["recorded_lead_id"], "TEST-001")
        self.assertEqual(grouped["M2"]["brand_store"], {"STORE"})
        tables = market.directory_tables(directory, grouped)
        country = tables[0][1][1]
        self.assertEqual(country[4:6], [1, 1])
        self.assertEqual(country[8], 1)
        self.assertIn("不能相加", country[9])
        self.assertIn("同主体 1 个", country[9])

    def test_old_alias_candidate_does_not_replace_canonical_merchant_decision(self):
        master = self.committed_fixture()
        second = master["batches"]["two"]
        # Both records concern one market. The old duplicate candidate is listed
        # after the canonical main row, and is merged in a later frozen batch.
        canonical = copy.deepcopy(second["lead_snapshots"][1])
        duplicate = copy.deepcopy(second["lead_snapshots"][0])
        duplicate["status"] = "candidate"
        second["lead_snapshots"] = [canonical, duplicate]
        self.alias_correction(master)
        _, views, _, grouped = market.bundle_views(master, "correction")
        self.assertEqual([(x["lead_id"], x["status"]) for x in views["M2"]["lead_snapshots"]], [("STORE", "main")])
        self.assertEqual(grouped["M2"]["brand_store"], {"STORE"})
        # A later decision made on the canonical row itself must still win.
        revised = copy.deepcopy(master["batches"]["correction"])
        revised["committed_at"] = "2026-11-15T03:00:00+00:00"
        revised["lead_snapshots"] = [dict(canonical, status="candidate")]
        master["batches"]["revised"] = revised
        _, views, _, grouped = market.bundle_views(master, "revised")
        self.assertEqual(views["M2"]["lead_snapshots"][0]["status"], "candidate")
        self.assertFalse(grouped["M2"]["brand_store"])

    def test_active_market_with_zero_records_still_gets_its_workbook(self):
        master = self.committed_fixture()
        master["batches"]["two"]["lead_snapshots"] = []
        core.safe_write(self.root / "execution/master.json", master)
        before = (self.root / "execution/master.json").read_bytes()
        result = market.export_bundle(self.root, "two", self.root / "outputs/empty-market")
        self.assertEqual(result["market_workbooks"], 2)
        output = next(Path(p) for p in result["files"] if "M2-" in p)
        with zipfile.ZipFile(output) as archive:
            self.assertNotIn("TEST-001", archive.read("xl/worksheets/sheet1.xml").decode())
            notes = archive.read("xl/worksheets/sheet5.xml").decode()
            self.assertIn("乙市场", notes)
            self.assertIn("采集中", notes)
            self.assertIn("独立商户 0", notes)
        self.assertEqual(before, (self.root / "execution/master.json").read_bytes())

    def test_unknown_market_candidates_stay_unassigned_and_missing_active_id_is_rejected(self):
        master = self.committed_fixture()
        candidate = master["batches"]["two"]["lead_snapshots"][-1]
        candidate["market_assignment"] = {"market_id":"UNREGISTERED", "relation":"within"}
        _, views, unassigned, _ = market.bundle_views(master, "two")
        self.assertNotIn("UNREGISTERED", views)
        self.assertEqual([x["lead_id"] for x in unassigned["lead_snapshots"]], ["UNKNOWN"])
        master["batches"]["two"]["research_summary"]["market_sequence"]["active_market_id"] = "UNREGISTERED"
        with self.assertRaisesRegex(core.LeadkitError, "missing from the directory"):
            market.bundle_views(master, "two")

    def test_preflight_without_selected_market_can_deliver_pending_records(self):
        master = self.committed_fixture()
        first = master["batches"]["one"]
        first["research_summary"]["market_sequence"].update(active_market_id=None)
        first["research_summary"]["market_sequence"]["directory"]["markets"] = []
        first["lead_snapshots"][0].update(status="candidate")
        first["lead_snapshots"][0].pop("market_assignment")
        _, views, unassigned, _ = market.bundle_views(master, "one")
        self.assertEqual(views, {})
        self.assertEqual(len(unassigned["lead_snapshots"]), 1)

    def test_long_unicode_and_path_like_market_names_export_within_destination(self):
        master = self.committed_fixture()
        directory = master["batches"]["two"]["research_summary"]["market_sequence"]["directory"]
        mid = "M" + "2" * 79
        directory["countries"][0]["name"] = "../测试/国家"
        directory["cities"][0]["name"] = "测试\\城市"
        directory["markets"][1].update(market_id=mid, name="../" + "长名字市场" * 20)
        master["batches"]["two"]["research_summary"]["market_sequence"]["active_market_id"] = mid
        for lead in master["batches"]["two"]["lead_snapshots"]:
            if "market_assignment" in lead:
                lead["market_assignment"]["market_id"] = mid
        core.safe_write(self.root / "execution/master.json", master)
        destination = self.root / "outputs/path-safe"
        result = market.export_bundle(self.root, "two", destination)
        for filename in result["files"]:
            path = Path(filename)
            self.assertTrue(path.is_file())
            path.relative_to(destination.resolve())
            self.assertLessEqual(len(path.name.encode("utf-8")), 255)


if __name__ == "__main__":
    unittest.main()
