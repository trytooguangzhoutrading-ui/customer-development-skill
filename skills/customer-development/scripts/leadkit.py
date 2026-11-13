#!/usr/bin/env python3
"""Local customer research ledger and XLSX export. No network or AI API calls.

These checks validate structure and declared evidence links, not factual truth.
Codex must read actual sources and apply the skill's research rules.
"""
import argparse
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

VERSION = "1.1.0"
PRODUCTS = ["智能眼镜", "智能手表", "智能手环", "智能戒指", "蓝牙耳机", "蓝牙音响"]
LEVELS = {"current_original", "cached_original", "third_party_original", "search_snippet", "failed_read"}
STRONG = {"current_original", "cached_original", "third_party_original"}
CONTACTS = {"phone", "whatsapp", "email", "website", "form", "facebook", "instagram", "telegram", "tiktok", "youtube", "linkedin", "zalo", "chat", "other"}
PLATFORMS = {"facebook.com", "instagram.com", "t.me", "telegram.me", "youtube.com", "youtu.be", "tiktok.com", "linkedin.com", "wa.me", "whatsapp.com", "google.com", "maps.google.com"}
BRANDS = {"white_label", "mixed", "brand_owner", "official_store", "brand_exclusive", "unknown"}
BRAND_LABELS = {"white_label": "白牌经营", "mixed": "品牌／白牌混合经营", "brand_owner": "品牌方", "official_store": "官方品牌店", "brand_exclusive": "品牌专卖店", "unknown": "品牌类型待核"}
EXCLUDED_BRANDS = {"brand_owner", "official_store", "brand_exclusive"}
CAPABILITIES = {"wholesale", "import", "distribution", "sales_capacity"}
CURRENT = {"current_original", "third_party_original"}
DEFAULT_BUYER_TYPES = ["批发商", "进口商", "分销商", "当地市场商户", "相关线上商家", "相关零售商"]
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
BATCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKGREL = "http://schemas.openxmlformats.org/package/2006/relationships"
ET.register_namespace("", NS)
ET.register_namespace("r", REL)


class LeadkitError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def digest_bytes(value):
    return hashlib.sha256(value).hexdigest()


def require(condition, message):
    if not condition:
        raise LeadkitError(message)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LeadkitError("Cannot read JSON %s: %s" % (path, exc)) from exc


