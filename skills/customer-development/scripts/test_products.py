#!/usr/bin/env python3
"""Synthetic product-scope regressions; no merchant facts or network calls."""
import copy
import tempfile
import unittest
import zipfile
from pathlib import Path

import leadkit as core
import leadkit_workflow as flow
import test_leadkit as fixtures


class ProductScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "workspace"
        core.init_workspace(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def packet(self, bid="scope", product="智能手表表带", targets=None):
        p = fixtures.LeadkitTests.packet(self, bid)
        p["task"]["products"] = list(targets or core.PRIMARY_PRODUCTS)
        p["task"]["supplementary_products"] = [x for x in core.SUPPLEMENTARY_PRODUCTS if x not in p["task"]["products"]]
        for name in ("product_evidence", "white_label_evidence"):
            p["leads"][0][name][0]["product"] = product
            p["leads"][0][name][0]["detail"] = "Synthetic standalone item and explicit unbranded evidence; not real merchant data"
        return p

    def commit(self, packet):
        return fixtures.LeadkitTests.commit(self, packet)

    def test_new_project_defaults_and_audio_assignment_do_not_rewrite_company(self):
        company = (self.root / "config.json").read_bytes()
        p = core.read_json(flow.snapshot(self.root, "default")["batch_path"])
        self.assertEqual(p["task"]["products"], ["智能眼镜", "智能手表", "智能手表表带", "智能手环", "智能戒指"])
        self.assertEqual(p["task"]["supplementary_products"], ["蓝牙耳机", "蓝牙音响"])
        p = core.read_json(flow.snapshot(self.root, "audio", {"products":["蓝牙耳机"]})["batch_path"])
        self.assertEqual(p["task"]["products"], ["蓝牙耳机"])
        self.assertEqual(p["task"]["supplementary_products"], [])
        self.assertEqual(company, (self.root / "config.json").read_bytes())

    def test_standalone_smartwatch_strap_can_qualify_separately(self):
        p = self.packet()
        result = self.commit(p)
        self.assertEqual(result["counts"]["new_qualified_entities"], 1)
        saved = core.load_master(self.root)[0]["batches"]["scope"]["lead_snapshots"][0]
        self.assertEqual(saved["qualified_products"], ["智能手表表带"])
        self.assertNotIn("智能手表", saved["qualified_products"])
        output = self.root / "outputs/straps.xlsx"
        core.export_batch(self.root, "scope", output)
        with zipfile.ZipFile(output) as z:
            self.assertIsNone(z.testzip())
            self.assertIn("智能手表表带", z.read("xl/worksheets/sheet1.xml").decode())
            self.assertIn("附带拓展品类", z.read("xl/worksheets/sheet4.xml").decode())

    def test_incidental_audio_does_not_inflate_primary_product_coverage(self):
        p = self.packet()
        for field in ("product_evidence", "white_label_evidence"):
            audio = copy.deepcopy(p["leads"][0][field][0])
            audio["product"] = "蓝牙耳机"
            p["leads"][0][field].append(audio)
        self.commit(p)
        saved = core.load_master(self.root)[0]["batches"]["scope"]["lead_snapshots"][0]
        self.assertEqual(saved["qualified_products"], ["智能手表表带"])
        self.assertEqual(len(saved["product_evidence"]), 2)

    def test_watch_white_label_proof_cannot_qualify_strap(self):
        p = self.packet(targets=["智能手表表带"])
        p["leads"][0]["white_label_evidence"][0]["product"] = "智能手表"
        with self.assertRaisesRegex(core.LeadkitError, "same requested product"):
            self.commit(p)
        self.assertFalse(core.load_master(self.root)[0]["leads"])

    def test_audio_only_is_incidental_not_main_and_can_later_qualify_for_audio_task(self):
        p = self.packet(product="蓝牙耳机")
        with self.assertRaisesRegex(core.LeadkitError, "requested product"):
            self.commit(p)
        p["leads"][0]["status"] = "candidate"
        p["leads"][0]["reason"] = "Synthetic incidental audio lead outside the primary task scope"
        result = self.commit(p)
        self.assertEqual(result["counts"]["main"], 0)
        self.assertEqual(result["counts"]["candidate"], 1)
        self.assertEqual(result["counts"]["new_qualified_entities"], 0)
        old_scope = core.scope_key(p["task"])
        p2 = self.packet("audio-task", "蓝牙耳机", ["蓝牙耳机"])
        result = self.commit(p2)
        self.assertEqual(result["counts"]["main"], 1)
        self.assertEqual(result["counts"]["new_entities"], 0)
        qualifications = core.load_master(self.root)[0]["leads"]["TEST-001"]["qualifications"]
        self.assertEqual(qualifications[old_scope]["status"], "candidate")
        self.assertEqual(qualifications[core.scope_key(p2["task"])]["qualified_products"], ["蓝牙耳机"])

    def test_explicit_or_invalid_supplementary_scope_is_checked_before_snapshot(self):
        for index, supplementary in enumerate((["智能眼镜"], ["普通皮带"], "蓝牙耳机")):
            with self.assertRaises(core.LeadkitError):
                flow.snapshot(self.root, "bad-" + str(index), {"products":["智能眼镜"], "supplementary_products":supplementary})
            self.assertFalse((self.root / "research" / ("bad-" + str(index))).exists())
        p = core.read_json(flow.snapshot(self.root, "valid", {"products":["智能眼镜"], "supplementary_products":["蓝牙耳机"]})["batch_path"])
        self.assertEqual(p["task"]["supplementary_products"], ["蓝牙耳机"])

    def test_legacy_scope_and_continuation_are_not_silently_changed(self):
        legacy = ["智能眼镜", "智能手表", "智能手环", "智能戒指", "蓝牙耳机", "蓝牙音响"]
        p = self.packet(product="智能眼镜", targets=legacy)
        p["task"].pop("supplementary_products")
        self.commit(p)
        original = copy.deepcopy(core.load_master(self.root)[0]["batches"]["scope"])
        continued = flow.continue_task(self.root, "scope", "continued")
        current = core.read_json(continued["batch_path"])
        self.assertEqual(current["task"]["products"], legacy)
        self.assertEqual(current["task"]["supplementary_products"], [])
        self.assertEqual(core.load_master(self.root)[0]["batches"]["scope"], original)
        new = core.read_json(flow.snapshot(self.root, "new-primary")["batch_path"])
        self.assertEqual(new["task"]["products"], core.PRIMARY_PRODUCTS)


if __name__ == "__main__":
    unittest.main()
