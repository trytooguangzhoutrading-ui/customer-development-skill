#!/usr/bin/env python3
"""Offline market delivery tests, using synthetic countries and merchants."""
import copy
import json
import subprocess
import sys
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

    def test_single_sheet_market_export_deduplicates_entities_and_keeps_all_relations(self):
        master = self.committed_fixture()
        master["batches"]["two"]["task"]["output_layout"] = "single_sheet"
        core.safe_write(self.root / "execution/master.json", master)
        before = (self.root / "execution/master.json").read_bytes()
        output = self.root / "outputs/customer-total.xlsx"
        result = core.export_batch(self.root, "two", output)
        names, rows = fixtures.single_sheet_rows(output)
        self.assertEqual(names, ["客户总表"])
        values = {row[5]: dict(zip(rows[0], row)) for row in rows[1:]}
        self.assertEqual(set(values), {"TEST-001", "STORE", "UNKNOWN"})
        self.assertEqual(result["counts"]["main"], 2)
        self.assertEqual(result["counts"]["independent_main"], 1)
        self.assertEqual(result["counts"]["brand_store_main"], 1)
        self.assertIn("甲市场", values["TEST-001"]["市场（已核关系）"])
        self.assertIn("乙市场", values["TEST-001"]["市场（已核关系）"])
        self.assertEqual(values["STORE"]["商户类型"], "品牌店")
        self.assertEqual(values["UNKNOWN"]["市场（已核关系）"], "待市场定位")
        self.assertIn("SRC-001", values["TEST-001"]["市场关系依据"])
        self.assertIn("https://example.invalid/merchant", values["TEST-001"]["来源URL"])
        self.assertEqual(list((self.root / "outputs").iterdir()), [output])
        self.assertEqual(before, (self.root / "execution/master.json").read_bytes())

    def test_pending_market_observation_does_not_hide_formal_entity_in_single_row(self):
        master = self.committed_fixture()
        master["batches"]["two"]["lead_snapshots"][0]["status"] = "candidate"
        view = market.single_view(master, "two")
        entity = next(x for x in view["lead_snapshots"] if x["lead_id"] == "TEST-001")
        self.assertEqual(entity["status"], "main")
        self.assertEqual({r["market_id"]:r["status"] for r in entity["market_relations"]}, {"M1":"main", "M2":"candidate"})
        self.assertEqual(view["counts"]["independent_main"], 1)

    def test_single_row_keeps_verified_market_relation_and_identity_source_from_merged_alias(self):
        master = self.committed_fixture()
        self.alias_correction(master)
        source = dict(master["sources"]["SRC-001"], url="https://example.invalid/merged-branch", title="虚构别名关系及丙市场位置证据")
        master["sources"]["SRC-ALIAS"] = source
        correction = master["batches"]["correction"]
        sequence = correction["research_summary"]["market_sequence"]
        sequence["directory"]["markets"].append({"market_id":"M3", "city_id":"CITY", "name":"丙市场", "order":3, "state":"active", "boundary":"虚构第三市场"})
        sequence["active_market_id"] = "M3"
        alias = correction["lead_snapshots"][0]
        alias["market_assignment"].update(market_id="M3", source_ids=["SRC-ALIAS"])
        alias["exclusion"]["source_ids"] = ["SRC-ALIAS"]
        alias["relationship_evidence"] = {"detail":"同主体有据关系", "source_ids":["SRC-ALIAS"]}
        view = market.single_view(master, "correction")
        entity = next(x for x in view["lead_snapshots"] if x["lead_id"] == "STORE")
        self.assertEqual({r["market_id"] for r in entity["market_relations"]}, {"M1", "M2", "M3"})
        self.assertIn("SRC-ALIAS", entity["export_source_ids"])
        self.assertEqual(entity["status"], "main")
        output = self.root / "outputs/alias-relations.xlsx"
        core.export_single_sheet(master, view, "correction", output)
        _, rows = fixtures.single_sheet_rows(output)
        row = next(r for r in rows[1:] if r[5] == "STORE")
        self.assertIn("丙市场", row[2])
        self.assertIn("https://example.invalid/merged-branch", row[11])
        self.assertIn("同主体重复记录已合并", row[19])

    def test_single_view_does_not_promote_known_closed_entity_from_old_market_record(self):
        master = self.committed_fixture()
        master["batches"]["two"]["lead_snapshots"][0].update(status="excluded", exclusion={"code":"closed", "origin":"evidence", "detail":"本批确证停业", "source_ids":["SRC-001"]})
        view = market.single_view(master, "two")
        self.assertEqual(next(x for x in view["lead_snapshots"] if x["lead_id"] == "TEST-001")["status"], "excluded")
        self.assertEqual(view["counts"]["independent_main"], 0)

    def global_decision(self, master, bid, hour, code="closed", lid="TEST-001"):
        batch = copy.deepcopy(master["batches"]["two"])
        batch["committed_at"] = "2026-11-15T%02d:00:00+00:00" % hour
        batch["task"]["products"] = ["智能戒指"]
        batch["scope_id"] = core.scope_key(batch["task"])
        lead = copy.deepcopy(next(x for x in batch["lead_snapshots"] if x["lead_id"] == lid))
        lead.pop("qualifications", None)
        lead.update(status="excluded", exclusion={"code":code, "origin":"evidence", "detail":"虚构跨范围全主体排除", "source_ids":["SRC-001"]})
        if code == "brand_owner":
            lead["brand_profile"] = "brand_owner"
        else:
            lead["entity_status"] = "closed"
        batch["lead_snapshots"] = [lead]
        master["batches"][bid] = batch
        return batch, lead

    def later_market_batch(self, master, bid, hour):
        batch = copy.deepcopy(master["batches"]["two"])
        batch.update(committed_at="2026-11-15T%02d:00:00+00:00" % hour, lead_snapshots=[])
        master["batches"][bid] = batch
        return batch

    def test_cross_scope_global_exclusions_apply_by_cutoff_without_rewriting_old_batches(self):
        baseline = self.committed_fixture()
        for code in ("closed", "brand_owner"):
            with self.subTest(code=code):
                master = copy.deepcopy(baseline)
                self.global_decision(master, "global", 2, code)
                self.later_market_batch(master, "resumed", 3)
                original = copy.deepcopy(master)
                old = market.single_view(master, "two")
                new = market.single_view(master, "resumed")
                self.assertEqual(next(x for x in old["lead_snapshots"] if x["lead_id"] == "TEST-001")["status"], "main")
                self.assertEqual(next(x for x in new["lead_snapshots"] if x["lead_id"] == "TEST-001")["status"], "excluded")
                self.assertEqual(new["counts"]["independent_main"], 0)
                self.assertEqual(master, original)

    def test_alias_global_exclusion_blocks_canonical_without_explicit_restoration(self):
        master = self.committed_fixture()
        self.global_decision(master, "global", 2)
        self.alias_correction(master)
        master["batches"]["correction"]["committed_at"] = "2026-11-15T03:00:00+00:00"
        later = self.later_market_batch(master, "resumed", 4)
        later["lead_snapshots"] = [copy.deepcopy(master["batches"]["two"]["lead_snapshots"][1])]
        view = market.single_view(master, "resumed")
        self.assertEqual([(x["lead_id"], x["status"]) for x in view["lead_snapshots"] if x["lead_id"] == "STORE"], [("STORE", "excluded")])
        self.assertEqual(view["counts"]["brand_store_main"], 0)

    def test_inherited_or_unsupported_resolution_cannot_restore_entity(self):
        baseline = self.committed_fixture()
        for mode in ("inherited", "old_source", "weak", "not_active"):
            with self.subTest(mode=mode):
                master = copy.deepcopy(baseline)
                self.global_decision(master, "global", 2)
                restoration, lead = self.global_decision(master, "restore", 3)
                lead.update(status="main", entity_status="active", brand_profile="independent")
                lead.pop("exclusion")
                master["sources"]["SRC-RESTORE"] = dict(master["sources"]["SRC-001"], title="虚构恢复原文", level="search_snippet" if mode == "weak" else "current_original")
                lead["exclusion_resolution"] = {"detail":"明确纠正旧排除", "source_ids":["SRC-001" if mode == "old_source" else "SRC-RESTORE"]}
                if mode == "inherited":
                    facts = copy.deepcopy(lead)
                    facts.pop("exclusion_resolution")
                    lead["qualifications"] = {restoration["scope_id"]:{"batch_id":"restore", "evidence_snapshot":facts}}
                if mode == "not_active":
                    lead.pop("entity_status")
                self.later_market_batch(master, "resumed", 4)
                view = market.single_view(master, "resumed")
                self.assertEqual(next(x for x in view["lead_snapshots"] if x["lead_id"] == "TEST-001")["status"], "excluded")

    def test_explicit_restoration_needs_same_scope_requalification_and_keeps_sources(self):
        baseline = self.committed_fixture()
        for code in ("closed", "brand_owner"):
            with self.subTest(code=code):
                master = copy.deepcopy(baseline)
                self.global_decision(master, "global", 2, code)
                _, lead = self.global_decision(master, "restore", 3, code)
                master["sources"]["SRC-RESTORE"] = dict(master["sources"]["SRC-001"], title="虚构新恢复原文")
                lead.update(status="main", entity_status="active", brand_profile="independent", identity_source_ids=["SRC-RESTORE"], exclusion_resolution={"detail":"新证据明确恢复", "source_ids":["SRC-RESTORE"]})
                lead.pop("exclusion")
                self.later_market_batch(master, "resumed", 4)
                view = market.single_view(master, "resumed")
                pending = next(x for x in view["lead_snapshots"] if x["lead_id"] == "TEST-001")
                self.assertEqual(pending["status"], "candidate")
                self.assertIn("SRC-RESTORE", pending["export_source_ids"])
                self.assertTrue(any("须重新核验" in x for x in pending["unknowns"]))
                rechecked = self.later_market_batch(master, "rechecked", 5)
                rechecked["lead_snapshots"] = [copy.deepcopy(master["batches"]["two"]["lead_snapshots"][0])]
                result = market.single_view(master, "rechecked")
                self.assertEqual(next(x for x in result["lead_snapshots"] if x["lead_id"] == "TEST-001")["status"], "main")

    def test_current_market_export_obeys_cross_scope_global_exclusion(self):
        master = self.committed_fixture()
        master["batches"]["two"]["task"]["output_layout"] = "single_sheet"
        self.global_decision(master, "global", 2)
        core.safe_write(self.root / "execution/master.json", master)
        output = self.root / "outputs/current-excluded.xlsx"
        result = core.export_current(self.root, output, batch_id="two")
        _, rows = fixtures.single_sheet_rows(output)
        self.assertNotIn("TEST-001", [row[5] for row in rows[1:]])
        self.assertEqual(result["counts"]["independent_main"], 0)

    def test_explicit_same_scope_restoration_of_canonical_entity_is_accepted(self):
        master = self.committed_fixture()
        self.global_decision(master, "global", 2)
        self.alias_correction(master)
        master["batches"]["correction"]["committed_at"] = "2026-11-15T03:00:00+00:00"
        restored = self.later_market_batch(master, "restored", 4)
        lead = copy.deepcopy(master["batches"]["two"]["lead_snapshots"][1])
        master["sources"]["SRC-RESTORE"] = dict(master["sources"]["SRC-001"], title="虚构恢复营业原文")
        lead.update(entity_status="active", identity_source_ids=["SRC-RESTORE"], exclusion_resolution={"detail":"明确恢复营业", "source_ids":["SRC-RESTORE"]})
        restored["lead_snapshots"] = [lead]
        view = market.single_view(master, "restored")
        self.assertEqual(next(x for x in view["lead_snapshots"] if x["lead_id"] == "STORE")["status"], "main")
        self.assertEqual(view["counts"]["brand_store_main"], 1)
        self.assertEqual(view["counts"]["independent_main"], 0)

    def test_current_export_progress_uses_latest_market_batch_in_scope(self):
        master = self.committed_fixture()
        master["batches"]["one"]["task"]["output_layout"] = "single_sheet"
        master["batches"]["two"]["research_summary"]["next_action"] = "继续乙市场真实入口"
        core.safe_write(self.root / "execution/master.json", master)
        output = self.root / "outputs/current-progress.xlsx"
        core.export_current(self.root, output, batch_id="one")
        _, rows = fixtures.single_sheet_rows(output)
        self.assertIn("当前市场：测试国／测试城／乙市场", rows[1][-1])
        self.assertIn("继续乙市场真实入口", rows[1][-1])

    def test_nonempty_single_sheet_reports_committed_market_progress_and_next_entry(self):
        master = self.committed_fixture()
        batch = master["batches"]["two"]
        batch["research_summary"].update(next_action="继续乙市场西门第二页", next_route_id="ROUTE-M2", pending_entry_ids=["ENTRY-P2"], blocked_entry_ids=["ENTRY-LOCKED"])
        output = self.root / "outputs/progress.xlsx"
        core.export_single_sheet(master, market.single_view(master, "two"), "two", output)
        _, rows = fixtures.single_sheet_rows(output)
        self.assertEqual(len(rows), 4)
        for row in rows[1:]:
            self.assertIn("当前市场：测试国／测试城／乙市场", row[-1])
            self.assertIn("采集中，未完成", row[-1])
            self.assertIn("仅虚构目录", row[-1])
            self.assertIn("继续乙市场西门第二页", row[-1])
            self.assertIn("ENTRY-P2", row[-1])
            self.assertIn("ENTRY-LOCKED", row[-1])

    def test_zero_result_single_sheet_explains_current_market_without_creating_market_files(self):
        master = self.committed_fixture()
        for batch in master["batches"].values():
            batch["lead_snapshots"] = []
        core.safe_write(self.root / "execution/master.json", master)
        output = self.root / "outputs/zero.xlsx"
        result = market.export_single(self.root, "two", output)
        names, rows = fixtures.single_sheet_rows(output)
        self.assertEqual(names, ["客户总表"])
        self.assertEqual(result["customer_rows"], 0)
        self.assertEqual(rows[1][2], "乙市场")
        self.assertEqual(rows[1][4], "说明（非客户）")
        self.assertEqual(list((self.root / "outputs").iterdir()), [output])

    def test_current_single_sheet_includes_all_known_markets_for_current_qualified_entity(self):
        master = self.committed_fixture()
        master["batches"]["two"]["task"]["output_layout"] = "single_sheet"
        core.safe_write(self.root / "execution/master.json", master)
        output = self.root / "outputs/current-total.xlsx"
        core.export_current(self.root, output, batch_id="two")
        names, rows = fixtures.single_sheet_rows(output)
        self.assertEqual(names, ["客户总表"])
        row = next(r for r in rows[1:] if r[5] == "TEST-001")
        self.assertIn("甲市场", row[2])
        self.assertIn("乙市场", row[2])
        self.assertIn("当前有效资格", row[-1])

    def test_market_cli_output_is_single_file_and_output_dir_is_explicit_legacy(self):
        master = self.committed_fixture()
        master["batches"]["two"]["task"]["output_layout"] = "single_sheet"
        core.safe_write(self.root / "execution/master.json", master)
        script = Path(market.__file__)
        base = [sys.executable, str(script), "export", "--workspace", str(self.root), "--batch-id", "two"]
        output = self.root / "outputs/cli.xlsx"
        call = subprocess.run(base + ["--output", str(output)], capture_output=True, text=True)
        self.assertEqual(call.returncode, 0, call.stderr)
        self.assertEqual(json.loads(call.stdout)["sheets"], 1)
        self.assertEqual(fixtures.single_sheet_rows(output)[0], ["客户总表"])
        call = subprocess.run(base + ["--output-dir", str(self.root / "outputs/legacy")], capture_output=True, text=True)
        self.assertEqual(call.returncode, 0, call.stderr)
        result = json.loads(call.stdout)
        self.assertEqual(result["market_workbooks"], 2)
        brand_file = next(Path(p) for p in result["files"] if "M2-" in p)
        with zipfile.ZipFile(brand_file) as archive:
            self.assertIn("品牌店", archive.read("xl/workbook.xml").decode())


if __name__ == "__main__":
    unittest.main()