def safe_write(path, value):
    """Atomic replacement, called only while holding the workspace lock."""
    path = Path(path)
    require(not path.is_symlink(), "Refusing symlink: %s" % path)
    fd, temporary = tempfile.mkstemp(prefix=".leadkit-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def workspace_path(value):
    p = Path(value).expanduser().absolute()
    require(not p.is_symlink(), "Workspace may not be a symlink")
    # Resolve parents for consistent lock placement.
    return p.resolve()


@contextmanager
def locked(root):
    lock = root / ".leadkit-lock"
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise LeadkitError("Workspace is locked. Do not remove the lock while another writer runs: %s" % lock) from exc
    try:
        (lock / "owner.json").write_bytes(encoded({"pid": os.getpid(), "created_at": now()}))
        yield
    finally:
        (lock / "owner.json").unlink(missing_ok=True)
        lock.rmdir()


def load_master(root):
    path = root / "execution" / "master.json"
    require(not path.is_symlink(), "Master may not be a symlink")
    require(path.is_file(), "Workspace is not initialized; run init in a new workspace")
    raw = path.read_bytes()
    master = json.loads(raw)
    require(master.get("schema_version") == 1, "Unsupported master schema")
    return master, digest_bytes(raw)


def init_workspace(root):
    root.mkdir(parents=True, exist_ok=True)
    with locked(root):
        reserved = [root / "config.json", root / "execution", root / "research", root / "outputs"]
        require(not any(p.exists() or p.is_symlink() for p in reserved), "init refuses to overwrite an existing project; choose a new workspace")
        for name in ("execution", "research", "outputs"):
            (root / name).mkdir()
        config = {"schema_version": 1, "products": PRODUCTS, "default_buyer_types": DEFAULT_BUYER_TYPES, "brand_policy": "white_label_required", "outreach_enabled": False, "employee_output": "xlsx", "default_region": None, "rules_version": VERSION}
        safe_write(root / "config.json", config)
        master = {"schema_version": 1, "created_at": now(), "updated_at": now(), "leads": {}, "sources": {}, "batches": {}}
        safe_write(root / "execution" / "master.json", master)
    return {"workspace": str(root), "master": str(root / "execution" / "master.json"), "created": True}


def valid_id(value, label, pattern=ID_RE):
    require(isinstance(value, str) and pattern.fullmatch(value), "Invalid %s: use a stable ASCII ID" % label)


def snapshot(root, batch_id):
    valid_id(batch_id, "batch_id", BATCH_RE)
    with locked(root):
        master, sha = load_master(root)
        require(batch_id not in master["batches"], "Batch already committed")
        target = root / "research" / batch_id
        require(not target.exists(), "Batch directory already exists; reuse its saved snapshot or choose a new ID")
        target.mkdir()
        baseline = {"schema_version": 1, "batch_id": batch_id, "base_sha256": sha, "created_at": now(), "lead_ids": sorted(master["leads"])}
        safe_write(target / "snapshot.json", baseline)
        packet = {"schema_version": 2, "batch_id": batch_id, "base_sha256": sha, "task": {"request": "", "countries": [], "products": PRODUCTS, "requirements": [], "allow_channel_candidates": False, "customer_profile": {"buyer_types": DEFAULT_BUYER_TYPES, "brand_policy": "white_label_required", "supplier_constraints": {}}}, "region_research": [], "sources": [], "leads": [], "queue": [], "limitations": []}
        safe_write(target / "batch.json", packet)
    return {"batch_id": batch_id, "base_sha256": sha, "batch_path": str(target / "batch.json"), "snapshot_path": str(target / "snapshot.json")}


def nonempty(value, label):
    require(isinstance(value, str) and bool(value.strip()), "%s must be a nonempty string" % label)


def string_list(value, label):
    require(isinstance(value, list) and all(isinstance(x, str) for x in value), "%s must be a string array" % label)


def refs(value, label, sources, strong=False, required=True):
    require(isinstance(value, list) and all(isinstance(x, str) for x in value), "%s must be a source-ID array" % label)
    require(len(value) == len(set(value)), "%s has duplicate source IDs" % label)
    if required:
        require(bool(value), "%s must contain evidence" % label)
    for source_id in value:
        require(source_id in sources, "%s references missing source %s" % (label, source_id))
        require(sources[source_id]["level"] != "failed_read", "%s cannot use failed_read as evidence" % label)
    if strong and required:
        require(any(sources[s]["level"] in STRONG for s in value), "%s requires readable original evidence; a search snippet is insufficient" % label)


def iso_time(value, label):
    nonempty(value, label)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LeadkitError(label + " must be an ISO timestamp") from exc
    require(parsed.tzinfo is not None, label + " needs timezone")


def validate_regions(regions, sources):
    require(isinstance(regions, list) and bool(regions), "region_research must record the researched scope, including blocked attempts")
    for region in regions:
        require(isinstance(region, dict), "region_research items must be objects")
        for field in ("country", "area"):
            nonempty(region.get(field), "region_research." + field)
        iso_time(region.get("checked_at"), "region_research.checked_at")
        status = region.get("status")
        require(status in {"complete", "partial", "blocked"}, "Invalid region_research.status")
        string_list(region.get("languages"), "region_research.languages")
        string_list(region.get("limitations"), "region_research.limitations")
        if status != "blocked":
            require(bool(region["languages"]) and all(x.strip() for x in region["languages"]), "Researched regions need actual language choices")
        refs(region.get("source_ids", []), "region_research.source_ids", sources, strong=status != "blocked", required=status != "blocked")
        incomplete = status != "complete"
        for field in ("markets", "communication_channels", "ecommerce_platforms"):
            entries = region.get(field)
            require(isinstance(entries, list), "region_research." + field + " must be an array")
            incomplete = incomplete or not entries
            for entry in entries:
                require(isinstance(entry, dict), "Regional channel entries must be objects")
                nonempty(entry.get("name"), "regional channel name")
                refs(entry.get("source_ids", []), "regional channel source_ids", sources)
                if field == "markets":
                    nonempty(entry.get("kind"), "market.kind")
                    require(entry.get("size") in {"large", "medium", "small", "unknown"}, "Invalid market.size")
                    refs(entry.get("size_source_ids", []), "market.size_source_ids", sources, strong=entry["size"] != "unknown", required=entry["size"] != "unknown")
        if incomplete:
            require(any(x.strip() for x in region["limitations"]), "Partial/blocked research or empty channel lists need explicit limitations")


def actionable_contact(contact, sources):
    if contact.get("status", "active") != "active" or contact.get("reachability", "untested") == "unavailable":
        return False
    if not any(sources[x]["level"] in STRONG for x in contact["source_ids"]):
        return False
    kind = contact["kind"]
    if kind == "website":
        return False
    if kind in {"phone", "email"}:
        return True
    action = "form" if kind == "form" else "message"
    return (contact.get("action") == action and bool(contact.get("action_evidence", "").strip())
            and any(sources[x]["level"] in STRONG for x in contact.get("action_source_ids", [])))


def derive_priority(lead, sources):
    if lead["status"] != "main":
        return "", "未通过正式客户准入，不评级"
    capability = [x for x in lead.get("capability_evidence", []) if any(sources[s]["level"] in STRONG for s in x["source_ids"])]
    activity = lead.get("ongoing_activity", {})
    if capability and any(sources[s]["level"] in CURRENT for s in activity.get("source_ids", [])):
        ids = sorted({s for x in capability for s in x["source_ids"]} | set(activity["source_ids"]))
        return "A", "已通过准入；经营能力：%s；持续经营：%s [%s]" % ("；".join(x["detail"] for x in capability), activity["detail"], " ".join(ids))
    return "B", "已通过产品、白牌、主体、地域、客户类型和联系入口准入；经营能力或持续经营依据尚不齐全，不推定采购规模"


def validate_packet(packet, master):
    require(isinstance(packet, dict) and packet.get("schema_version") == 2, "New commits require batch schema_version 2; create a new snapshot and apply v1.1 rules")
    valid_id(packet.get("batch_id"), "batch_id", BATCH_RE)
    require(isinstance(packet.get("base_sha256"), str) and re.fullmatch(r"[a-f0-9]{64}", packet["base_sha256"]), "base_sha256 must come from snapshot")
    task = packet.get("task")
    require(isinstance(task, dict), "task is required")
    nonempty(task.get("request"), "task.request")
    for field in ("countries", "products", "requirements"):
        string_list(task.get(field), "task." + field)
    require(bool(task["countries"]), "task.countries needs an explicit scope (use 全球 for global tasks)")
    require(bool(task["products"]) and set(task["products"]) <= set(PRODUCTS), "task.products must select the configured six product classes")
    require(isinstance(task.get("allow_channel_candidates", False), bool), "allow_channel_candidates must be boolean")
    profile = task.get("customer_profile")
    require(isinstance(profile, dict), "task.customer_profile is required")
    string_list(profile.get("buyer_types"), "customer_profile.buyer_types")
    require(bool(profile["buyer_types"]) and all(x.strip() for x in profile["buyer_types"]), "customer_profile.buyer_types cannot be empty")
    require(profile.get("brand_policy") == "white_label_required", "customer_profile.brand_policy must be white_label_required")
    require(isinstance(profile.get("supplier_constraints"), dict), "customer_profile.supplier_constraints must be an object; leave unknown terms unset")
    for field in ("sources", "leads", "queue", "limitations"):
        require(isinstance(packet.get(field), list), field + " must be an array")
    string_list(packet["limitations"], "limitations")
    for q in packet["queue"]:
        require(isinstance(q, dict), "queue items must be objects")
        nonempty(q.get("next_action"), "queue.next_action")
    sources = copy.deepcopy(master["sources"])
    seen = set()
    for source in packet["sources"]:
        require(isinstance(source, dict), "source must be an object")
        sid = source.get("source_id")
        valid_id(sid, "source_id")
        require(sid not in seen, "Duplicate source_id in batch: " + sid)
        seen.add(sid)
        for field in ("url", "title", "read_at", "excerpt"):
            nonempty(source.get(field), "source." + field)
        url = urlsplit(source["url"])
        require(url.scheme in {"http", "https"} and url.hostname and not url.username and not url.password, "Sources require public HTTP(S) URLs without embedded credentials")
        try:
            timestamp = datetime.fromisoformat(source["read_at"].replace("Z", "+00:00"))
            require(timestamp.tzinfo is not None, "source.read_at needs timezone")
        except ValueError as exc:
            raise LeadkitError("Invalid source.read_at ISO timestamp") from exc
        require(source.get("level") in LEVELS, "Unknown source.level")
        if source["level"] == "cached_original":
            nonempty(source.get("limitations"), "cached_original source.limitations")
        if sid in sources:
            require(source == sources[sid], "Source IDs are immutable; assign a new ID to changed source " + sid)
        sources[sid] = copy.deepcopy(source)
    validate_regions(packet.get("region_research"), sources)
    seen_leads = set()
    for lead in packet["leads"]:
        require(isinstance(lead, dict), "lead must be an object")
        lid = lead.get("lead_id")
        valid_id(lid, "lead_id")
        require(lid not in seen_leads, "Duplicate lead_id in batch: " + lid)
        seen_leads.add(lid)
        for field in ("name", "country", "reason"):
            nonempty(lead.get(field), "lead." + field)
        require(lead.get("status") in {"main", "candidate", "excluded"}, "lead.status must be main/candidate/excluded")
        main = lead["status"] == "main"
        brand = lead.get("brand_profile", "unknown")
        require(brand in BRANDS, "Invalid brand_profile")
        if brand in EXCLUDED_BRANDS:
            require(lead["status"] == "excluded", "Brand owners, official stores and brand-exclusive stores must be excluded")
        if main:
            require(brand in {"white_label", "mixed"}, "main needs white_label or mixed brand_profile with evidence")
            require(any(r["country"] == lead["country"] and r["status"] != "blocked" for r in packet["region_research"]), "main needs non-blocked region_research for its actual country")
            require(lead.get("buyer_type") in profile["buyer_types"], "main buyer_type must match customer_profile.buyer_types")
        refs(lead.get("buyer_type_source_ids", []), "buyer_type_source_ids", sources, strong=main, required=main)
        string_list(lead.get("unknowns", []), "lead.unknowns")
        refs(lead.get("identity_source_ids", []), "identity_source_ids", sources, strong=main, required=main)
        refs(lead.get("location_source_ids", []), "location_source_ids", sources, strong=main, required=main)
        if main and "全球" not in task["countries"]:
            require(lead["country"] in task["countries"], "main lead country is outside task.countries")
        pe = lead.get("product_evidence", [])
        require(isinstance(pe, list), "product_evidence must be an array")
        matching = set()
        for item in pe:
            require(isinstance(item, dict) and item.get("product") in PRODUCTS, "Unknown product class")
            nonempty(item.get("detail"), "product_evidence.detail")
            refs(item.get("source_ids", []), "product_evidence.source_ids", sources)
            if item["product"] in task["products"] and any(sources[x]["level"] in STRONG for x in item["source_ids"]):
                matching.add(item["product"])
        channel_exception = bool(task.get("allow_channel_candidates") and lead.get("channel_fit"))
        if channel_exception:
            nonempty(lead["channel_fit"], "channel_fit")
            refs(lead.get("channel_source_ids", []), "channel_source_ids", sources, strong=True)
        if main:
            require(bool(matching), "main lead needs readable evidence for a requested product; channel candidates stay candidate")
        white_evidence = lead.get("white_label_evidence", [])
        require(isinstance(white_evidence, list), "white_label_evidence must be an array")
        white_products = set()
        for item in white_evidence:
            require(isinstance(item, dict) and item.get("product") in PRODUCTS, "Unknown white-label product class")
            nonempty(item.get("detail"), "white_label_evidence.detail")
            refs(item.get("source_ids", []), "white_label_evidence.source_ids", sources)
            if any(sources[x]["level"] in STRONG for x in item["source_ids"]):
                white_products.add(item["product"])
        if main:
            require(bool(matching & white_products), "main needs readable white_label_evidence for the same requested product")
        capability = lead.get("capability_evidence", [])
        require(isinstance(capability, list), "capability_evidence must be an array")
        for item in capability:
            require(isinstance(item, dict) and item.get("kind") in CAPABILITIES, "Invalid capability_evidence.kind")
            nonempty(item.get("detail"), "capability_evidence.detail")
            refs(item.get("source_ids", []), "capability_evidence.source_ids", sources)
        activity = lead.get("ongoing_activity")
        if "ongoing_activity" in lead:
            require(isinstance(activity, dict), "ongoing_activity must be an object")
            nonempty(activity.get("detail"), "ongoing_activity.detail")
            refs(activity.get("source_ids", []), "ongoing_activity.source_ids", sources)
            if activity.get("observed_on") is not None:
                require(isinstance(activity["observed_on"], str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", activity["observed_on"]), "ongoing_activity.observed_on must be YYYY-MM-DD")
                try:
                    datetime.strptime(activity["observed_on"], "%Y-%m-%d")
                except ValueError as exc:
                    raise LeadkitError("Invalid ongoing_activity.observed_on") from exc
        contacts = lead.get("contacts", [])
        require(isinstance(contacts, list), "contacts must be an array")
        strong_contact = False
        for contact in contacts:
            require(isinstance(contact, dict) and contact.get("kind") in CONTACTS, "Invalid contact.kind")
            nonempty(contact.get("value"), "contact.value (must stay text)")
            nonempty(contact.get("role"), "contact.role")
            nonempty(contact.get("attribution"), "contact.attribution (explain merchant ownership)")
            require(contact.get("status", "active") in {"active", "historical", "invalid"}, "contact.status must be active/historical/invalid")
            refs(contact.get("source_ids", []), "contact.source_ids", sources)
            if "action" in contact:
                require(contact["action"] in {"form", "message", "none"}, "contact.action must be form/message/none")
                if contact["action"] != "none":
                    require(contact["kind"] not in {"phone", "email", "website"}, "phone/email use their own entry; website cannot become actionable")
                    require(contact["action"] == ("form" if contact["kind"] == "form" else "message"), "contact.action does not match kind")
                    nonempty(contact.get("action_evidence"), "contact.action_evidence")
            refs(contact.get("action_source_ids", []), "contact.action_source_ids", sources, required=False)
            if actionable_contact(contact, sources):
                strong_contact = True
            if contact["kind"] == "whatsapp":
                refs(contact.get("whatsapp_source_ids", []), "contact.whatsapp_source_ids", sources, strong=main)
                nonempty(contact.get("whatsapp_evidence"), "contact.whatsapp_evidence (explicit wording or link)")
            if contact.get("reachability", "untested") not in {"untested", "unavailable"}:
                raise LeadkitError("This collection skill does not test contacts; reachability must be untested/unavailable")
        if main:
            require(strong_contact, "main lead needs an actionable attributable business entry; bare websites or display-only social profiles are insufficient")
        if "manual_notes" in lead:
            require(isinstance(lead["manual_notes"], str), "manual_notes must be text")
        if lead.get("sales_stage", "未联系") != "未联系":
            raise LeadkitError("Research cannot create sales events or change sales_stage")
    return sources


def normalized(value):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).casefold())


def contact_key(contact):
    kind, value = contact["kind"], contact["value"].strip()
    if kind in {"phone", "whatsapp"}:
        return "number:" + re.sub(r"\D", "", value)
    if kind == "email":
        return "email:" + value.casefold()
    if kind == "website":
        host = (urlsplit(value).hostname or "").casefold().removeprefix("www.")
        if host and not any(host == p or host.endswith("." + p) for p in PLATFORMS):
            return "domain:" + host
        return None
    return kind + ":" + value.casefold().rstrip("/")


def duplicate_hints(leads, focus_ids=None):
    buckets = {}
    for lead in leads.values():
        keys = {"name:" + normalized(lead["name"])}
        keys.update("name:" + normalized(a) for a in lead.get("aliases", []) if isinstance(a, str))
        for contact in lead.get("contacts", []):
            key = contact_key(contact)
            if key and not key.endswith(":"):
                keys.add(key)
        for key in keys:
            buckets.setdefault(key, set()).add(lead["lead_id"])
    output = []
    for key, ids in sorted(buckets.items()):
        if len(ids) > 1 and (focus_ids is None or ids & set(focus_ids)):
            output.append({"matching_key": key, "lead_ids": sorted(ids), "action": "review_relationship_do_not_auto_merge"})
    return output


def append_unique(old_items, new_items):
    """Keep earlier evidence/contacts; an explicit correction lives in unknowns/history."""
    combined = copy.deepcopy(old_items)
    seen = {json.dumps(item, ensure_ascii=False, sort_keys=True) for item in combined}
    for item in new_items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            combined.append(copy.deepcopy(item))
            seen.add(key)
    return combined


def merge_contacts(old_items, new_items):
    combined = copy.deepcopy(old_items)
    lookup = {(c["kind"], c["value"]): i for i, c in enumerate(combined)}
    for incoming in new_items:
        key = (incoming["kind"], incoming["value"])
        if key in lookup:
            old = combined[lookup[key]]
            replacement = dict(old, **copy.deepcopy(incoming))
            replacement["source_ids"] = append_unique(old.get("source_ids", []), incoming.get("source_ids", []))
            if old.get("whatsapp_source_ids") or incoming.get("whatsapp_source_ids"):
                replacement["whatsapp_source_ids"] = append_unique(old.get("whatsapp_source_ids", []), incoming.get("whatsapp_source_ids", []))
            combined[lookup[key]] = replacement
        else:
            lookup[key] = len(combined)
            combined.append(copy.deepcopy(incoming))
    return combined


def backup_master(root, expected_sha):
    """Keep an exact, immutable copy before replacement; abort if the baseline moved."""
    current = root / "execution" / "master.json"
    original = current.read_bytes()
    require(digest_bytes(original) == expected_sha, "Master changed while preparing commit; reread and reconcile")
    directory = root / "execution" / "backups"
    require(not directory.is_symlink(), "Backup directory may not be a symlink")
    directory.mkdir(exist_ok=True)
    destination = directory / (expected_sha + ".json")
    require(not destination.is_symlink(), "Backup may not be a symlink")
    try:
        handle = destination.open("xb")
    except FileExistsError:
        require(destination.read_bytes() == original, "Existing backup content does not match its hash")
    else:
        try:
            with handle:
                handle.write(original)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    require(digest_bytes(destination.read_bytes()) == expected_sha, "Backup read-back verification failed")
    return destination


def commit(root, packet):
    fingerprint = digest_bytes(encoded(packet))
    with locked(root):
        master, sha = load_master(root)
        bid = packet.get("batch_id") if isinstance(packet, dict) else None
        if bid in master["batches"]:
            saved = master["batches"][bid]
            require(saved["packet_sha256"] == fingerprint, "Batch ID already exists with different content")
            return {"batch_id": bid, "already_committed": True, "counts": saved["counts"]}
        sources = validate_packet(packet, master)
        require(packet["base_sha256"] == sha, "Master changed after snapshot; reread and reconcile before updating base_sha256")
        baseline_path = root / "research" / bid / "snapshot.json"
        baseline = read_json(baseline_path)
        require(baseline.get("base_sha256") == packet["base_sha256"], "Batch snapshot does not match. Create a new batch after reconciliation")
        require(baseline.get("batch_id") == bid, "Snapshot batch ID mismatch")
        stamp = now()
        for incoming in packet["leads"]:
            old = master["leads"].get(incoming["lead_id"])
            lead = copy.deepcopy(old) if old else {}
            lead.update(copy.deepcopy(incoming))
            # Omitted fields inherit prior facts; that must not reopen a known excluded brand.
            if lead.get("brand_profile") in EXCLUDED_BRANDS:
                require(lead["status"] == "excluded", "Merged brand profile is excluded; keep excluded status or explicitly correct brand_profile with evidence")
            if old:
                history = copy.deepcopy(old.get("history", []))
                previous = {k: v for k, v in old.items() if k != "history"}
                history.append(previous)
                lead["history"] = history
                lead["created_at"] = old["created_at"]
                for field in ("manual_notes", "manual_owner", "sales_stage"):
                    if field in old:
                        lead[field] = old[field]
                for field in ("identity_source_ids", "location_source_ids", "product_evidence", "aliases", "unknowns"):
                    lead[field] = append_unique(old.get(field, []), lead.get(field, []))
                lead["contacts"] = merge_contacts(old.get("contacts", []), lead.get("contacts", []))
            else:
                lead["created_at"] = stamp
                lead.setdefault("manual_notes", "")
                lead.setdefault("manual_owner", "")
                lead["sales_stage"] = "未联系"
            # Eligibility and priority describe this batch, never silently inherit a prior A.
            lead["priority"], lead["priority_reason"] = derive_priority(incoming, sources)
            lead["rules_version"] = VERSION
            lead["updated_at"] = stamp
            lead["last_batch_id"] = bid
            master["leads"][lead["lead_id"]] = lead
        master["sources"] = sources
        ids = [x["lead_id"] for x in packet["leads"]]
        hints = duplicate_hints(master["leads"], ids)
        for lead in packet["leads"]:
            relevant = [hint for hint in hints if lead["lead_id"] in hint["lead_ids"]]
            if lead["status"] == "main" and relevant:
                review = lead.get("duplicate_review")
                require(isinstance(review, dict) and review.get("decision") == "distinct_entities", "Potential duplicate main lead %s: reuse the correct stable ID, retain candidate status, or document distinct_entities evidence" % lead["lead_id"])
                nonempty(review.get("reason"), "duplicate_review.reason")
                refs(review.get("source_ids", []), "duplicate_review.source_ids", sources, strong=True)
                others = {lid for hint in relevant for lid in hint["lead_ids"] if lid != lead["lead_id"]}
                require(others <= set(review.get("related_lead_ids", [])), "duplicate_review must cover every matched lead ID")
        counts = {status: sum(x["status"] == status for x in packet["leads"]) for status in ("main", "candidate", "excluded")}
        master["batches"][bid] = {"schema_version": 2, "rules_version": VERSION, "region_research": packet["region_research"], "packet_sha256": fingerprint, "committed_at": stamp, "task": packet["task"], "lead_ids": ids, "source_ids": [s["source_id"] for s in packet["sources"]], "lead_snapshots": [copy.deepcopy(master["leads"][lid]) for lid in ids], "queue": packet["queue"], "limitations": packet["limitations"], "counts": counts, "duplicate_hints": hints}
        master["updated_at"] = stamp
        # One authoritative atomic write. No state is split across a ledger and receipt.
        backup_path = backup_master(root, sha)
        safe_write(root / "execution" / "master.json", master)
        saved, after_sha = load_master(root)
        require(saved["batches"][bid]["packet_sha256"] == fingerprint, "Saved batch read-back failed")
    return {"batch_id": bid, "already_committed": False, "counts": counts, "master_sha256": after_sha, "backup_path": str(backup_path), "duplicate_hints": hints}


def column_name(index):
    result = ""
    while index:
        index, rem = divmod(index - 1, 26)
        result = chr(65 + rem) + result
    return result


def tag(name):
    return "{" + NS + "}" + name


def xml_bytes(root):
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def excel_text(value):
    value = str(value if value is not None else "")
    require(len(value) <= 32767, "Excel cell exceeds 32767 characters; split evidence into source rows, do not truncate")
    require(not re.search(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", value), "Invalid XML control character in text")
    return value


def sheet_xml(rows, widths):
    sheet = ET.Element(tag("worksheet"))
    views = ET.SubElement(sheet, tag("sheetViews"))
    view = ET.SubElement(views, tag("sheetView"), workbookViewId="0")
    ET.SubElement(view, tag("pane"), xSplit="2", ySplit="1", topLeftCell="C2", activePane="bottomRight", state="frozen")
    ET.SubElement(sheet, tag("sheetFormatPr"), defaultRowHeight="18")
    cols = ET.SubElement(sheet, tag("cols"))
    for i, width in enumerate(widths, 1):
        ET.SubElement(cols, tag("col"), min=str(i), max=str(i), width=str(width), customWidth="1")
    data = ET.SubElement(sheet, tag("sheetData"))
    for ri, values in enumerate(rows, 1):
        text_values = [excel_text(x) for x in values]
        line_count = max((sum(max(1, (len(part) * 1.4 + widths[ci] - 1) // widths[ci]) for part in value.split("\n")) for ci, value in enumerate(text_values)), default=1)
        height = 32 if ri == 1 else min(409, max(30, 16 * line_count + 10))
        row = ET.SubElement(data, tag("row"), r=str(ri), ht=str(height), customHeight="1")
        for ci, value in enumerate(text_values, 1):
            cell = ET.SubElement(row, tag("c"), r=column_name(ci) + str(ri), t="inlineStr", s="1" if ri == 1 else ("2" if ri % 2 == 0 else "3"))
            text_node = ET.SubElement(ET.SubElement(cell, tag("is")), tag("t"))
            text_node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            text_node.text = value
    if rows:
        ET.SubElement(sheet, tag("autoFilter"), ref="A1:%s%d" % (column_name(len(widths)), len(rows)))
    ET.SubElement(sheet, tag("pageMargins"), left="0.25", right="0.25", top="0.5", bottom="0.5", header="0.2", footer="0.2")
    return xml_bytes(sheet)


STYLES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Calibri"/><color rgb="FF1F2937"/></font><font><b/><sz val="11"/><name val="Calibri"/><color rgb="FFFFFFFF"/></font></fonts>
<fills count="4"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF173B57"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFF0F5F9"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="4"><xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="49" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf><xf numFmtId="49" fontId="0" fillId="3" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf><xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''


def write_xlsx(path, sheets):
    path = Path(path)
    require(path.suffix.lower() == ".xlsx", "Output must end in .xlsx")
    require(not path.exists() and not path.is_symlink(), "Export refuses to overwrite an existing file")
    path.parent.mkdir(parents=True, exist_ok=True)
    content = ET.Element("Types", xmlns="http://schemas.openxmlformats.org/package/2006/content-types")
    ET.SubElement(content, "Default", Extension="rels", ContentType="application/vnd.openxmlformats-package.relationships+xml")
    ET.SubElement(content, "Default", Extension="xml", ContentType="application/xml")
    ET.SubElement(content, "Override", PartName="/xl/workbook.xml", ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml")
    ET.SubElement(content, "Override", PartName="/xl/styles.xml", ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml")
    workbook = ET.Element(tag("workbook"))
    book_sheets = ET.SubElement(workbook, tag("sheets"))
    relationships = ET.Element("Relationships", xmlns=PKGREL)
    rootrels = ET.Element("Relationships", xmlns=PKGREL)
    ET.SubElement(rootrels, "Relationship", Id="rId1", Type=REL + "/officeDocument", Target="xl/workbook.xml")
    pieces = {}
    for i, (name, rows, widths) in enumerate(sheets, 1):
        ET.SubElement(book_sheets, tag("sheet"), name=name, sheetId=str(i), attrib={"{" + REL + "}id": "rId" + str(i)})
        ET.SubElement(relationships, "Relationship", Id="rId" + str(i), Type=REL + "/worksheet", Target="worksheets/sheet%d.xml" % i)
        ET.SubElement(content, "Override", PartName="/xl/worksheets/sheet%d.xml" % i, ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml")
        pieces["xl/worksheets/sheet%d.xml" % i] = sheet_xml(rows, widths)
    ET.SubElement(relationships, "Relationship", Id="rIdStyles", Type=REL + "/styles", Target="styles.xml")
    pieces.update({"[Content_Types].xml": xml_bytes(content), "_rels/.rels": xml_bytes(rootrels), "xl/workbook.xml": xml_bytes(workbook), "xl/_rels/workbook.xml.rels": xml_bytes(relationships), "xl/styles.xml": STYLES.encode("utf-8")})
    fd, temporary = tempfile.mkstemp(prefix=".leadkit-export-", suffix=".xlsx", dir=str(path.parent))
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for filename, raw in pieces.items():
                archive.writestr(filename, raw)
        # Exclusive creation prevents another exporter from being overwritten.
        with open(temporary, "rb") as source, path.open("xb") as dest:
            while True:
                chunk = source.read(65536)
                if not chunk:
                    break
                dest.write(chunk)
        with zipfile.ZipFile(path) as archive:
            require(archive.testzip() is None, "Export ZIP validation failed")
            for filename in archive.namelist():
                ET.fromstring(archive.read(filename))
    finally:
        Path(temporary).unlink(missing_ok=True)


def lead_source_ids(lead):
    result = set(lead.get("identity_source_ids", []) + lead.get("location_source_ids", []) + lead.get("channel_source_ids", []) + lead.get("buyer_type_source_ids", []))
    for item in lead.get("product_evidence", []):
        result.update(item["source_ids"])
    for group in ("white_label_evidence", "capability_evidence"):
        for item in lead.get(group, []):
            result.update(item.get("source_ids", []))
    result.update(lead.get("ongoing_activity", {}).get("source_ids", []))
    result.update(lead.get("duplicate_review", {}).get("source_ids", []))
    for contact in lead.get("contacts", []):
        result.update(contact["source_ids"])
        result.update(contact.get("whatsapp_source_ids", []))
        result.update(contact.get("action_source_ids", []))
    return result


def regional_source_ids(region):
    result = set(region.get("source_ids", []))
    for group in ("markets", "communication_channels", "ecommerce_platforms"):
        for item in region.get(group, []):
            result.update(item.get("source_ids", []))
            result.update(item.get("size_source_ids", []))
    return result


def export_batch(root, batch_id, output):
    master, _ = load_master(root)
    require(batch_id in master["batches"], "Unknown committed batch")
    batch = master["batches"][batch_id]
    leads = batch["lead_snapshots"]
    legacy = batch.get("schema_version", 1) < 2
    if not legacy:
        leads = sorted(leads, key=lambda x: {"A": 0, "B": 1}.get(x.get("priority"), 2))
    header = ["客户编号", "商户名称", "国家", "城市", "地址／精度", "经营角色", "匹配产品及证据摘要", "公开业务入口", "适配理由", "待核问题", "来源编号", "人工备注"]
    header += ["品牌经营类型", "白牌经营证据", "所在市场／位置精度", "近期经营依据", "开发优先级", "评级理由"]
    widths = [18, 28, 16, 18, 34, 22, 52, 52, 42, 42, 28, 32, 22, 52, 34, 52, 18, 60]
    output_rows = {"main": [header], "candidate": [header]}
    for lead in leads:
        if lead["status"] not in output_rows:
            continue
        evidence = "\n".join("%s：%s [%s]" % (x["product"], x["detail"], " ".join(x["source_ids"])) for x in lead.get("product_evidence", []))
        contacts = "\n".join("%s：%s（%s；%s；%s；%s）[%s]" % (x["kind"], x["value"], x["role"], x["attribution"], {"active": "当前采集", "historical": "历史入口", "invalid": "无效入口"}[x.get("status", "active")], "已知不可用" if x.get("reachability") == "unavailable" else "可达未测试", " ".join(x["source_ids"])) for x in lead.get("contacts", []))
        output_rows[lead["status"]].append([lead["lead_id"], lead["name"], lead["country"], lead.get("city", ""), lead.get("address", "未知"), lead.get("buyer_type", "未知"), evidence or lead.get("channel_fit", "未知"), contacts, lead["reason"], "\n".join(lead.get("unknowns", [])), " ".join(sorted(lead_source_ids(lead))), lead.get("manual_notes", ""), BRAND_LABELS.get(lead.get("brand_profile"), "旧版未评估" if legacy else "品牌类型待核"), "\n".join("%s：%s [%s]" % (x["product"], x["detail"], " ".join(x["source_ids"])) for x in lead.get("white_label_evidence", [])), lead.get("market_location", "未明确"), (lead.get("ongoing_activity", {}).get("detail", "未明确") + "；日期：" + lead.get("ongoing_activity", {}).get("observed_on", "未公开") + " [" + " ".join(lead.get("ongoing_activity", {}).get("source_ids", [])) + "]"), "旧版未评级" if legacy else (lead.get("priority") or "未评级"), "历史批次，未按1.1规则重新资格核验" if legacy else lead.get("priority_reason", "未评级")])
    source_rows = [["来源编号", "关联客户", "来源标题", "来源网址", "读取时间", "读取层级", "支持内容", "限制／资料日期"]]
    wanted = set(batch["source_ids"])
    for lead in leads:
        wanted.update(lead_source_ids(lead))
    for region in batch.get("region_research", []):
        wanted.update(regional_source_ids(region))
    for sid in sorted(wanted):
        source = master["sources"][sid]
        linked = " ".join(x["lead_id"] for x in leads if sid in lead_source_ids(x))
        regional_links = ["地区预研：" + r["area"] for r in batch.get("region_research", []) if sid in regional_source_ids(r)]
        linked = " / ".join([linked] + regional_links).strip(" / ")
        source_rows.append([sid, linked, source["title"], source["url"], source["read_at"], source["level"], source["excerpt"], "\n".join(str(source.get(k, "")) for k in ("limitations", "content_date") if source.get(k))])
    notes = [["项目", "内容"], ["批次编号", batch_id], ["原始需求", batch["task"]["request"]], ["目标国家", "、".join(batch["task"]["countries"])], ["目标产品", "、".join(batch["task"]["products"])], ["本轮结果", "可开发候选 %d；待核 %d；后台排除 %d" % (batch["counts"]["main"], batch["counts"]["candidate"], batch["counts"]["excluded"])], ["结果边界", "可开发客户为符合本次研究条件的候选，不代表采购意愿、采购权限或可达性已确认。本技能未外联。"], ["已知限制", "\n".join(batch["limitations"]) or "无额外记录"], ["接续事项", "\n".join(q["next_action"] for q in batch["queue"]) or "无记录"], ["查重提示", "\n".join("%s：%s，关系待复核，未自动合并" % (h["matching_key"], " / ".join(h["lead_ids"])) for h in batch["duplicate_hints"]) or "无匹配提示"], ["数据时间", batch["committed_at"]]]
    profile = batch["task"].get("customer_profile", {})
    notes += [["资格规则版本", batch.get("rules_version", "1.0.0（历史批次未重新核验）")], ["目标客户类型", "、".join(profile.get("buyer_types", [])) or "历史批次未单列"], ["品牌准入", "目标产品白牌经营有据；混合经营须有白牌证据；品牌方／官方店／专卖店排除" if not legacy else "历史规则，未按新门槛复核"], ["优先级说明", "A/B仅为已通过准入候选的研究交接优先级，不是成交概率；无评级不等于低价值。旧批次保留历史资格，不自动晋级。"]]
    for name, value in profile.get("supplier_constraints", {}).items():
        notes.append(["供货条件：" + str(name), str(value)])
    for region in batch.get("region_research", []):
        notes.append(["地区预研：" + region["area"], "%s；%s；语言：%s；核查时间：%s；限制：%s [%s]" % (region["country"], {"complete": "已完成本次预研", "partial": "部分完成", "blocked": "受阻"}[region["status"]], "、".join(region["languages"]) or "未核", region["checked_at"], "；".join(region["limitations"]) or "无额外记录", " ".join(region["source_ids"]))])
        for group, label in (("markets", "市场"), ("communication_channels", "通信渠道"), ("ecommerce_platforms", "电商平台")):
            for item in region[group]:
                detail = item["name"]
                if group == "markets":
                    detail += "；" + item["kind"] + "；规模：" + {"large": "大型", "medium": "中型", "small": "小型", "unknown": "待核"}[item["size"]]
                notes.append([region["area"] + "／" + label, detail + " [" + " ".join(item["source_ids"] + item.get("size_source_ids", [])) + "]"])
    write_xlsx(output, [("可开发客户", output_rows["main"], widths), ("待核线索", output_rows["candidate"], widths), ("来源证据", source_rows, [18, 24, 34, 62, 28, 24, 80, 50]), ("采集说明", notes, [24, 100])])
    return {"output": str(Path(output).resolve()), "batch_id": batch_id, "counts": batch["counts"], "sheets": 4}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "snapshot", "validate", "commit", "dedupe", "export"):
        sub = subs.add_parser(name)
        sub.add_argument("--workspace", required=True, help="Customer project directory, separate from installed skill")
        if name in {"snapshot", "export"}:
            sub.add_argument("--batch-id", required=True)
        if name in {"validate", "commit"}:
            sub.add_argument("--batch", required=True, help="Codex-authored evidence batch JSON")
        if name == "export":
            sub.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        root = workspace_path(args.workspace)
        if args.command == "init":
            result = init_workspace(root)
        elif args.command == "snapshot":
            result = snapshot(root, args.batch_id)
        elif args.command == "commit":
            result = commit(root, read_json(args.batch))
        elif args.command == "validate":
            master, sha = load_master(root)
            packet = read_json(args.batch)
            validate_packet(packet, master)
            result = {"valid": True, "base_matches_current": packet["base_sha256"] == sha, "fact_truth_verified": False}
        elif args.command == "dedupe":
            master, _ = load_master(root)
            result = {"hints": duplicate_hints(master["leads"]), "automatically_merged": False}
        else:
            result = export_batch(root, args.batch_id, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (LeadkitError, OSError, ValueError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
