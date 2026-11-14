#!/usr/bin/env python3
"""Bind a customer project to one authoritative local master and active rules.

No source data is copied, imported, uploaded or merged. Bindings use relative
paths and travel with a copied project; missing supplemental files remain visible.
"""
import argparse
import copy
import json
import os
import sys
import uuid
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import leadkit as core

BINDING = ".customer-development-project.json"
KINDS = {"json", "xlsx"}


def locate(start):
    root = Path(start).expanduser().resolve()
    for candidate in (root,) + tuple(root.parents):
        path = candidate / BINDING
        core.require(not path.is_symlink(), "Project binding may not be a symlink")
        if path.is_file():
            binding = core.read_json(path)
            core.require(binding.get("schema_version") == 1, "Unsupported project binding version")
            return candidate, binding
    return None, None


def _path(root, relative, label, external=False):
    root = Path(root).resolve()
    core.nonempty(relative, label)
    core.require(not Path(relative).is_absolute() and "\\" not in relative, label + " must use a relative path with forward slashes")
    lexical = root / relative
    core.require(not lexical.is_symlink(), label + " may not be a symlink")
    path = lexical.resolve()
    if not external:
        core.require(path == root or root in path.parents, label + " must remain within the bound project")
    core.require(path != root / BINDING, "Binding cannot refer to itself")
    return path


def _file(root, relative, label, external=False, required=True):
    path = _path(root, relative, label, external)
    if required:
        core.require(path.is_file(), label + " does not exist: " + str(path))
    return {"path": relative, "resolved_path": str(path), "exists": path.is_file(),
            "sha256": core.digest_bytes(path.read_bytes()) if path.is_file() else None}


def _validate_master(path, kind):
    core.require(kind in KINDS, "master.kind must be json or xlsx")
    if kind == "json":
        value = core.read_json(path)
        core.require(isinstance(value, dict) and value.get("schema_version") == 1 and all(isinstance(value.get(k), dict) for k in ("leads", "sources", "batches")), "JSON master must be a leadkit master, not a batch or summary")
    else:
        core.require(path.suffix.lower() == ".xlsx" and zipfile.is_zipfile(path), "Excel master must be a genuine .xlsx workbook")
        with zipfile.ZipFile(path) as archive:
            core.require("xl/workbook.xml" in archive.namelist(), "Excel master has no workbook definition")
            ET.fromstring(archive.read("xl/workbook.xml"))


def _validate_additional_source(path, kind):
    """Read-only historical documents need not use the writable master schema."""
    core.require(kind in KINDS, "additional_source.kind must be json or xlsx")
    if kind == "json":
        value = core.read_json(path)
        core.require(isinstance(value, (dict, list)), "Additional JSON source must be an object or array document; scalar values are not customer history")
    else:
        _validate_master(path, kind)


def _spec(root, master, config_path, rules_path, rules_version, additional_sources):
    core.require(isinstance(master, dict) and set(master) == {"kind", "path"}, "master needs kind and path")
    m = _file(root, master["path"], "master.path")
    _validate_master(Path(m["resolved_path"]), master["kind"])
    if master["kind"] == "json":
        core.require(Path(m["resolved_path"]).name == "master.json" and Path(m["resolved_path"]).parent.name == "execution", "JSON master must use <workspace>/execution/master.json")
    config = _file(root, config_path, "config_path")
    core.require(isinstance(core.read_json(config["resolved_path"]), dict), "Company configuration must be a JSON object")
    rules = _file(root, rules_path, "rules_path")
    core.nonempty(rules_version, "rules_version")
    core.require(isinstance(additional_sources, list), "additional_sources must be an array")
    sources, seen = [], {m["resolved_path"]}
    for item in additional_sources:
        core.require(isinstance(item, dict) and set(item) <= {"kind", "path", "label"}, "Additional source needs kind/path and optional label")
        f = _file(root, item.get("path"), "additional_source.path", external=True)
        core.require(f["resolved_path"] not in seen, "A source file may be bound only once")
        seen.add(f["resolved_path"])
        _validate_additional_source(Path(f["resolved_path"]), item.get("kind"))
        source = dict(item, role="dedupe_only", bound_sha256=f["sha256"])
        sources.append(source)
    return {"master": dict(master), "config_path": config_path, "rules_path": rules_path,
            "rules_version": rules_version, "additional_sources": sources,
            "configuration_sha256": config["sha256"], "rules_sha256": rules["sha256"]}


def bind(root, master, config_path="config.json", rules_path="config.json", rules_version=None,
         additional_sources=None, replace=False, reason=None):
    root = Path(root).expanduser().resolve()
    core.require(root.is_dir(), "Project directory must already exist")
    ancestor, _ = locate(root.parent)
    core.require(ancestor is None, "A parent project is already bound; use its binding instead of creating a second master: " + str(ancestor))
    with core.locked(root):
        path = root / BINDING
        core.require(not path.is_symlink(), "Project binding may not be a symlink")
        spec = _spec(root, master, config_path, rules_path, rules_version or core.VERSION, additional_sources or [])
        if master["kind"] == "json":
            workspace = _path(root, master["path"], "master.path").parent.parent
            core.require(workspace == root or not (workspace / BINDING).exists(), "The JSON workspace already has its own binding; use that project root instead of shadowing its configuration")
        old = core.read_json(path) if path.exists() else None
        if old and all(old.get(k) == v for k, v in spec.items()):
            return {"binding_path": str(path), "changed": False, "project_id": old["project_id"]}
        if old:
            core.require(replace, "Binding already exists; use --replace with a reason for a reviewed change")
            core.nonempty(reason, "Binding changes require a reason")
        history = copy.deepcopy((old or {}).get("history", []))
        if old:
            previous = {k: v for k, v in old.items() if k != "history"}
            history.append({"changed_at": core.now(), "reason": reason, "previous": previous})
        binding = dict(spec, schema_version=1, project_id=(old or {}).get("project_id", uuid.uuid4().hex),
                       created_at=(old or {}).get("created_at", core.now()), updated_at=core.now(), history=history)
        core.safe_write(path, binding)
    return {"binding_path": str(path), "changed": True, "project_id": binding["project_id"], "master_kind": master["kind"], "source_data_modified": False}


