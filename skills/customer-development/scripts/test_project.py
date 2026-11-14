#!/usr/bin/env python3
"""Isolated project-binding regression tests. No real customers or networking."""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import leadkit as core
import leadkit_project as project
import leadkit_workflow as flow


class ProjectBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "project"

    def tearDown(self):
        self.temp.cleanup()

    def excel_project(self):
        self.root.mkdir()
        core.write_xlsx(self.root / "old.xlsx", [("Customers", [["ID", "Name"], ["TEST-1", "Synthetic"]], [20, 30])])
        core.safe_write(self.root / "company.json", {"products": core.PRODUCTS, "default_buyer_types": core.DEFAULT_BUYER_TYPES})
        (self.root / "rules.md").write_text("Synthetic effective company rules", encoding="utf-8")
        project.bind(self.root, {"kind": "xlsx", "path": "old.xlsx"}, "company.json", "rules.md", "test-rules")

    def test_new_json_workspace_binds_once_and_copied_project_resolves(self):
        core.init_workspace(self.root)
        initial = project.inspect(self.root)
        self.assertEqual(initial["master"]["kind"], "json")
        raw = (self.root / project.BINDING).read_text()
        self.assertNotIn(str(self.base), raw)
        moved = self.base / "moved"
        shutil.copytree(self.root, moved)
        result = project.inspect(moved / "outputs")
        self.assertTrue(result["ready"])
        self.assertEqual(result["project_root"], str(moved))
        self.assertEqual(result["project_id"], initial["project_id"])
        self.assertEqual(result["master"]["resolved_path"], str(moved / "execution/master.json"))

    def test_init_rejects_nested_empty_database_and_existing_binding(self):
        core.init_workspace(self.root)
        with self.assertRaisesRegex(core.LeadkitError, "another customer database"):
            core.init_workspace(self.root / "new-chat")
        self.assertFalse((self.root / "new-chat").exists())
        with self.assertRaisesRegex(core.LeadkitError, "another customer database"):
            core.init_workspace(self.root)

    def test_legacy_json_ancestor_also_blocks_nested_init(self):
        core.init_workspace(self.root)
        (self.root / project.BINDING).unlink()
        with self.assertRaisesRegex(core.LeadkitError, "existing customer workspace"):
            core.init_workspace(self.root / "nested")

    def test_excel_binding_preserves_workbook_and_blocks_json_runtime(self):
        self.excel_project()
        before = (self.root / "old.xlsx").read_bytes()
        with self.assertRaisesRegex(core.LeadkitError, "Excel master"):
            core.load_master(self.root)
        with self.assertRaisesRegex(core.LeadkitError, "Excel master"):
            flow.snapshot(self.root, "mistaken-new-db")
        with self.assertRaises(core.LeadkitError):
            core.init_workspace(self.root / "customer-research")
        self.assertEqual(before, (self.root / "old.xlsx").read_bytes())
        self.assertFalse((self.root / "execution/master.json").exists())

    def test_binding_change_requires_reason_and_preserves_previous(self):
        self.excel_project()
        core.write_xlsx(self.root / "reviewed.xlsx", [("Customers", [["ID"], ["TEST-2"]], [20])])
        args = (self.root, {"kind": "xlsx", "path": "reviewed.xlsx"}, "company.json", "rules.md", "test-rules")
        with self.assertRaisesRegex(core.LeadkitError, "--replace"):
            project.bind(*args)
        with self.assertRaisesRegex(core.LeadkitError, "reason"):
            project.bind(*args, replace=True)
        project.bind(*args, replace=True, reason="User selected reviewed workbook")
        b = core.read_json(self.root / project.BINDING)
        self.assertEqual(b["history"][0]["previous"]["master"]["path"], "old.xlsx")
        self.assertEqual(b["history"][0]["reason"], "User selected reviewed workbook")
        self.assertFalse(project.bind(*args)["changed"])

    def test_supplemental_history_is_read_only_and_missing_is_explicit(self):
        core.init_workspace(self.root)
        other = self.base / "supplement"
        core.init_workspace(other)
        prior = (other / "execution/master.json").read_bytes()
        project.bind(self.root, {"kind": "json", "path": "execution/master.json"}, additional_sources=[{"kind":"json", "path":"../supplement/execution/master.json", "label":"Prior collection"}], replace=True, reason="Use previous collection in dedupe")
        report = project.inspect(self.root)
        self.assertTrue(report["history_review_required"])
        self.assertFalse(report["net_new_verified"])
        task = core.read_json(flow.snapshot(self.root, "next")["batch_path"])["task"]
        context = task["project_context"]
        self.assertEqual(context["additional_sources"][0]["role"], "dedupe_only")
        self.assertEqual(context["additional_sources"][0]["sha256"], core.digest_bytes(prior))
        self.assertFalse(context["net_new_verified"])
        self.assertEqual(prior, (other / "execution/master.json").read_bytes())
        (other / "execution/master.json").unlink()
        self.assertFalse(project.inspect(self.root)["ready"])
        with self.assertRaisesRegex(core.LeadkitError, "missing"):
            flow.snapshot(self.root, "blocked")
        self.assertFalse((self.root / "research/blocked").exists())

    def test_new_tasks_capture_latest_configuration_old_task_remains_frozen(self):
        core.init_workspace(self.root)
        first = core.read_json(flow.snapshot(self.root, "one")["batch_path"])
        config = core.read_json(self.root / "config.json")
        config["products"] = ["蓝牙耳机"]
        core.safe_write(self.root / "config.json", config)
        report = project.inspect(self.root)
        self.assertTrue(report["configuration_changed_since_binding"])
        second = core.read_json(flow.snapshot(self.root, "two")["batch_path"])
        self.assertEqual(second["task"]["products"], ["蓝牙耳机"])
        self.assertNotEqual(first["task"]["project_context"]["config_sha256"], second["task"]["project_context"]["config_sha256"])
        self.assertEqual(first, core.read_json(self.root / "research/one/batch.json"))
        master = core.read_json(self.root / "execution/master.json")
        master["updated_at"] = "changed by ordinary data transaction"
        core.safe_write(self.root / "execution/master.json", master)
        self.assertEqual(core.load_master(self.root)[0]["updated_at"], master["updated_at"])

    def test_task_override_cannot_forge_project_context(self):
        core.init_workspace(self.root)
        task = core.read_json(flow.snapshot(self.root, "one", {"project_context":{"net_new_verified":True}})["batch_path"])["task"]
        self.assertFalse(task["project_context"]["net_new_verified"])

    def test_company_buyer_preference_limits_to_task_profile(self):
        core.init_workspace(self.root)
        task = core.read_json(flow.snapshot(self.root, "one", {"customer_profile":{"buyer_types":["进口商"]}})["batch_path"])["task"]
        self.assertEqual(task["preferred_buyer_types"], ["进口商"])
        self.assertEqual(task["business_priorities"], ["buyer_type"])

    def test_excel_setup_keeps_unrelated_instructions_and_master(self):
        self.excel_project()
        original = "# Existing project\nKeep all handwritten notes.\n"
        (self.root / "AGENTS.md").write_text(original)
        master = (self.root / "old.xlsx").read_bytes()
        company = (self.root / "company.json").read_bytes()
        result = flow.setup_project(self.root, dedicated=True)
        self.assertEqual(Path(result["backup_path"]).read_text(), original)
        self.assertTrue((self.root / "AGENTS.md").read_text().startswith(original))
        self.assertEqual(master, (self.root / "old.xlsx").read_bytes())
        self.assertEqual(company, (self.root / "company.json").read_bytes())
        self.assertFalse(project.inspect(self.root)["configuration_changed_since_binding"])
        self.assertFalse(flow.setup_project(self.root, dedicated=True)["changed"])
        self.assertFalse((self.root / "execution/master.json").exists())

    def test_binding_disallows_outside_master_duplicate_source_and_invalid_excel(self):
        self.excel_project()
        with self.assertRaisesRegex(core.LeadkitError, "within"):
            project.bind(self.root, {"kind":"xlsx", "path":"../outside.xlsx"}, "company.json", "rules.md", replace=True, reason="test")
        with self.assertRaisesRegex(core.LeadkitError, "only once"):
            project.bind(self.root, {"kind":"xlsx", "path":"old.xlsx"}, "company.json", "rules.md", additional_sources=[{"kind":"xlsx","path":"old.xlsx"}], replace=True, reason="test")
        (self.root / "fake.xlsx").write_text("not Excel")
        with self.assertRaisesRegex(core.LeadkitError, "genuine"):
            project.bind(self.root, {"kind":"xlsx", "path":"fake.xlsx"}, "company.json", "rules.md", replace=True, reason="test")

    def test_unbound_folder_is_not_proof_of_empty_customer_history(self):
        self.assertFalse(project.inspect(self.root)["bound"])

    def test_existing_child_binding_cannot_be_shadowed_by_parent_configuration(self):
        self.root.mkdir()
        core.init_workspace(self.root / "child")
        core.safe_write(self.root / "company.json", {"products": core.PRODUCTS})
        with self.assertRaisesRegex(core.LeadkitError, "own binding"):
            project.bind(self.root, {"kind":"json", "path":"child/execution/master.json"}, "company.json", "company.json")
        self.assertFalse((self.root / project.BINDING).exists())

    def test_plain_historical_json_objects_and_lists_bind_without_importing(self):
        core.init_workspace(self.root)
        master_before = (self.root / "execution/master.json").read_bytes()
        documents = {"old-list.json": [{"customer": "Synthetic prior customer", "phone": "00012345"}],
                     "old-report.json": {"records": [{"id": "OLD-1", "name": "Synthetic prior customer"}], "region": "Test region"}}
        originals = {}
        for name, value in documents.items():
            core.safe_write(self.root / name, value)
            originals[name] = (self.root / name).read_bytes()
        project.bind(self.root, {"kind": "json", "path": "execution/master.json"},
                     additional_sources=[{"kind": "json", "path": name} for name in documents],
                     replace=True, reason="Review historical collection documents")
        report = project.inspect(self.root)
        self.assertTrue(report["ready"])
        self.assertTrue(report["history_review_required"])
        self.assertFalse(report["net_new_verified"])
        task = core.read_json(flow.snapshot(self.root, "review-history")["batch_path"])["task"]
        self.assertEqual({s["path"] for s in task["project_context"]["additional_sources"]}, set(documents))
        self.assertTrue(all(s["role"] == "dedupe_only" for s in task["project_context"]["additional_sources"]))
        self.assertEqual(master_before, (self.root / "execution/master.json").read_bytes())
        self.assertEqual({}, core.load_master(self.root)[0]["leads"])
        for name, original in originals.items():
            self.assertEqual(original, (self.root / name).read_bytes())

    def test_supplemental_scalar_and_broken_json_reject_without_changing_binding(self):
        core.init_workspace(self.root)
        binding_before = (self.root / project.BINDING).read_bytes()
        for content in ["null", "42", '"a string"', "true", '{"broken":']:
            with self.subTest(content=content):
                path = self.root / "bad-history.json"
                path.write_text(content, encoding="utf-8")
                with self.assertRaises((core.LeadkitError, ValueError)):
                    project.bind(self.root, {"kind": "json", "path": "execution/master.json"},
                                 additional_sources=[{"kind": "json", "path": path.name}],
                                 replace=True, reason="Try invalid input")
                self.assertEqual(binding_before, (self.root / project.BINDING).read_bytes())
                self.assertEqual(content, path.read_text(encoding="utf-8"))

    def test_plain_historical_json_still_cannot_be_authoritative_json_master(self):
        self.excel_project()
        (self.root / "execution").mkdir()
        binding_before = (self.root / project.BINDING).read_bytes()
        for value in [{"records": [{"name": "Synthetic"}]}, [{"id": "OLD-1"}], None]:
            with self.subTest(value=value):
                core.safe_write(self.root / "execution/master.json", value)
                with self.assertRaisesRegex(core.LeadkitError, "leadkit master"):
                    project.bind(self.root, {"kind": "json", "path": "execution/master.json"},
                                 "company.json", "rules.md", replace=True, reason="Try incompatible master")
                self.assertEqual(binding_before, (self.root / project.BINDING).read_bytes())


if __name__ == "__main__":
    unittest.main()
