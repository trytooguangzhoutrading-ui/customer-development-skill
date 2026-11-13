#!/usr/bin/env python3
"""Local checkpoint, batch preparation and employee correction round trips.

No network calls, outreach, or third-party packages. Incomplete drafts never
become customer records until leadkit.commit accepts their evidence.
"""
import argparse
import copy
import hashlib
import json
import re
import secrets
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import leadkit as core

HEADERS = ["客户编号", "商户名称（只读）", "联系入口编号（只读）", "人工备注", "需复核事项", "失效联系入口编号", "重新采集"]
META_HEADERS = ["项目", "值"]


def merge_dict(base, changes):
    result = copy.deepcopy(base)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_dict(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def snapshot(root, batch_id, overrides=None):
    """Company defaults then explicit task overrides; never infer a previous task."""
    config = core.read_json(root / "config.json")
    overrides = {} if overrides is None else overrides
    core.require(isinstance(overrides, dict), "Task overrides must be an object")
    defaults = config.get("task_defaults", {})
    core.require(isinstance(defaults, dict), "config.task_defaults must be an object")
    products = config.get("products", core.PRODUCTS)
    buyers = config.get("default_buyer_types", core.DEFAULT_BUYER_TYPES)
    core.require(isinstance(products, list) and products and set(products) <= set(core.PRODUCTS), "Invalid company products")
    core.require(isinstance(buyers, list) and buyers and all(isinstance(x, str) and x.strip() for x in buyers), "Invalid company buyer types")
    supply = config.get("supplier_profile", config.get("supplier_constraints", {}))
    core.require(isinstance(supply, dict), "config.supplier_profile must be an object")
    task = {"products": products, "customer_profile": {"buyer_types": buyers, "brand_policy": "white_label_required", "supplier_constraints": supply}, "activity_max_age_days": config.get("activity_max_age_days", 180)}
    region = config.get("default_region")
    if isinstance(region, str) and region.strip():
        task["countries"] = [region]
    elif isinstance(region, dict):
        task.update(copy.deepcopy(region))
        if "country" in task:
            task["countries"] = [task.pop("country")]
    elif isinstance(region, list):
        core.require(all(isinstance(x, dict) and x.get("country") for x in region), "default_region list needs country objects")
        task["geo_scope"] = copy.deepcopy(region)
        task["countries"] = list(dict.fromkeys(x["country"] for x in region))
    else:
        core.require(region is None, "default_region must be a country string, object, list or null")
    task = merge_dict(task, defaults)
    # A new country/city scope must not inherit narrower limits of another task.
    if "countries" in overrides or "geo_scope" in overrides:
        for key in ("city", "market", "address", "geo_scope"):
            if key not in overrides:
                task.pop(key, None)
    elif "city" in overrides:
        for key in ("market", "address", "geo_scope"):
            if key not in overrides:
                task.pop(key, None)
    task = merge_dict(task, overrides)
    if "geo_scope" in overrides and "countries" not in overrides:
        task["countries"] = list(dict.fromkeys(x["country"] for x in overrides["geo_scope"]))
    basic = getattr(core, "basic_snapshot", None)
    core.require(callable(basic), "Runtime requires basic_snapshot from leadkit 1.2")
    result = basic(root, batch_id)
    with core.locked(root):
        path = Path(result["batch_path"])
        packet = core.read_json(path)
        packet["task"] = merge_dict(packet["task"], task)
        core.safe_write(path, packet)
    result["inherited_config"] = True
    return result


def _draft_path(root, batch_id):
    core.valid_id(batch_id, "batch_id", core.BATCH_RE)
    path = root / "research" / batch_id
    core.require(path.is_dir() and not path.is_symlink(), "Create a snapshot before checkpointing")
    return path


def checkpoint(root, packet):
    core.require(isinstance(packet, dict), "Draft must be an object")
    batch_id = packet.get("batch_id")
    path = _draft_path(root, batch_id)
    with core.locked(root):
        master, sha = core.load_master(root)
        core.require(batch_id not in master["batches"], "Committed batches are immutable; start a new batch")
        baseline = core.read_json(path / "snapshot.json")
        core.require(packet.get("base_sha256") == baseline["base_sha256"], "Draft base must match its original snapshot")
        digest = core.digest_bytes(core.encoded(packet))
        history = path / "checkpoints"
        core.require(not history.is_symlink(), "Checkpoint directory may not be a symlink")
        history.mkdir(exist_ok=True)
        revision = history / (digest + ".json")
        if revision.exists():
            core.require(revision.read_bytes() == core.encoded(packet), "Checkpoint hash collision or corruption")
        else:
            core.safe_write(revision, packet)
        receipt = {"batch_id": batch_id, "packet_sha256": digest, "saved_at": core.now(), "base_matches_current": sha == baseline["base_sha256"], "committed": False}
        core.safe_write(path / "checkpoint.json", receipt)
        core.safe_write(path / "batch.json", packet)
    return dict(receipt, batch_path=str(path / "batch.json"), revision_path=str(revision))


def resume(root, batch_id):
    path = _draft_path(root, batch_id)
    with core.locked(root):
        master, sha = core.load_master(root)
        baseline = core.read_json(path / "snapshot.json")
        if batch_id in master["batches"]:
            return {"batch_id": batch_id, "committed": True, "next_action": "export the saved batch; do not research it again", "base_matches_current": False}
        receipt_path = path / "checkpoint.json"
        if receipt_path.exists():
            receipt = core.read_json(receipt_path)
            revision = path / "checkpoints" / (receipt["packet_sha256"] + ".json")
            raw = revision.read_bytes()
            core.require(core.digest_bytes(raw) == receipt["packet_sha256"], "Checkpoint integrity failure")
            packet = json.loads(raw)
        else:
            packet = core.read_json(path / "batch.json")
        core.require(packet.get("batch_id") == batch_id and packet.get("base_sha256") == baseline["base_sha256"], "Draft does not match snapshot")
        # Expose checkpoint contents, never overwrite newer unsaved working edits.
        return {"batch_id": batch_id, "committed": False, "base_matches_current": sha == baseline["base_sha256"], "requires_reconciliation": sha != baseline["base_sha256"], "packet": packet, "working_path": str(path / "batch.json")}


def _subset(packet, leads):
    result = copy.deepcopy(packet)
    result["leads"] = copy.deepcopy(leads)
    ids = {x.get("lead_id") for x in leads if isinstance(x, dict)}
    for cell in result.get("coverage", []):
        if isinstance(cell, dict):
            cell["discovered_lead_ids"] = [x for x in cell.get("discovered_lead_ids", []) if x in ids]
            if cell.get("status") == "searched":
                found = set(cell["discovered_lead_ids"])
                qualified = any(x.get("lead_id") in found and x.get("status") == "main" and cell.get("product") in ({e.get("product") for e in x.get("product_evidence", []) if isinstance(e, dict)} & {e.get("product") for e in x.get("white_label_evidence", []) if isinstance(e, dict)}) for x in leads if isinstance(x, dict))
                if not qualified:
                    cell["status"] = "searched_zero"
                    cell.setdefault("limitations", []).append("本准备文件仅统计保留记录，当前格无合格客户；完整原始覆盖见 original.json。")
    result["queue"] = [q for q in result.get("queue", []) if isinstance(q, dict) and (not q.get("lead_id") or q["lead_id"] in ids)]
    return result


def prepare(root, packet, output_dir):
    """Isolate invalid records, preserving originals and a repair ledger on disk."""
    core.require(isinstance(packet, dict), "Batch must be an object")
    output_dir = Path(output_dir).expanduser().absolute()
    core.require(not output_dir.exists() and not output_dir.is_symlink(), "prepare output directory must be new")
    with core.locked(root):
        master, sha = core.load_master(root)
        validate = getattr(core, "prepare_validation", core.validate_packet)
        errors, accepted, rejected = [], [], []
        # A bad shared task, source registry or region cannot be isolated per lead.
        try:
            core.require(packet.get("base_sha256") == sha, "Master changed; reconcile before preparing")
            for field in ("leads", "queue", "coverage"):
                core.require(isinstance(packet.get(field), list), field + " must be an array")
            core.require(all(isinstance(q, dict) for q in packet["queue"]), "queue items must be objects")
            core.require(all(isinstance(c, dict) for c in packet["coverage"]), "coverage items must be objects")
            original_ids = {x["lead_id"] for x in packet["leads"] if isinstance(x, dict) and isinstance(x.get("lead_id"), str)}
            for item in packet["queue"]:
                if item.get("lead_id"):
                    core.require(isinstance(item["lead_id"], str) and item["lead_id"] in original_ids | set(master["leads"]), "queue references lead outside original packet and master")
            for cell in packet["coverage"]:
                core.string_list(cell.get("discovered_lead_ids", []), "coverage.discovered_lead_ids")
                core.require(set(cell.get("discovered_lead_ids", [])) <= original_ids, "coverage references lead outside original packet")
            shared = _subset(packet, [])
            shared_sources = core.validate_packet(shared, master)
            core.validate_issues(packet["queue"], shared_sources, "queue")
        except (core.LeadkitError, ValueError, TypeError, KeyError) as exc:
            errors.append(str(exc))
        raw_leads = packet.get("leads", [])
        if not isinstance(raw_leads, list):
            errors.append("leads must be an array")
            raw_leads = []
        if not errors:
            duplicates = {x.get("lead_id") for x in raw_leads if isinstance(x, dict) and sum(isinstance(y, dict) and y.get("lead_id") == x.get("lead_id") for y in raw_leads) > 1}
            for index, lead in enumerate(raw_leads):
                try:
                    core.require(isinstance(lead, dict), "Lead must be an object")
                    core.require(lead.get("lead_id") not in duplicates, "Repeated lead_id: reconcile rows instead of choosing one")
                    validate(_subset(packet, accepted + [lead]), master)
                    accepted.append(lead)
                except (core.LeadkitError, ValueError, TypeError, KeyError) as exc:
                    rejected.append({"index": index, "lead_id": lead.get("lead_id") if isinstance(lead, dict) else None, "error": str(exc), "record": copy.deepcopy(lead)})
        else:
            rejected = [{"index": i, "lead_id": x.get("lead_id") if isinstance(x, dict) else None, "error": "Shared validation blocked", "record": copy.deepcopy(x)} for i, x in enumerate(raw_leads)]
        prepared = _subset(packet, accepted) if not errors else copy.deepcopy(packet)
        repairs = []
        for index, item in enumerate(rejected):
            issue = {"issue_id": "REPAIR-" + core.digest_bytes(core.encoded([packet.get("batch_id"), item["index"], item["record"]]))[:20], "kind": "validation", "detail": item["error"], "state": "open", "next_action": "Correct the preserved rejected record, then prepare a new batch against the current baseline", "attempts": 1, "next_trigger": "record_corrected", "source_ids": []}
            if isinstance(item["lead_id"], str) and core.ID_RE.fullmatch(item["lead_id"]):
                issue["lead_id"] = item["lead_id"]
            repairs.append(issue)
        if not errors:
            prepared["limitations"] = list(prepared.get("limitations", [])) + (["预验证隔离 %d 条记录；原始记录与修复队列见 prepare 输出，未计入本轮正式客户。" % len(rejected)] if rejected else [])
        # Keep recoverable links in the committed queue even when the failed
        # record has no valid/stored customer ID yet.
        for repair in repairs if not errors else []:
            pointer = copy.deepcopy(repair)
            rejected_id = pointer.pop("lead_id", None)
            pointer["detail"] = "隔离记录 %s：%s" % (rejected_id or "编号待修复", pointer["detail"])
            pointer["next_action"] = "读取 %s 中保留的原始记录，修复后按当前基线建立新批次" % (output_dir / "report.json")
            prepared.setdefault("queue", []).append(pointer)
        if not errors:
            validate(prepared, master)
        report = {"batch_id": packet.get("batch_id"), "base_sha256": sha, "input_sha256": core.digest_bytes(core.encoded(packet)), "shared_errors": errors, "input_count": len(raw_leads), "accepted_count": len(accepted), "rejected_count": len(rejected), "ready_to_commit": not errors and (bool(accepted) or not raw_leads), "complete": not errors and not rejected, "committed": False, "rejected": rejected, "repair_queue": repairs}
        output_dir.mkdir(parents=True)
        core.safe_write(output_dir / "original.json", packet)
        core.safe_write(output_dir / "report.json", report)
        core.safe_write(output_dir / "repair-queue.json", repairs)
        if not errors:
            core.safe_write(output_dir / "prepared.json", prepared)
    return {k: v for k, v in report.items() if k not in {"rejected", "repair_queue"}} | {"report_path": str(output_dir / "report.json"), "prepared_path": str(output_dir / "prepared.json") if not errors else None}


def _contact_id(contact):
    return contact.get("contact_id") or "LEGACY-" + core.digest_bytes(core.encoded([contact.get("kind"), contact.get("value")]))[:20]


def _mkdir(path):
    core.require(not path.is_symlink(), "Refusing symlink directory")
    path.mkdir(exist_ok=True)
    return path


def correction_template(root, output):
    with core.locked(root):
        master, sha = core.load_master(root)
        token = secrets.token_hex(20)
        registry = _mkdir(root / "execution" / "correction_templates")
        baseline = {}
        rows = [HEADERS]
        for lid, lead in sorted(master["leads"].items()):
            contact_text = "\n".join("%s | %s | %s" % (_contact_id(c), c.get("kind", ""), c.get("value", "")) for c in lead.get("contacts", []))
            row = [lid, lead["name"], contact_text, lead.get("manual_notes", ""), "", "", ""]
            baseline[lid] = row
            rows.append(row)
        meta = [META_HEADERS, ["template_token", token], ["base_sha256", sha], ["说明", "只修改人工备注、需复核事项、失效联系入口编号、重新采集。重新采集填写是或留空；失效入口填编号，多项用换行或逗号分隔。客户编号和只读列不可改；公式不可用。"]]
        core.write_xlsx(output, [("采集纠错", rows, [22, 30, 66, 42, 50, 38, 18]), ("模板说明", meta, [28, 100])])
        record = {"token": token, "base_sha256": sha, "created_at": core.now(), "rows": baseline, "columns": HEADERS}
        core.safe_write(registry / (token + ".json"), record)
    return {"output": str(Path(output).resolve()), "template_token": token, "base_sha256": sha, "rows": len(baseline)}


def _read_workbook(path):
    """Read literal cells from our two-sheet template (Excel shared strings allowed)."""
    result = {}
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            core.require(len(names) == len(set(names)), "Duplicate workbook ZIP members")
            core.require(sum(x.file_size for x in archive.infolist()) <= 32 * 1024 * 1024, "Correction workbook is too large")
            shared = []
            if "xl/sharedStrings.xml" in names:
                sst = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                shared = ["".join(n.text or "" for n in item.iter(core.tag("t"))) for item in sst]
            book = ET.fromstring(archive.read("xl/workbook.xml"))
            rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            targets = {}
            for rel in rels:
                core.require(rel.get("TargetMode") != "External", "External workbook relationships are not supported")
                target = rel.get("Target", "")
                targets[rel.get("Id")] = target.lstrip("/") if target.startswith("/") else "xl/" + target
            sheets = book.find(core.tag("sheets"))
            core.require(sheets is not None, "Workbook has no sheets")
            for sheet in sheets:
                filename = targets.get(sheet.get("{" + core.REL + "}id"))
                core.require(filename in names and ".." not in Path(filename).parts, "Invalid sheet path")
                xml = ET.fromstring(archive.read(filename))
                core.require(not list(xml.iter(core.tag("f"))), "Formulas are not permitted in correction files; paste literal values")
                rows = []
                data = xml.find(core.tag("sheetData"))
                for row in data if data is not None else []:
                    cells = {}
                    for cell in row:
                        address = cell.get("r", "")
                        match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", address)
                        core.require(match is not None, "Invalid cell coordinate")
                        column = 0
                        for letter in match[1]:
                            column = column * 26 + ord(letter) - 64
                        core.require(column <= 100, "Unexpected correction columns")
                        core.require(column not in cells, "Duplicate cell coordinate")
                        kind = cell.get("t", "n")
                        if kind == "inlineStr":
                            value = "".join(t.text or "" for t in cell.iter(core.tag("t")))
                        else:
                            raw = cell.findtext(core.tag("v"), "")
                            if kind == "s":
                                core.require(raw.isdigit() and int(raw) < len(shared), "Invalid shared-string index")
                                value = shared[int(raw)]
                            else:
                                core.require(kind in {"str", "n"}, "Unsupported cell type")
                                value = raw
                        cells[column] = value
                    rows.append([cells.get(i, "") for i in range(1, max(cells, default=0) + 1)])
                core.require(sheet.get("name") not in result, "Repeated sheet name")
                result[sheet.get("name")] = rows
    except (OSError, zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
        raise core.LeadkitError("Cannot read correction workbook: %s" % exc) from exc
    return result


def _correction_plan(root, path, master, sha):
    workbook = _read_workbook(path)
    core.require(set(workbook) == {"采集纠错", "模板说明"}, "Use the exported correction template and keep both sheets")
    meta_rows = workbook["模板说明"]
    core.require(meta_rows and meta_rows[0] == META_HEADERS, "Template metadata header changed")
    pairs = [r for r in meta_rows[1:] if any(r)]
    core.require(all(len(r) == 2 for r in pairs) and len({r[0] for r in pairs}) == len(pairs), "Invalid or duplicated metadata")
    meta = dict(pairs)
    token = meta.get("template_token", "")
    core.require(re.fullmatch(r"[a-f0-9]{40}", token) is not None, "Invalid template token")
    record = core.read_json(root / "execution" / "correction_templates" / (token + ".json"))
    core.require(meta.get("base_sha256") == record["base_sha256"], "Template baseline was altered")
    core.require(sha == record["base_sha256"], "Template is stale; export a fresh template and reapply intended edits")
    rows = workbook["采集纠错"]
    core.require(rows and rows[0] == HEADERS, "Correction column names or order changed")
    seen, changes = set(), []
    for raw in rows[1:]:
        if not any(raw):
            continue
        core.require(len(raw) <= len(HEADERS), "Extra correction columns are not permitted")
        row = raw + [""] * (len(HEADERS) - len(raw))
        lid = row[0]
        core.require(lid in record["rows"] and lid in master["leads"] and lid not in seen, "Unknown or duplicated customer ID")
        seen.add(lid)
        base = record["rows"][lid]
        core.require(row[:3] == base[:3], "Customer ID and read-only identification columns must not change")
        core.require(row[6] in {"", "是", "否"}, "重新采集 accepts 是, 否 or blank")
        invalid = [x.strip() for x in re.split(r"[,，;；\n]", row[5]) if x.strip()]
        contacts = {_contact_id(c) for c in master["leads"][lid].get("contacts", [])}
        core.require(set(invalid) <= contacts, "Invalid contact ID is not owned by this customer")
        change = {"lead_id": lid, "before_manual_notes": base[3], "manual_notes": row[3], "review_detail": row[4].strip(), "invalid_contact_ids": sorted(set(invalid)), "resample": row[6] == "是"}
        if row[3] != base[3] or change["review_detail"] or invalid or change["resample"]:
            changes.append(change)
    core.require(seen == set(record["rows"]), "Correction template rows were removed; keep all customer rows")
    return {"template_token": token, "base_sha256": sha, "workbook_sha256": core.digest_bytes(Path(path).read_bytes()), "changes": changes}


def corrections_preview(root, path):
    with core.locked(root):
        master, sha = core.load_master(root)
        plan = _correction_plan(root, path, master, sha)
        preview_id = core.digest_bytes(core.encoded(plan))
        directory = _mkdir(root / "execution" / "correction_previews")
        target = directory / (preview_id + ".json")
        if target.exists():
            core.require(core.read_json(target) == plan, "Preview mismatch")
        else:
            core.safe_write(target, plan)
    return dict(plan, preview_id=preview_id, changed_count=len(plan["changes"]), applied=False)


def corrections_apply(root, path, preview_id):
    core.require(re.fullmatch(r"[a-f0-9]{64}", preview_id or "") is not None, "Use preview_id returned by corrections-preview")
    with core.locked(root):
        master, sha = core.load_master(root)
        registry = master.setdefault("correction_receipts", {})
        fingerprint = core.digest_bytes(Path(path).read_bytes())
        if preview_id in registry:
            core.require(registry[preview_id]["workbook_sha256"] == fingerprint, "Applied preview cannot be reused for another file")
            return dict(registry[preview_id], already_applied=True)
        preview = core.read_json(root / "execution" / "correction_previews" / (preview_id + ".json"))
        core.require(core.digest_bytes(core.encoded(preview)) == preview_id, "Preview integrity failed")
        plan = _correction_plan(root, path, master, sha)
        core.require(plan == preview, "Correction file or baseline changed after preview; preview it again")
        stamp = core.now()
        for change in plan["changes"]:
            lead = master["leads"][change["lead_id"]]
            old = copy.deepcopy(lead)
            lead.setdefault("history", []).append({k: v for k, v in old.items() if k != "history"})
            lead["manual_notes"] = change["manual_notes"]
            invalid = set(change["invalid_contact_ids"])
            for contact in lead.get("contacts", []):
                if _contact_id(contact) in invalid:
                    contact.update(status="invalid", invalidated_at=stamp, invalidation_origin="employee_correction", invalidation_preview_id=preview_id)
            remaining_contact = any(core.actionable_contact(contact, master["sources"]) for contact in lead.get("contacts", []))
            needs_review = bool(change["review_detail"] or change["resample"] or (invalid and not remaining_contact))
            if needs_review or invalid:
                issue = {"issue_id": "CORR-" + core.digest_bytes(core.encoded([preview_id, change["lead_id"]]))[:20], "kind": "employee_correction" if needs_review else "contact_correction", "detail": change["review_detail"] or ("员工标记联系入口失效，需补核可用入口" if invalid else "员工要求重新采集"), "state": "open", "next_action": "依据员工纠错重新核验客户及联系入口，补证后明确关闭本问题", "attempts": 0, "next_trigger": "next_collection", "source_ids": [], "decision_origin": "user", "created_at": stamp}
                lead.setdefault("issues", []).append(issue)
                if needs_review:
                    for qualification in lead.get("qualifications", {}).values():
                        if qualification.get("status") == "main":
                            qualification.update(status="candidate", priority="", reason="员工纠错后待重新核验", updated_at=stamp)
                    if lead.get("status") == "main":
                        lead.update(status="candidate", last_task_status="candidate", priority="", priority_reason="员工纠错后待重新核验")
            lead["updated_at"] = stamp
            lead.setdefault("correction_history", []).append(dict(change, preview_id=preview_id, applied_at=stamp, origin="employee_correction"))
        receipt = {"preview_id": preview_id, "template_token": plan["template_token"], "workbook_sha256": fingerprint, "changed_count": len(plan["changes"]), "applied_at": stamp}
        registry[preview_id] = receipt
        master["updated_at"] = stamp
        backup = core.backup_master(root, sha)
        core.safe_write(root / "execution" / "master.json", master)
        saved, saved_sha = core.load_master(root)
        core.require(saved["correction_receipts"][preview_id] == receipt, "Correction read-back failed")
    return dict(receipt, already_applied=False, backup_path=str(backup), master_sha256=saved_sha)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("snapshot", "checkpoint", "resume", "prepare", "correction-template", "corrections-preview", "corrections-apply"):
        sub = subs.add_parser(name)
        sub.add_argument("--workspace", required=True)
        if name in {"snapshot", "resume"}:
            sub.add_argument("--batch-id", required=True)
        if name == "snapshot":
            sub.add_argument("--overrides", help="JSON object containing explicit task overrides")
        if name in {"checkpoint", "prepare"}:
            sub.add_argument("--batch", required=True)
        if name == "prepare":
            sub.add_argument("--output-dir", required=True)
        if name == "correction-template":
            sub.add_argument("--output", required=True)
        if name in {"corrections-preview", "corrections-apply"}:
            sub.add_argument("--input", required=True)
        if name == "corrections-apply":
            sub.add_argument("--preview-id", required=True)
    args = parser.parse_args()
    try:
        root = core.workspace_path(args.workspace)
        if args.command == "snapshot":
            result = snapshot(root, args.batch_id, core.read_json(args.overrides) if args.overrides else None)
        elif args.command == "checkpoint":
            result = checkpoint(root, core.read_json(args.batch))
        elif args.command == "resume":
            result = resume(root, args.batch_id)
        elif args.command == "prepare":
            result = prepare(root, core.read_json(args.batch), args.output_dir)
        elif args.command == "correction-template":
            result = correction_template(root, args.output)
        elif args.command == "corrections-preview":
            result = corrections_preview(root, args.input)
        else:
            result = corrections_apply(root, args.input, args.preview_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (core.LeadkitError, OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