def bind_default_unlocked(root):
    """Only called by init while it owns the workspace lock."""
    spec = _spec(root, {"kind": "json", "path": "execution/master.json"}, "config.json", "config.json", core.VERSION, [])
    core.safe_write(root / BINDING, dict(spec, schema_version=1, project_id=uuid.uuid4().hex,
                    created_at=core.now(), updated_at=core.now(), history=[]))


def guard_init(root):
    bound_root, binding = locate(root)
    core.require(binding is None, "init refuses to create another customer database in an existing project; use bound project: " + str(bound_root))
    for parent in root.parents:
        if (parent / "execution/master.json").is_file() and (parent / "config.json").is_file():
            raise core.LeadkitError("init refuses a nested empty database; existing customer workspace: " + str(parent))


def guard_json_workspace(root):
    bound_root, binding = locate(root)
    if binding is None:
        return None
    master = binding["master"]
    path = _path(bound_root, master["path"], "master.path")
    core.require(master["kind"] == "json", "This project uses an Excel master; read/update the bound workbook with spreadsheet tools, do not initialize a second JSON master: " + str(path))
    core.require(path == (root / "execution/master.json").resolve(), "Use the authoritative JSON workspace: " + str(path.parent.parent))
    return bound_root, binding


def inspect(start):
    root, binding = locate(start)
    if binding is None:
        return {"bound": False, "next_action": "Locate the existing customer master and active rules, then bind this project. An unbound folder does not prove there are no historical customers."}
    master = _file(root, binding["master"]["path"], "master.path", required=False)
    config = _file(root, binding["config_path"], "config_path", required=False)
    rules = _file(root, binding["rules_path"], "rules_path", required=False)
    additional = [dict(item, **{k: v for k, v in _file(root, item["path"], "additional_source.path", external=True, required=False).items() if k != "path"}) for item in binding.get("additional_sources", [])]
    missing = [x["resolved_path"] for x in [master, config, rules] + additional if not x["exists"]]
    return {"bound": True, "project_root": str(root), "binding_path": str(root / BINDING), "project_id": binding["project_id"],
            "master": dict(master, kind=binding["master"]["kind"]), "workspace": str(Path(master["resolved_path"]).parent.parent) if binding["master"]["kind"] == "json" else None,
            "company_config": config, "active_rules": dict(rules, version=binding["rules_version"]), "additional_sources": additional,
            "missing_paths": missing, "ready": not missing,
            "configuration_changed_since_binding": config["sha256"] != binding.get("configuration_sha256"),
            "rules_changed_since_binding": rules["sha256"] != binding.get("rules_sha256"),
            "net_new_verified": False, "history_review_required": bool(additional),
            "next_action": "Read the bound master, effective company configuration and rules; compare all additional sources before claiming company-wide net-new customers. Binding is not data synchronization."}


def active_context(root):
    """Capture actual inputs in each new task, without changing older tasks."""
    found = guard_json_workspace(root)
    if found is None:
        return root / "config.json", {"bound": False, "history_scope": "local_master_only"}
    report = inspect(root)
    core.require(report["ready"], "Bound project files are missing; restore them before new research: " + ", ".join(report["missing_paths"]))
    context = {"project_id": report["project_id"], "rules_version": report["active_rules"]["version"], "runtime_rules_version": core.VERSION,
               "rules_path": report["active_rules"]["path"], "rules_sha256": report["active_rules"]["sha256"],
               "config_path": report["company_config"]["path"], "config_sha256": report["company_config"]["sha256"],
               "additional_sources": [{"kind": s["kind"], "path": s["path"], "sha256": s["sha256"], "role": "dedupe_only"} for s in report["additional_sources"]],
               "binding_path": BINDING, "net_new_verified": False,
               "history_scope": "primary_and_additional_review_required" if report["additional_sources"] else "local_master_only"}
    return Path(report["company_config"]["resolved_path"]), context


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    read = commands.add_parser("inspect")
    read.add_argument("--project", required=True)
    write = commands.add_parser("bind")
    write.add_argument("--project", required=True)
    write.add_argument("--master", required=True, help="Relative authoritative master path")
    write.add_argument("--kind", required=True, choices=sorted(KINDS))
    write.add_argument("--config", default="config.json")
    write.add_argument("--rules", default="config.json")
    write.add_argument("--rules-version", default=core.VERSION)
    write.add_argument("--additional-sources", help="JSON array of kind/path/label read-only sources")
    write.add_argument("--replace", action="store_true")
    write.add_argument("--reason")
    args = parser.parse_args()
    try:
        root = core.workspace_path(args.project)
        result = inspect(root) if args.command == "inspect" else bind(root, {"kind": args.kind, "path": args.master}, args.config, args.rules, args.rules_version,
                            core.read_json(args.additional_sources) if args.additional_sources else None, args.replace, args.reason)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (core.LeadkitError, OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
