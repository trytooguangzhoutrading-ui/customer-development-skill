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
from datetime import datetime, timezone, date
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from xml.etree import ElementTree as ET

VERSION = "1.5.0"
PRIMARY_PRODUCTS = ["智能眼镜", "智能手表", "智能手表表带", "智能手环", "智能戒指"]
SUPPLEMENTARY_PRODUCTS = ["蓝牙耳机", "蓝牙音响"]
PRODUCTS = PRIMARY_PRODUCTS + SUPPLEMENTARY_PRODUCTS
LEVELS = {"current_original", "cached_original", "third_party_original", "search_snippet", "failed_read"}
STRONG = {"current_original", "cached_original", "third_party_original"}
CONTACTS = {"phone", "whatsapp", "email", "website", "form", "facebook", "instagram", "telegram", "tiktok", "youtube", "linkedin", "zalo", "chat", "other"}
PLATFORMS = {"facebook.com", "instagram.com", "t.me", "telegram.me", "youtube.com", "youtu.be", "tiktok.com", "linkedin.com", "wa.me", "whatsapp.com", "google.com", "maps.google.com"}
BRANDS = {"independent", "white_label", "mixed", "brand_owner", "official_store", "brand_exclusive", "unknown"}
BRAND_LABELS = {"independent": "独立商户", "white_label": "白牌经营", "mixed": "品牌／白牌混合经营", "brand_owner": "品牌方", "official_store": "官方品牌店", "brand_exclusive": "品牌专卖店", "unknown": "品牌类型待核"}
EXCLUDED_BRANDS = {"brand_owner", "official_store", "brand_exclusive"}
BRAND_STORES = {"official_store", "brand_exclusive"}
CAPABILITIES = {"wholesale", "import", "distribution", "sales_capacity"}
BLOCKING_ISSUES = {"identity_conflict", "location_conflict", "white_label_conflict", "product_conflict", "buyer_conflict", "contact_conflict", "employee_correction"}
ENTITY_ISSUES = {"identity_conflict", "contact_conflict"}
BUSINESS_PRIORITIES = {"supplier_fit", "product_coverage", "buyer_type", "procurement_access"}
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
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


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
    import leadkit_project as project
    project.guard_json_workspace(root)
    path = root / "execution" / "master.json"
    require(not path.is_symlink(), "Master may not be a symlink")
    require(path.is_file(), "Workspace is not initialized; run init in a new workspace")
    raw = path.read_bytes()
    master = json.loads(raw)
    require(master.get("schema_version") == 1, "Unsupported master schema")
    return master, digest_bytes(raw)


def init_workspace(root):
    import leadkit_project as project
    project.guard_init(root)
    root.mkdir(parents=True, exist_ok=True)
    with locked(root):
        reserved = [root / "config.json", root / "execution", root / "research", root / "outputs"]
        require(not any(p.exists() or p.is_symlink() for p in reserved), "init refuses to overwrite an existing project; choose a new workspace")
        for name in ("execution", "research", "outputs"):
            (root / name).mkdir()
        config = {"schema_version": 1, "products": PRIMARY_PRODUCTS, "supplementary_products": SUPPLEMENTARY_PRODUCTS, "default_buyer_types": DEFAULT_BUYER_TYPES, "preferred_buyer_types": ["批发商", "进口商", "分销商", "当地市场商户"], "business_priorities": ["buyer_type"], "brand_policy": "independent_product", "collect_brand_stores": True, "outreach_enabled": False, "employee_output": "xlsx", "default_region": None, "supplier_constraints": {}, "activity_max_age_days": 180, "rules_version": VERSION}
        safe_write(root / "config.json", config)
        master = {"schema_version": 1, "created_at": now(), "updated_at": now(), "leads": {}, "sources": {}, "batches": {}}
        safe_write(root / "execution" / "master.json", master)
        project.bind_default_unlocked(root)
    return {"workspace": str(root), "master": str(root / "execution" / "master.json"), "created": True}


def valid_id(value, label, pattern=ID_RE):
    require(isinstance(value, str) and pattern.fullmatch(value), "Invalid %s: use a stable ASCII ID" % label)


def basic_snapshot(root, batch_id):
    valid_id(batch_id, "batch_id", BATCH_RE)
    with locked(root):
        master, sha = load_master(root)
        require(batch_id not in master["batches"], "Batch already committed")
        target = root / "research" / batch_id
        require(not target.exists(), "Batch directory already exists; reuse its saved snapshot or choose a new ID")
        target.mkdir()
        baseline = {"schema_version": 1, "batch_id": batch_id, "base_sha256": sha, "created_at": now(), "lead_ids": sorted(master["leads"])}
        safe_write(target / "snapshot.json", baseline)
        packet = {"schema_version": 3, "batch_id": batch_id, "base_sha256": sha, "task": {"request": "", "countries": [], "products": PRIMARY_PRODUCTS, "requirements": [], "allow_channel_candidates": False, "customer_profile": {"buyer_types": DEFAULT_BUYER_TYPES, "brand_policy": "independent_product", "collect_brand_stores": True, "supplier_constraints": {}}}, "region_research": [], "sources": [], "leads": [], "queue": [], "coverage": [], "limitations": []}
        safe_write(target / "batch.json", packet)
    return {"batch_id": batch_id, "base_sha256": sha, "batch_path": str(target / "batch.json"), "snapshot_path": str(target / "snapshot.json")}


def snapshot(root, batch_id, overrides=None):
    import leadkit_workflow
    return leadkit_workflow.snapshot(root, batch_id, overrides)


def nonempty(value, label):
    require(isinstance(value, str) and bool(value.strip()), "%s must be a nonempty string" % label)


def string_list(value, label):
    require(isinstance(value, list) and all(isinstance(x, str) for x in value), "%s must be a string array" % label)


def validate_product_scope(task):
    string_list(task.get("products"), "task.products")
    require(bool(task["products"]) and set(task["products"]) <= set(PRODUCTS), "task.products must select supported product classes")
    supplementary = task.get("supplementary_products", [])
    string_list(supplementary, "task.supplementary_products")
    require(set(supplementary) <= set(PRODUCTS), "Unknown supplementary product class")
    require(not set(supplementary) & set(task["products"]), "Supplementary products must be outside this task's main product scope")


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
    if kind == "whatsapp" and (not contact.get("whatsapp_evidence", "").strip() or not any(sources[x]["level"] in STRONG for x in contact.get("whatsapp_source_ids", []))):
        return False
    if kind in {"phone", "email"}:
        return True
    action = "form" if kind == "form" else "message"
    return (contact.get("action") == action and bool(contact.get("action_evidence", "").strip())
            and any(sources[x]["level"] in STRONG for x in contact.get("action_source_ids", [])))


def derive_priority(lead, sources, task=None):
    if lead["status"] != "main":
        return "", "未通过正式客户准入，不评级"
    capability = [x for x in lead.get("capability_evidence", []) if any(sources[s]["level"] in STRONG for s in x["source_ids"])]
    activity = lead.get("ongoing_activity", {})
    if capability and recent_activity(activity, (task or {}).get("activity_max_age_days", 180)) and any(sources[s]["level"] in CURRENT for s in activity.get("source_ids", [])):
        ids = sorted({s for x in capability for s in x["source_ids"]} | set(activity["source_ids"]))
        return "A", "已通过准入；经营能力：%s；持续经营：%s [%s]" % ("；".join(x["detail"] for x in capability), activity["detail"], " ".join(ids))
    gates = "产品、白牌、主体、地域、客户类型和联系入口" if brand_policy(task or {}) == "white_label_required" else "产品、主体、地域、客户类型和联系入口"
    return "B", "已通过" + gates + "准入；经营能力或持续经营依据尚不齐全，不推定采购规模"


def derive_business_priority(lead, sources, task):
    """Explain sourced business fit separately from evidence-completeness A/B."""
    products = matched_products(lead, task, sources)
    fit = lead.get("supplier_fit", [])
    matched = [x for x in fit if x.get("status") == "matched" and any(sources[s]["level"] in STRONG for s in x.get("source_ids", []))]
    preferred = lead.get("buyer_type") in task.get("preferred_buyer_types", []) and any(sources[s]["level"] in STRONG for s in lead.get("buyer_type_source_ids", []))
    procurement = lead.get("procurement_access", {})
    explicit_access = bool(procurement.get("detail")) and any(sources[s]["level"] in STRONG for s in procurement.get("source_ids", []))
    signals = {"supplier_fit": len(matched), "product_coverage": len(products), "buyer_type": int(preferred), "procurement_access": int(explicit_access)}
    details = ["目标产品已证：" + ("、".join(products) or "待核"),
               "供货条件：已证匹配 %d 项；其余未知不等于不合格" % len(matched),
               "经营角色：" + lead.get("buyer_type", "未知") + ("（符合本次明确偏好）" if preferred else "（无额外偏好判断）"),
               "采购入口：" + (procurement["detail"] + " [" + " ".join(procurement["source_ids"]) + "]" if explicit_access else "未核，不推定采购权限")]
    return {"criteria": copy.deepcopy(task.get("business_priorities", [])), "signals": signals, "reason": "；".join(details)}


def business_sort_key(lead, task):
    signals = lead.get("business_priority", {}).get("signals", {})
    return tuple(-signals.get(k, 0) for k in task.get("business_priorities", [])) + ({"A": 0, "B": 1}.get(lead.get("priority"), 2),)


def validate_packet(packet, master):
    require(isinstance(packet, dict) and packet.get("schema_version") == 3, "New commits require batch schema_version 3; create a new snapshot and apply v1.2 rules")
    valid_id(packet.get("batch_id"), "batch_id", BATCH_RE)
    require(isinstance(packet.get("base_sha256"), str) and re.fullmatch(r"[a-f0-9]{64}", packet["base_sha256"]), "base_sha256 must come from snapshot")
    task = packet.get("task")
    require(isinstance(task, dict), "task is required")
    nonempty(task.get("request"), "task.request")
    for field in ("countries", "products", "requirements"):
        string_list(task.get(field), "task." + field)
    require(bool(task["countries"]), "task.countries needs an explicit scope (use 全球 for global tasks)")
    validate_product_scope(task)
    require(isinstance(task.get("allow_channel_candidates", False), bool), "allow_channel_candidates must be boolean")
    profile = task.get("customer_profile")
    require(isinstance(profile, dict), "task.customer_profile is required")
    string_list(profile.get("buyer_types"), "customer_profile.buyer_types")
    require(bool(profile["buyer_types"]) and all(x.strip() for x in profile["buyer_types"]), "customer_profile.buyer_types cannot be empty")
    require(profile.get("brand_policy") in {"white_label_required", "independent_product"}, "Invalid customer_profile.brand_policy")
    require(type(profile.get("collect_brand_stores", False)) is bool, "customer_profile.collect_brand_stores must be boolean")
    require(brand_policy(task) != "white_label_required" or not profile.get("collect_brand_stores", False), "white_label_required cannot collect brand stores")
    require(isinstance(profile.get("supplier_constraints"), dict), "customer_profile.supplier_constraints must be an object; leave unknown terms unset")
    import leadkit_research as research
    research.validate_policy(task)
    string_list(task.get("business_priorities", []), "task.business_priorities")
    require(set(task.get("business_priorities", [])) <= BUSINESS_PRIORITIES, "Unknown task.business_priorities")
    require(len(task.get("business_priorities", [])) == len(set(task.get("business_priorities", []))), "Duplicate task.business_priorities")
    string_list(task.get("preferred_buyer_types", []), "task.preferred_buyer_types")
    require(set(task.get("preferred_buyer_types", [])) <= set(profile["buyer_types"]), "preferred_buyer_types must be within allowed buyer_types")
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
        if brand in excluded_brands(task):
            require(lead["status"] == "excluded", "This brand profile must be excluded under the saved task policy")
        if main:
            allowed_brands = {"white_label", "mixed"} if brand_policy(task) == "white_label_required" else {"independent", "white_label", "mixed"} | (BRAND_STORES if profile.get("collect_brand_stores", False) else set())
            require(brand in allowed_brands, "main needs a supported brand_profile identifying an independent merchant or allowed brand store")
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
        if main and brand_policy(task) == "white_label_required":
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
    validate_v3(packet, master, sources)
    return sources


def recent_activity(activity, max_age):
    """Reading a page today does not make an old business event recent."""
    try:
        observed = date.fromisoformat(activity.get("observed_on", ""))
    except (ValueError, TypeError):
        return False
    age = (datetime.now(timezone.utc).date() - observed).days
    return 0 <= age <= max_age


def task_scopes(task):
    scopes = task.get("geo_scope")
    if scopes is None:
        scopes = [dict({"country": c}, **{k: task[k] for k in ("city", "market", "address") if task.get(k)}) for c in task["countries"]]
    require(isinstance(scopes, list) and bool(scopes), "geo_scope must be a nonempty array")
    for scope in scopes:
        require(isinstance(scope, dict) and set(scope) <= {"country", "city", "market", "address"}, "Invalid geo_scope fields")
        nonempty(scope.get("country"), "geo_scope.country")
        require("全球" in task["countries"] or scope["country"] in task["countries"], "geo_scope country outside task countries")
        for key, value in scope.items():
            nonempty(value, "geo_scope." + key)
    return scopes


def brand_policy(task):
    # Missing policy is historical data, never a request to apply new defaults.
    return task.get("customer_profile", {}).get("brand_policy", "white_label_required")


def excluded_brands(task):
    if brand_policy(task) == "independent_product" and task.get("customer_profile", {}).get("collect_brand_stores", False):
        return {"brand_owner"}
    return EXCLUDED_BRANDS


def collection_segment(lead, task):
    """Classification is separate from qualification and never user-assigned."""
    if lead.get("brand_profile") in BRAND_STORES:
        return "brand_store"
    if lead.get("brand_profile") in {"independent", "white_label", "mixed"}:
        return "independent"
    return "unclassified"


def is_independent_main(lead, task):
    return lead.get("status") == "main" and collection_segment(lead, task) == "independent"


def result_counts(leads, task):
    counts = {status: sum(x["status"] == status for x in leads) for status in ("main", "candidate", "excluded")}
    if brand_policy(task) == "independent_product":
        counts.update(independent_main=sum(is_independent_main(x, task) for x in leads),
                      brand_store_main=sum(x["status"] == "main" and collection_segment(x, task) == "brand_store" for x in leads))
    return counts


def scope_key(task):
    # Request wording, run dates and target counts must not create new eligibility scopes.
    dimensions = {"geo_scope": sorted([{k: normalized(v) for k, v in s.items()} for s in task_scopes(task)], key=lambda x: json.dumps(x, sort_keys=True)), "products": sorted(task["products"]), "buyer_types": sorted(task["customer_profile"]["buyer_types"]), "requirements": sorted(task["requirements"]), "supplier_constraints": task["customer_profile"].get("supplier_constraints", {}), "brand_policy": brand_policy(task)}
    if brand_policy(task) != "white_label_required":
        dimensions["collect_brand_stores"] = task["customer_profile"].get("collect_brand_stores", False)
    return "scope-" + digest_bytes(encoded(dimensions))[:24]


def matched_products(lead, task, sources):
    def available(group):
        return {x["product"] for x in lead.get(group, []) if any(sources[s]["level"] in STRONG for s in x.get("source_ids", []))}
    products = set(task.get("products", [])) & available("product_evidence")
    if brand_policy(task) == "white_label_required":
        products &= available("white_label_evidence")
    return sorted(products)


def white_label_review(lead, task, sources):
    """Report only this task's sourced products; historical merged facts do not qualify."""
    products = set(matched_products(lead, dict(task, customer_profile=dict(task.get("customer_profile", {}), brand_policy="independent_product")), sources))
    products &= {x["product"] for x in lead.get("white_label_evidence", []) if any(sources[s]["level"] in STRONG for s in x.get("source_ids", []))}
    for issue in lead.get("issues", []):
        if issue.get("kind") == "white_label_conflict" and issue.get("state") != "resolved":
            if issue.get("affected_scope_ids") and scope_key(task) not in issue["affected_scope_ids"]:
                continue
            products -= set(issue.get("affected_products", products))
    return {"white_label_status": "verified" if products else "unverified", "white_label_products": sorted(products)}


def validate_issues(items, sources, label):
    require(isinstance(items, list), label + " must be an array")
    seen = set()
    for item in items:
        require(isinstance(item, dict), label + " items must be objects")
        valid_id(item.get("issue_id"), label + ".issue_id")
        require(item["issue_id"] not in seen, "Duplicate issue_id")
        seen.add(item["issue_id"])
        for field in ("kind", "detail", "next_action", "next_trigger"):
            nonempty(item.get(field), label + "." + field)
        require(item.get("state") in {"open", "resolved", "blocked"}, "Invalid issue.state")
        require(type(item.get("attempts")) is int and item["attempts"] >= 0, "issue.attempts must be nonnegative integer")
        if "blocks_qualification" in item:
            require(type(item["blocks_qualification"]) is bool, "issue.blocks_qualification must be boolean")
            require(item["kind"] not in BLOCKING_ISSUES or item["blocks_qualification"], "Critical issue cannot disable blocks_qualification")
        for field in ("affected_scope_ids", "affected_products"):
            if field in item:
                string_list(item[field], "issue." + field)
                require(bool(item[field]), "issue." + field + " cannot be empty; omit for all scopes/products")
        for scope in item.get("affected_scope_ids", []):
            valid_id(scope, "issue.affected_scope_ids")
        require(set(item.get("affected_products", [])) <= set(PRODUCTS), "Unknown issue.affected_products")
        refs(item.get("source_ids", []), label + ".source_ids", sources, strong=item["state"] == "resolved", required=item["state"] == "resolved")
        if item["state"] == "resolved":
            iso_time(item.get("resolved_at"), label + ".resolved_at")


def issue_blocks(issue, task, scope_id=None):
    """Identity/contact ownership conflicts concern the entity, not one product."""
    if issue.get("state") == "resolved" or not (issue.get("blocks_qualification") or issue.get("kind") in BLOCKING_ISSUES):
        return False
    if issue.get("kind") == "white_label_conflict" and brand_policy(task) == "independent_product":
        return False
    if issue.get("kind") in ENTITY_ISSUES:
        return True
    if issue.get("affected_scope_ids") and (scope_id or scope_key(task)) not in issue["affected_scope_ids"]:
        return False
    if issue.get("affected_products") and not set(task.get("products", [])) & set(issue["affected_products"]):
        return False
    return True


def validate_issue_update(previous, issue):
    require(issue["attempts"] >= previous["attempts"], "issue.attempts cannot decrease")
    if previous.get("state") != "resolved" and (previous.get("blocks_qualification") or previous.get("kind") in BLOCKING_ISSUES):
        require(issue["kind"] == previous["kind"], "Resolve blocking issue before changing its kind")
        for field in ("affected_scope_ids", "affected_products"):
            require(issue.get(field) == previous.get(field), "Resolve blocking issue before narrowing its scope")
        require(issue.get("blocks_qualification", issue["kind"] in BLOCKING_ISSUES), "Resolve blocking issue before disabling its gate")


def validate_checks(items, keys, key_field, sources, label, main, unknown_allowed=False):
    require(isinstance(items, list), label + " must be array")
    found = {}
    for item in items:
        require(isinstance(item, dict) and item.get(key_field) in keys, "Unknown " + label + " key")
        key = item[key_field]
        require(key not in found, "Duplicate " + label + " key")
        found[key] = item
        require(item.get("status") in {"matched", "unknown", "conflict"}, "Invalid " + label + " status")
        nonempty(item.get("detail"), label + ".detail")
        refs(item.get("source_ids", []), label + ".source_ids", sources, strong=item["status"] != "unknown", required=item["status"] != "unknown")
    if main:
        for key in keys:
            require(key in found, "main needs explicit " + label + " for " + key)
            require(found[key]["status"] in ({"matched", "unknown"} if unknown_allowed else {"matched"}), "main has unresolved or conflicting " + label)


def validate_input_reference(value):
    require(isinstance(value, dict), "input_reference must be an object")
    nonempty(value.get("file"), "input_reference.file")
    if "sheet" in value:
        nonempty(value["sheet"], "input_reference.sheet")
    if "row" in value:
        require(type(value["row"]) is int and value["row"] > 0, "input_reference.row must be positive integer")
    if "sha256" in value:
        require(isinstance(value["sha256"], str) and bool(re.fullmatch(r"[a-f0-9]{64}", value["sha256"])), "Invalid input_reference.sha256")


def validate_v3(packet, master, sources):
    task = packet["task"]
    scopes = task_scopes(task)
    age = task.get("activity_max_age_days", 180)
    require(type(age) is int and 1 <= age <= 730, "activity_max_age_days must be 1..730")
    validate_issues(packet["queue"], sources, "queue")
    prior_queue = {q["issue_id"]: q for q in master.get("queue", [])}
    for q in packet["queue"]:
        if q["issue_id"] in prior_queue:
            validate_issue_update(prior_queue[q["issue_id"]], q)
    ids = {x["lead_id"] for x in packet["leads"]}
    for q in packet["queue"]:
        if q.get("lead_id"):
            require(q["lead_id"] in ids or q["lead_id"] in master["leads"], "queue lead_id is unknown")
    coverage = packet.get("coverage")
    require(isinstance(coverage, list), "coverage must be an array")
    pairs = set()
    discovered = {}
    for cell in coverage:
        require(isinstance(cell, dict) and cell.get("product") in task["products"], "coverage product outside task")
        nonempty(cell.get("channel"), "coverage.channel")
        pair = (cell["product"], cell["channel"])
        require(pair not in pairs, "Duplicate coverage product/channel")
        pairs.add(pair)
        inputs = cell.get("input_references", [])
        require(isinstance(inputs, list), "coverage.input_references must be array")
        for reference in inputs:
            validate_input_reference(reference)
        status = cell.get("status")
        require(status in {"unsearched", "blocked", "searched_zero", "searched"}, "Invalid coverage.status")
        require(type(cell.get("attempts")) is int and cell["attempts"] >= 0, "coverage.attempts must be nonnegative integer")
        require((status == "unsearched" and cell["attempts"] == 0) or (status != "unsearched" and cell["attempts"] > 0), "coverage attempts disagree with status")
        if status in {"blocked", "unsearched"}:
            string_list(cell.get("source_ids", []), "coverage.source_ids")
            require(set(cell.get("source_ids", [])) <= set(sources), "coverage missing source")
        else:
            refs(cell.get("source_ids", []), "coverage.source_ids", sources, required=not inputs)
        string_list(cell.get("limitations", []), "coverage.limitations")
        if status in {"blocked", "unsearched"}:
            require(bool(cell.get("limitations")), "blocked/unsearched coverage needs limitations")
        found = cell.get("discovered_lead_ids", [])
        string_list(found, "coverage.discovered_lead_ids")
        require(set(found) <= ids, "coverage references lead outside current packet")
        require(not found or status in {"searched", "searched_zero"}, "Unsearched/blocked coverage cannot contain discovered leads")
        if status == "searched":
            require(any(x["lead_id"] in found and x["status"] == "main" and cell["product"] in matched_products(x, task, sources) for x in packet["leads"]), "searched requires a qualified lead for this product")
        if status == "searched_zero":
            require(not any(x["lead_id"] in found and x["status"] == "main" and cell["product"] in matched_products(x, task, sources) for x in packet["leads"]), "searched_zero cannot contain qualified product leads")
        for lid in found:
            discovered.setdefault(lid, set()).add(cell["channel"])
    require(set(task["products"]) <= {p for p, _ in pairs}, "coverage must expose every requested product, including unsearched ones")
    for lead in packet["leads"]:
        lid, main = lead["lead_id"], lead["status"] == "main"
        for field in ("exclusion", "exclusion_resolution", "procurement_access", "relationship_evidence", "location_match", "duplicate_review"):
            if field in lead:
                require(isinstance(lead[field], dict), field + " must be an object")
                refs(lead[field].get("source_ids", []), field + ".source_ids", sources, required=False)
        for field in ("exclusion_resolution", "procurement_access"):
            if field in lead:
                nonempty(lead[field].get("detail"), field + ".detail")
                refs(lead[field].get("source_ids", []), field + ".source_ids", sources, strong=True)
        validate_issues(lead.get("issues", []), sources, "lead.issues")
        old_issues = {x["issue_id"]: x for x in master["leads"].get(lid, {}).get("issues", [])}
        for issue in lead.get("issues", []):
            if issue["issue_id"] in old_issues:
                validate_issue_update(old_issues[issue["issue_id"]], issue)
        origins = lead.get("discovered_by", [])
        require(isinstance(origins, list) and bool(origins), "lead.discovered_by must record actual discovery channel")
        for origin in origins:
            require(isinstance(origin, dict), "discovered_by item must be object")
            nonempty(origin.get("channel"), "discovered_by.channel")
            if "input_reference" in origin:
                validate_input_reference(origin["input_reference"])
                require(any(origin["input_reference"] in cell.get("input_references", []) for cell in coverage if cell["channel"] == origin["channel"] and lid in cell.get("discovered_lead_ids", [])), "input_reference must be recorded in matching coverage")
            refs(origin.get("source_ids", []), "discovered_by.source_ids", sources, required="input_reference" not in origin)
        require(set(x["channel"] for x in origins) <= discovered.get(lid, set()), "discovered_by channel missing from coverage")
        validate_checks(lead.get("requirement_checks", []), task["requirements"], "requirement", sources, "requirement_checks", main)
        validate_checks(lead.get("supplier_fit", []), list(task["customer_profile"].get("supplier_constraints", {})), "constraint", sources, "supplier_fit", main, True)
        if main:
            location = lead.get("location_match", {})
            aliases = location.get("aliases", {})
            require(isinstance(aliases, dict) and set(aliases) <= {"city", "market", "address"}, "Invalid location_match.aliases")
            matched = []
            for index, scope in enumerate(scopes):
                if scope["country"] not in {"全球", lead["country"]}:
                    continue
                if all(normalized(lead.get(k, "")) == normalized(v) or (location.get("scope_index") == index and normalized(aliases.get(k, "")) == normalized(v)) for k, v in scope.items() if k != "country"):
                    matched.append(index)
            require(bool(matched), "main lead location outside or unknown for task geo_scope")
            def researched_scope(index):
                scope = scopes[index]
                for region in packet["region_research"]:
                    if region["status"] == "blocked" or region["country"] != lead["country"]:
                        continue
                    city_ok = not scope.get("city") or normalized(region.get("city", "")) == normalized(scope["city"]) or normalized(scope["city"]) in normalized(region["area"])
                    market_ok = not scope.get("market") or scope.get("city") or normalized(scope["market"]) in normalized(region["area"]) or any(normalized(x["name"]) == normalized(scope["market"]) for x in region["markets"])
                    if city_ok and market_ok:
                        return True
                return False
            require(any(researched_scope(index) for index in matched), "main needs region_research applicable to its city or market scope")
            if aliases:
                require(location.get("relation") == "within" and location.get("scope_index") in matched, "location_match must identify an in-scope position")
                nonempty(location.get("detail"), "location_match.detail")
                refs(location.get("source_ids", []), "location_match.source_ids", sources, strong=True)
        exclusion = lead.get("exclusion")
        require(lead["status"] == "excluded" or "exclusion" not in lead, "Non-excluded lead cannot carry current exclusion; use exclusion_resolution and retain history")
        if lead["status"] == "excluded":
            require(isinstance(exclusion, dict), "excluded needs documented exclusion, not an unsupported guess")
            require(exclusion.get("code") in {"brand_owner", "official_store", "brand_exclusive", "outside_scope", "product_mismatch", "buyer_mismatch", "closed", "user_excluded", "duplicate"}, "Invalid exclusion.code")
            nonempty(exclusion.get("detail"), "exclusion.detail")
            require(exclusion.get("origin") in {"evidence", "user"}, "Invalid exclusion.origin")
            if exclusion["origin"] == "user":
                require(exclusion["code"] == "user_excluded", "User exclusions must use user_excluded code")
                nonempty(exclusion.get("user_instruction"), "exclusion.user_instruction")
            else:
                refs(exclusion.get("source_ids", []), "exclusion.source_ids", sources, strong=True)
            require(exclusion["code"] not in BRAND_STORES or exclusion["code"] in excluded_brands(task), "This task collects brand stores; use the actual disqualifying condition or explicit user exclusion")
            if lead.get("brand_profile") in excluded_brands(task):
                require(exclusion["code"] == lead["brand_profile"] or exclusion["code"] == "user_excluded", "exclusion.code must match brand fact")
        canonical = lead.get("canonical_id")
        if canonical:
            valid_id(canonical, "canonical_id")
            require(canonical != lid and canonical in (ids | set(master["leads"])), "canonical_id must identify another existing entity")
            relation = lead.get("relationship_evidence", {})
            nonempty(relation.get("detail"), "relationship_evidence.detail")
            refs(relation.get("source_ids", []), "relationship_evidence.source_ids", sources, strong=True)
            require(lead["status"] == "excluded" and exclusion["code"] == "duplicate", "Merged alias must be excluded with duplicate reason")
        if exclusion and exclusion.get("code") == "duplicate":
            require(bool(canonical), "duplicate exclusion requires canonical_id")
        for contact in lead.get("contacts", []):
            kind, value = contact["kind"], contact["value"].strip()
            if kind == "whatsapp":
                require(valid_whatsapp(value), "Invalid WhatsApp phone or official contact URL")
            elif kind == "phone":
                require(bool(re.fullmatch(r"[+\d ()\-.]+", value)) and 6 <= len(re.sub(r"\D", "", value)) <= 18, "Invalid business phone format")
            if kind == "email":
                require(bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value)), "Invalid business email format")
            if kind == "website":
                require(contact.get("identity_scope", "unknown") in {"unknown", "owned_domain", "platform_store"}, "Invalid website identity_scope")
                if contact.get("identity_scope") == "owned_domain":
                    refs(contact.get("ownership_source_ids", []), "ownership_source_ids", sources, strong=True)
                if contact.get("identity_scope") == "platform_store":
                    nonempty(contact.get("platform"), "contact.platform")
                    require(bool(contact.get("seller_id") or urlsplit(value).path.strip("/")), "platform_store needs seller_id or store URL path")
            refs(contact.get("ownership_source_ids", []), "contact.ownership_source_ids", sources, required=False)
            if contact.get("contact_id"):
                valid_id(contact["contact_id"], "contact_id")
        entity = lead.get("entity_status", "unknown")
        require(entity in {"active", "unknown", "closed", "merged"}, "Invalid entity_status")
        if entity == "closed":
            require(exclusion and exclusion.get("code") == "closed", "closed entity needs closure evidence")
        if entity == "merged":
            require(bool(canonical), "merged entity needs canonical_id")


def merge_issues(old, incoming, unknowns=None):
    combined = {x["issue_id"]: copy.deepcopy(x) for x in old}
    for item in incoming:
        combined[item["issue_id"]] = copy.deepcopy(item)
    known_details = {x["detail"] for x in combined.values()}
    for detail in unknowns or []:
        if detail in known_details:
            continue
        known_details.add(detail)
        key = "legacy-" + digest_bytes(detail.encode("utf-8"))[:20]
        if key not in combined:
            combined[key] = {"issue_id": key, "kind": "legacy_unknown", "detail": detail, "state": "open", "attempts": 0, "next_action": "核对历史待核问题", "next_trigger": "下次相关采集时", "source_ids": []}
    return list(combined.values())


def normalized(value):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).casefold())


def canonical_url(value):
    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    if not host:
        return value.strip().rstrip("/")
    return host + parsed.path.rstrip("/") + ("?" + parsed.query if parsed.query else "")


def whatsapp_number(value):
    """Extract only a number explicitly exposed by an official URL, never infer it."""
    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
        return None
    if host == "wa.me" and re.fullmatch(r"/[0-9]{6,18}/?", parsed.path):
        return parsed.path.strip("/")
    if host == "api.whatsapp.com" and parsed.path.rstrip("/") == "/send":
        values = parse_qs(parsed.query).get("phone", [])
        if len(values) == 1 and re.fullmatch(r"[0-9]{6,18}", values[0]):
            return values[0]
    return None


def valid_whatsapp(value):
    if re.fullmatch(r"[+\d ()\-.]+", value) and 6 <= len(re.sub(r"\D", "", value)) <= 18:
        return True
    if whatsapp_number(value):
        return True
    parsed = urlsplit(value)
    return (parsed.scheme == "https" and parsed.hostname == "wa.me" and not parsed.username
            and not parsed.password and not parsed.port
            and bool(re.fullmatch(r"/message/[A-Za-z0-9_-]+/?", parsed.path)))


def contact_key(contact, country=""):
    kind, value = contact["kind"], contact["value"].strip()
    if kind == "whatsapp" and urlsplit(value).scheme:
        number = whatsapp_number(value)
        return "number:+" + number if number else "whatsapp:" + canonical_url(value)
    if kind in {"phone", "whatsapp"}:
        digits = re.sub(r"\D", "", value)
        # Never infer an international prefix for a local number.
        return "number:" + ("+" if value.startswith("+") else normalized(country) + ":local:") + digits
    if kind == "email":
        return "email:" + value.casefold()
    if kind == "website":
        scope = contact.get("identity_scope", "unknown")
        if scope == "owned_domain":
            host = (urlsplit(value).hostname or "").casefold().removeprefix("www.")
            return "domain:" + host if host else None
        # Generic hostnames are never merchant identities. Full store URLs can be.
        if scope == "platform_store":
            return "store:" + contact.get("platform", "").casefold() + ":" + ((urlsplit(value).hostname or "").casefold() + ":" + str(contact["seller_id"]) if contact.get("seller_id") else canonical_url(value))
        return None
    return kind + ":" + canonical_url(value)


def contact_identity(contact, country=""):
    key = contact_key(contact, country) or (contact["kind"] + ":" + canonical_url(contact["value"]))
    # Phone and WhatsApp may identify the same merchant, but remain separate entries.
    return "contact-" + digest_bytes((contact["kind"] + "|" + key).encode("utf-8"))[:24]


def canonical_entity(lid, leads):
    seen = set()
    while leads.get(lid, {}).get("canonical_id"):
        require(lid not in seen, "canonical_id cycle")
        seen.add(lid)
        lid = leads[lid]["canonical_id"]
        require(lid in leads, "canonical_id target missing")
    return lid


def duplicate_hints(leads, focus_ids=None):
    buckets = {}
    for lead in leads.values():
        entity = canonical_entity(lead["lead_id"], leads)
        keys = {"name:" + normalized(lead["name"])}
        keys.update("name:" + normalized(a) for a in lead.get("aliases", []) if isinstance(a, str))
        for contact in lead.get("contacts", []):
            if contact.get("status", "active") != "active":
                continue
            key = contact_key(contact, lead.get("country", ""))
            if key and not key.endswith(":"):
                keys.add(key)
        for key in keys:
            buckets.setdefault(key, set()).add(entity)
    focus = {canonical_entity(lid, leads) for lid in (focus_ids or leads)}
    return [{"matching_key": key, "lead_ids": sorted(ids), "strength": "weak" if key.startswith("name:") else "strong", "action": "review_relationship_do_not_auto_merge"} for key, ids in sorted(buckets.items()) if len(ids) > 1 and ids & focus]


def append_unique(old_items, new_items):
    combined = copy.deepcopy(old_items)
    seen = {json.dumps(item, ensure_ascii=False, sort_keys=True) for item in combined}
    for item in new_items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            combined.append(copy.deepcopy(item))
            seen.add(key)
    return combined


def merge_contacts(old_items, new_items, country="", sources=None, batch_id=None):
    combined = []
    for item in old_items:
        contact = copy.deepcopy(item)
        contact.setdefault("contact_id", contact_identity(contact, country))
        combined.append(contact)
    lookup = {contact_identity(c, country): i for i, c in enumerate(combined)}
    for incoming in new_items:
        key = contact_identity(incoming, country)
        if incoming.get("contact_id"):
            known = [x for x in combined if x["contact_id"] == incoming["contact_id"]]
            require(not known or contact_identity(known[0], country) == key, "contact_id cannot change identity; create a new contact")
            require(known or incoming["contact_id"] == key, "Unknown contact_id; omit to derive from canonical entry")
        replacement = copy.deepcopy(incoming)
        if key in lookup:
            old = combined[lookup[key]]
            replacement = dict(old, **replacement)
            replacement["contact_id"] = old["contact_id"]
            for field in ("source_ids", "whatsapp_source_ids", "action_source_ids", "ownership_source_ids"):
                if field in old or field in incoming:
                    replacement[field] = append_unique(old.get(field, []), incoming.get(field, []))
            replacement["value_variants"] = append_unique(old.get("value_variants", [old["value"]]), [incoming["value"]])
        else:
            replacement["contact_id"] = key
            replacement["value_variants"] = [incoming["value"]]
        replacement["status"] = incoming.get("status", "active")
        replacement["reachability"] = incoming.get("reachability", "untested")
        if key in lookup and (combined[lookup[key]].get("status") == "invalid" or combined[lookup[key]].get("reachability") == "unavailable") and incoming.get("status", "active") == "active" and incoming.get("reachability", "untested") == "untested":
            old_contact = combined[lookup[key]]
            novel = set(incoming.get("source_ids", [])) - set(old_contact.get("source_ids", []))
            cutoff = old_contact.get("invalidated_at", old_contact.get("last_verified_at", ""))
            require(sources and any(s in sources and sources[s]["level"] in STRONG and (not cutoff or datetime.fromisoformat(sources[s]["read_at"].replace("Z", "+00:00")) >= datetime.fromisoformat(cutoff.replace("Z", "+00:00"))) for s in novel), "Reactivating invalid contact requires new verification evidence after invalidation")
        if incoming.get("status") == "invalid":
            replacement["invalidated_at"] = now()
        if sources:
            old_refs = set(combined[lookup[key]].get("source_ids", [])) if key in lookup else set()
            times = [sources[s]["read_at"] for s in incoming.get("source_ids", []) if sources[s]["level"] in STRONG and s not in old_refs]
            if times:
                replacement["last_verified_at"] = max(times, key=lambda x: datetime.fromisoformat(x.replace("Z", "+00:00")))
                replacement["verified_batch_id"] = batch_id
        if key in lookup:
            combined[lookup[key]] = replacement
        else:
            lookup[key] = len(combined)
            combined.append(replacement)
    return combined


def merged_lead(old, incoming, packet, sources, stamp):
    bid = packet["batch_id"]
    lead = copy.deepcopy(old or {})
    lead.update(copy.deepcopy(incoming))
    key = scope_key(packet["task"])
    if lead.get("brand_profile") in excluded_brands(packet["task"]):
        require(incoming["status"] == "excluded", "Merged brand profile is excluded; explicitly correct brand_profile with evidence")
    old_exclusion = (old or {}).get("global_exclusion")
    if not old_exclusion and (old or {}).get("exclusion", {}).get("code") in EXCLUDED_BRANDS | {"closed"}:
        old_exclusion = old["exclusion"]
    if not old_exclusion and (old or {}).get("brand_profile") in BRAND_STORES and incoming.get("brand_profile") not in BRAND_STORES:
        old_exclusion = {"code": old["brand_profile"], "source_ids": sorted(lead_source_ids(old))}
    prior_scope = (old or {}).get("qualifications", {}).get(key, {})
    if not old_exclusion and prior_scope.get("status") == "excluded":
        old_exclusion = prior_scope.get("evidence_snapshot", {}).get("exclusion") or (old or {}).get("exclusion") or {"source_ids": prior_scope.get("source_ids", [])}
    if incoming["status"] != "excluded":
        policy_allows_prior_store = (old_exclusion and old_exclusion.get("code") in BRAND_STORES
                                    and incoming.get("brand_profile") in BRAND_STORES
                                    and incoming["brand_profile"] not in excluded_brands(packet["task"]))
        if old_exclusion and not policy_allows_prior_store:
            resolution = incoming.get("exclusion_resolution", {})
            nonempty(resolution.get("detail"), "exclusion_resolution.detail required to replace prior exclusion")
            refs(resolution.get("source_ids", []), "exclusion_resolution.source_ids", sources, strong=True)
            novel = set(resolution["source_ids"]) - set(old_exclusion.get("source_ids", []))
            require(any(sources[s]["level"] in STRONG for s in novel), "exclusion_resolution requires new strong evidence for replacement decision")
        lead.pop("exclusion", None)
        lead.pop("global_exclusion", None)
    if old:
        history = copy.deepcopy(old.get("history", []))
        history.append({k: v for k, v in old.items() if k != "history"})
        lead["history"] = history
        for field in ("manual_notes", "manual_owner", "sales_stage"):
            if field in old:
                lead[field] = old[field]
        for field in ("identity_source_ids", "location_source_ids", "product_evidence", "white_label_evidence", "aliases"):
            lead[field] = append_unique(old.get(field, []), incoming.get(field, []))
    else:
        lead.update(created_at=stamp, manual_notes=incoming.get("manual_notes", ""), manual_owner="", sales_stage="未联系")
    lead["contacts"] = merge_contacts((old or {}).get("contacts", []), incoming.get("contacts", []), incoming["country"], sources, bid)
    if incoming["status"] == "main":
        require(not lead.get("canonical_id"), "Canonical alias cannot qualify independently")
        require(any(actionable_contact(c, sources) for c in lead["contacts"]), "Merged main needs actionable contact")
        if (old or {}).get("entity_status") == "closed":
            require(incoming.get("entity_status") == "active", "Closed entity needs explicit evidence-backed active correction")
            require(bool(set(incoming.get("identity_source_ids", [])) - set(old.get("identity_source_ids", []))), "Reopening closed entity requires new identity evidence")
    lead["issues"] = merge_issues((old or {}).get("issues", []), incoming.get("issues", []), append_unique((old or {}).get("unknowns", []) if not (old or {}).get("issues") else [], incoming.get("unknowns", [])))
    lead["unknowns"] = [x["detail"] for x in lead["issues"] if x["state"] != "resolved"]
    if incoming["status"] == "main":
        blocking = [x["kind"] for x in lead["issues"] if issue_blocks(x, packet["task"], key)]
        require(not blocking, "Resolve qualification-blocking issues before main: " + ", ".join(blocking))
    qualifications = copy.deepcopy((old or {}).get("qualifications", {}))
    if old and not qualifications and old.get("status"):
        qualifications["legacy-unreviewed"] = {"status": old["status"], "priority": "", "qualified_products": [], "requires_review": True, "reason": "旧版全局资格保留，未按1.2复核"}
    priority, reason = derive_priority(incoming, sources, packet["task"])
    business = derive_business_priority(incoming, sources, packet["task"])
    qualifications[key] = {"status": incoming["status"], "priority": priority, "priority_reason": reason, "business_priority": business, "qualified_products": matched_products(incoming, packet["task"], sources) if incoming["status"] == "main" else [], "task": copy.deepcopy(packet["task"]), "batch_id": bid, "updated_at": stamp, "reason": incoming["reason"], "source_ids": sorted(lead_source_ids(incoming)), "evidence_snapshot": {k: copy.deepcopy(v) for k, v in incoming.items() if k not in {"history", "qualifications", "global_exclusion"}}}
    review_issues = lead["issues"] + [q for q in packet.get("queue", []) if q.get("lead_id") == incoming["lead_id"]]
    derived = dict(white_label_review(dict(incoming, issues=review_issues), packet["task"], sources), collection_segment=collection_segment(incoming, packet["task"]))
    qualifications[key].update(copy.deepcopy(derived))
    qualifications[key]["evidence_snapshot"].update(copy.deepcopy(derived))
    lead.update(derived)
    lead["qualifications"] = qualifications
    lead["last_task_status"] = incoming["status"]
    lead["last_scope_id"] = key
    lead["entity_status"] = "merged" if lead.get("canonical_id") else incoming.get("entity_status", (old or {}).get("entity_status", "unknown"))
    global_exclusion = incoming["status"] == "excluded" and incoming.get("exclusion", {}).get("code") in EXCLUDED_BRANDS | {"closed"}
    if global_exclusion:
        lead["global_exclusion"] = copy.deepcopy(incoming["exclusion"])
        # A confirmed closed or brand-only entity cannot retain actionable old qualification.
        for qkey, qualification in qualifications.items():
            incompatible = (incoming["exclusion"]["code"] == "closed"
                            or incoming["exclusion"]["code"] in excluded_brands(qualification.get("task", {}))
                            or collection_segment(qualification.get("evidence_snapshot", {}), qualification.get("task", {})) != "brand_store")
            if qkey != key and qualification["status"] == "main" and incompatible:
                qualification.update(status="candidate", priority="", requires_review=True, reason="主体事实变化，历史资格待复核")
    elif incoming["status"] == "main":
        lead.pop("global_exclusion", None)
        for qkey, qualification in qualifications.items():
            prior_segment = collection_segment(qualification.get("evidence_snapshot", {}), qualification.get("task", {}))
            if qkey != key and qualification["status"] == "main" and prior_segment in {"independent", "brand_store"} and prior_segment != derived["collection_segment"]:
                qualification.update(status="candidate", priority="", requires_review=True, reason="商户经营身份已有新证据，原范围须复核独立商户／品牌店归类")
    if not any(actionable_contact(c, sources) for c in lead["contacts"]):
        for qualification in qualifications.values():
            if qualification["status"] == "main":
                qualification.update(status="candidate", priority="", requires_review=True, reason="现有联系入口失效，资格待补核")
    for qkey, qualification in qualifications.items():
        if qualification["status"] == "main" and any(issue_blocks(x, qualification.get("task", {}), qkey) for x in lead["issues"]):
            qualification.update(status="candidate", priority="", requires_review=True, reason="关键资格事实冲突，待证据解决")
    # Compatibility field is aggregate, not the latest task decision.
    states = [q["status"] for q in qualifications.values() if not q.get("requires_review")]
    lead["status"] = "excluded" if lead.get("canonical_id") or lead.get("global_exclusion", {}).get("code") in {"brand_owner", "closed"} else ("main" if "main" in states else "candidate" if "candidate" in states else incoming["status"])
    lead["priority"], lead["priority_reason"] = priority, reason
    lead["business_priority"] = business
    lead["qualified_products"] = qualifications[key]["qualified_products"]
    lead.update(rules_version=VERSION, updated_at=stamp, last_batch_id=bid)
    return lead


def prepare_validation(packet, master):
    sources = validate_packet(packet, master)
    proposed = copy.deepcopy(master["leads"])
    queue = merge_issues(master.get("queue", []), packet["queue"])
    review_packet = dict(packet, queue=queue)
    for incoming in packet["leads"]:
        proposed[incoming["lead_id"]] = merged_lead(proposed.get(incoming["lead_id"]), incoming, review_packet, sources, now())
    for incoming in packet["leads"]:
        if incoming["status"] == "main":
            blocking = [q for q in queue if q.get("lead_id") == incoming["lead_id"] and issue_blocks(q, packet["task"])]
            require(not blocking, "Resolve qualification-blocking queue issues before main")
    for lead in proposed.values():
        linked = [q for q in queue if q.get("lead_id") == lead["lead_id"]]
        changed = False
        for qkey, qualification in lead.get("qualifications", {}).items():
            if qualification["status"] == "main" and any(issue_blocks(q, qualification.get("task", {}), qkey) for q in linked):
                qualification.update(status="candidate", priority="", requires_review=True, reason="接续队列存在关键资格冲突，待证据解决")
                changed = True
        if changed:
            states = [q["status"] for q in lead["qualifications"].values() if not q.get("requires_review")]
            lead["status"] = "excluded" if lead.get("canonical_id") or lead.get("global_exclusion", {}).get("code") in {"brand_owner", "closed"} else "main" if "main" in states else "candidate"
            if lead["status"] != "main":
                lead["priority"] = ""
    hints = duplicate_hints(proposed, [x["lead_id"] for x in packet["leads"]])
    for incoming in packet["leads"]:
        relevant = [h for h in hints if incoming["lead_id"] in h["lead_ids"] and h["strength"] == "strong"]
        if incoming["status"] == "main" and relevant:
            review = incoming.get("duplicate_review", {})
            require(review.get("decision") == "distinct_entities", "Potential duplicate main lead %s: reuse correct stable ID or document distinct_entities evidence" % incoming["lead_id"])
            nonempty(review.get("reason"), "duplicate_review.reason")
            refs(review.get("source_ids", []), "duplicate_review.source_ids", sources, strong=True)
            others = {lid for hint in relevant for lid in hint["lead_ids"] if lid != incoming["lead_id"]}
            require(others <= set(review.get("related_lead_ids", [])), "duplicate_review must cover every matched lead ID")
    return {"sources": sources, "leads": proposed, "duplicate_hints": hints}


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
        prepared = prepare_validation(packet, master)
        import leadkit_research as research
        research_summary = research.summary_for_commit(root, packet, master)
        require(packet["base_sha256"] == sha, "Master changed after snapshot; reread and reconcile before updating base_sha256")
        baseline = read_json(root / "research" / bid / "snapshot.json")
        require(baseline.get("base_sha256") == packet["base_sha256"], "Batch snapshot does not match. Create a new batch after reconciliation")
        require(baseline.get("batch_id") == bid, "Snapshot batch ID mismatch")
        stamp = now()
        counts = result_counts(packet["leads"], packet["task"])
        counts.update(new_entities=0, new_qualified_entities=0, updated_entities=0, first_qualified=0, requalified=0, new_contacts=0, new_sources=len([s for s in packet["sources"] if s["source_id"] not in master["sources"]]))
        if brand_policy(packet["task"]) == "independent_product":
            counts.update(new_independent_qualified_entities=0, new_brand_store_entities=0)
        key = scope_key(packet["task"])
        snapshots = []
        for incoming in packet["leads"]:
            lid = incoming["lead_id"]
            old = master["leads"].get(lid)
            lead = prepared["leads"][lid]
            if not old and not lead.get("canonical_id"):
                counts["new_entities"] += 1
                if incoming["status"] == "main":
                    counts["new_qualified_entities"] += 1
                    if brand_policy(packet["task"]) == "independent_product":
                        counts["new_independent_qualified_entities" if is_independent_main(incoming, packet["task"]) else "new_brand_store_entities"] += 1
            elif old and not lead.get("canonical_id"):
                counts["updated_entities"] += 1
            if incoming["status"] == "main" and key not in (old or {}).get("qualifications", {}):
                counts["first_qualified"] += 1
            if old:
                before = old.get("qualifications", {}).get(key, {}).get("status")
                if before in {"candidate", "excluded"} and incoming["status"] == "main":
                    counts["requalified"] += 1
            previous_contacts = {contact_identity(c, incoming["country"]) for c in (old or {}).get("contacts", [])}
            counts["new_contacts"] += sum(contact_identity(c, incoming["country"]) not in previous_contacts for c in lead["contacts"])
            # Freeze the current task result, never the aggregate entity compatibility status.
            frozen = copy.deepcopy(lead)
            frozen["status"] = incoming["status"]
            frozen["product_evidence"] = copy.deepcopy(incoming.get("product_evidence", []))
            frozen["white_label_evidence"] = copy.deepcopy(incoming.get("white_label_evidence", []))
            frozen["ongoing_activity"] = copy.deepcopy(incoming.get("ongoing_activity", {}))
            snapshots.append(frozen)
        master["leads"] = prepared["leads"]
        master["sources"] = prepared["sources"]
        ids = [x["lead_id"] for x in packet["leads"]]
        # Global follow-up issues are updated by stable identity; omission never closes one.
        master["queue"] = merge_issues(master.get("queue", []), packet["queue"])
        master["batches"][bid] = {"schema_version": 3, "rules_version": VERSION, "scope_id": key, "region_research": packet["region_research"], "packet_sha256": fingerprint, "committed_at": stamp, "task": packet["task"], "lead_ids": ids, "source_ids": [s["source_id"] for s in packet["sources"]], "lead_snapshots": snapshots, "queue": packet["queue"], "coverage": packet["coverage"], "limitations": packet["limitations"], "counts": counts, "duplicate_hints": prepared["duplicate_hints"]}
        if research_summary is not None:
            master["batches"][bid]["research_summary"] = research_summary
        master["updated_at"] = stamp
        backup_path = backup_master(root, sha)
        safe_write(root / "execution" / "master.json", master)
        saved, after_sha = load_master(root)
        require(saved["batches"][bid]["packet_sha256"] == fingerprint, "Saved batch read-back failed")
    return {"batch_id": bid, "already_committed": False, "counts": counts, "master_sha256": after_sha, "backup_path": str(backup_path), "duplicate_hints": prepared["duplicate_hints"]}


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
    for field in ("exclusion", "exclusion_resolution", "procurement_access", "relationship_evidence", "location_match", "market_assignment"):
        result.update(lead.get(field, {}).get("source_ids", []))
    for group in ("issues", "discovered_by", "requirement_checks", "supplier_fit"):
        for item in lead.get(group, []):
            result.update(item.get("source_ids", []))
    for contact in lead.get("contacts", []):
        result.update(contact.get("ownership_source_ids", []))
    return result


def regional_source_ids(region):
    result = set(region.get("source_ids", []))
    for group in ("markets", "communication_channels", "ecommerce_platforms"):
        for item in region.get(group, []):
            result.update(item.get("source_ids", []))
            result.update(item.get("size_source_ids", []))
    return result


def contact_display(contact, batch_id):
    state = contact.get("status", "active")
    label = {"historical": "历史入口", "invalid": "无效入口"}.get(state)
    if label is None:
        label = "本轮已核" if contact.get("verified_batch_id") == batch_id else "历史沿用未复核"
    return "%s：%s（%s；%s；%s；末次核验：%s；%s；编号：%s）[%s]" % (contact["kind"], contact["value"], contact["role"], contact["attribution"], label, contact.get("last_verified_at", "历史未记录"), "已知不可用" if contact.get("reachability") == "unavailable" else "可达未测试", contact.get("contact_id", "旧版未编号"), " ".join(contact["source_ids"]))


def export_batch(root, batch_id, output):
    master, _ = load_master(root)
    require(batch_id in master["batches"], "Unknown committed batch")
    batch = master["batches"][batch_id]
    return export_view(master, batch, batch_id, output)


def current_scope_view(master, scope_id):
    """Rebuild from each qualification's own facts; never borrow a later task's facts."""
    result = []
    for entity in master["leads"].values():
        qualification = entity.get("qualifications", {}).get(scope_id)
        if not qualification:
            continue
        batch = master["batches"].get(qualification.get("batch_id"), {})
        facts = qualification.get("evidence_snapshot")
        if facts is None:
            facts = next((x for x in batch.get("lead_snapshots", []) if x["lead_id"] == entity["lead_id"]), None)
        task = qualification.get("task") or batch.get("task", {})
        missing = not facts or not task or scope_key(task) != scope_id
        lead = copy.deepcopy(facts) if not missing else {k: entity[k] for k in ("lead_id", "name", "country")}
        lead["status"] = qualification.get("status", "candidate")
        lead.setdefault("reason", qualification.get("reason", "历史资格缺少独立证据快照"))
        for field in ("manual_notes", "manual_owner", "sales_stage", "contacts", "entity_status", "canonical_id", "global_exclusion"):
            if field in entity:
                lead[field] = copy.deepcopy(entity[field])
        issues = merge_issues(lead.get("issues", []), entity.get("issues", []))
        issues = merge_issues(issues, [q for q in master.get("queue", []) if q.get("lead_id") == entity["lead_id"]])
        lead["issues"] = issues
        lead["unknowns"] = [x["detail"] for x in issues if x.get("state") != "resolved"]
        reasons = []
        if missing:
            reasons.append("该资格范围证据快照缺失，禁止使用其他任务事实替代")
        if qualification.get("requires_review"):
            reasons.append(qualification.get("reason", "当前资格待复核"))
        if any(issue_blocks(x, task, scope_id) for x in issues):
            reasons.append("存在未解决的关键资格冲突")
        if not any(actionable_contact(c, master["sources"]) for c in lead.get("contacts", [])):
            reasons.append("当前没有已核可操作联系入口")
        global_exclusion = entity.get("global_exclusion", {})
        exclusion_applies = bool(global_exclusion) and not (global_exclusion.get("code") in BRAND_STORES and global_exclusion["code"] not in excluded_brands(task))
        if entity.get("canonical_id") or entity.get("entity_status") in {"closed", "merged"} or exclusion_applies or entity.get("brand_profile") in excluded_brands(task):
            lead["status"] = "excluded"
        elif reasons and lead["status"] == "main":
            lead["status"] = "candidate"
        if reasons:
            lead["unknowns"] = append_unique(lead["unknowns"], reasons)
        lead["qualified_products"] = matched_products(lead, task, master["sources"]) if lead["status"] == "main" else []
        lead["priority"], lead["priority_reason"] = derive_priority(lead, master["sources"], task)
        lead["business_priority"] = derive_business_priority(lead, master["sources"], task)
        lead.update(white_label_review(lead, task, master["sources"]), collection_segment=collection_segment(lead, task))
        lead["qualification_batch_id"] = qualification.get("batch_id", "")
        result.append(lead)
    return result


def export_current(root, output, scope_id=None, batch_id=None):
    require(bool(scope_id) != bool(batch_id), "Specify exactly one scope_id or batch_id")
    master, _ = load_master(root)
    if batch_id:
        require(batch_id in master["batches"], "Unknown committed batch")
        reference = master["batches"][batch_id]
        scope_id = reference.get("scope_id")
        require(bool(scope_id), "Historical batch lacks a scoped qualification; use frozen export")
    else:
        valid_id(scope_id, "scope_id")
        matches = [(bid, b) for bid, b in master["batches"].items() if b.get("scope_id") == scope_id]
        require(bool(matches), "Unknown qualification scope")
        batch_id, reference = max(matches, key=lambda item: item[1].get("committed_at", ""))
    view = copy.deepcopy(reference)
    leads = current_scope_view(master, scope_id)
    view.update(view="current", lead_snapshots=leads, committed_at=now(), source_ids=[],
                counts=result_counts(leads, reference["task"]))
    view["limitations"] = append_unique(view.get("limitations", []), ["当前视图按各资格范围原证据与主库当前状态重建；不会重新联网核实。历史批次及历史导出保持不变。"])
    view["queue"] = [q for q in master.get("queue", []) if q.get("lead_id") in {x["lead_id"] for x in leads}]
    result = export_view(master, view, batch_id, output)
    return dict(result, view="current", scope_id=scope_id)


def export_view(master, batch, batch_id, output):
    leads = batch["lead_snapshots"]
    legacy = batch.get("schema_version", 1) < 2
    independent_policy = not legacy and brand_policy(batch["task"]) == "independent_product"
    combined_view = batch.get("view") in {"current", "market_delivery"}
    if not legacy:
        leads = sorted(leads, key=lambda x: business_sort_key(x, batch["task"]))
    header = ["客户编号", "商户名称", "国家", "城市", "地址／精度", "经营角色", "匹配产品及证据摘要", "公开业务入口", "适配理由", "待核问题", "来源编号", "人工备注"]
    header += ["品牌经营类型", "白牌经营证据", "所在市场／位置精度", "近期经营依据", "开发优先级", "评级理由", "本任务合格产品", "供货条件核对"]
    widths = [18, 28, 16, 18, 34, 22, 52, 52, 42, 42, 28, 32, 22, 52, 34, 52, 18, 60, 30, 55]
    header += ["业务适配依据", "本次业务排序偏好"]
    widths += [85, 40]
    output_rows = {"main": [header], "candidate": [header]}
    if independent_policy:
        header += ["白牌核实状态", "本任务已核白牌产品"]
        widths += [22, 38]
        output_rows["brand_store"] = [header]
    for lead in leads:
        if lead["status"] not in output_rows:
            continue
        row_group = "brand_store" if independent_policy and lead["status"] == "main" and collection_segment(lead, batch["task"]) == "brand_store" else lead["status"]
        brand_label = BRAND_LABELS.get(lead.get("brand_profile"), "旧版未评估" if legacy else "品牌类型待核")
        if independent_policy and collection_segment(lead, batch["task"]) == "independent":
            brand_label = "独立商户"
        evidence = "\n".join("%s：%s [%s]" % (x["product"], x["detail"], " ".join(x["source_ids"])) for x in lead.get("product_evidence", []))
        contacts = "\n".join(contact_display(x, lead.get("qualification_batch_id", batch_id)) for x in lead.get("contacts", []))
        output_rows[row_group].append([lead["lead_id"], lead["name"], lead["country"], lead.get("city", ""), lead.get("address", "未知"), lead.get("buyer_type", "未知"), evidence or lead.get("channel_fit", "未知"), contacts, lead["reason"], "\n".join(lead.get("unknowns", [])), " ".join(sorted(lead_source_ids(lead))), lead.get("manual_notes", ""), BRAND_LABELS.get(lead.get("brand_profile"), "旧版未评估" if legacy else "品牌类型待核"), "\n".join("%s：%s [%s]" % (x["product"], x["detail"], " ".join(x["source_ids"])) for x in lead.get("white_label_evidence", [])), lead.get("market_location", "未明确"), (lead.get("ongoing_activity", {}).get("detail", "未明确") + "；日期：" + lead.get("ongoing_activity", {}).get("observed_on", "未公开") + " [" + " ".join(lead.get("ongoing_activity", {}).get("source_ids", [])) + "]"), "旧版未评级" if legacy else (lead.get("priority") or "未评级"), "历史批次，未按1.1规则重新资格核验" if legacy else lead.get("priority_reason", "未评级"), "、".join(lead.get("qualified_products", [])) or "历史未单列", "\n".join("%s：%s；%s" % (x["constraint"], {"matched":"已匹配","unknown":"未知","conflict":"冲突"}[x["status"]], x["detail"]) for x in lead.get("supplier_fit", [])) or "未配置或历史未核对"])
        output_rows[row_group][-1] += [lead.get("business_priority", {}).get("reason", "历史未单列"), "、".join(batch["task"].get("business_priorities", [])) or "按原A/B顺序；未设置额外业务偏好"]
        output_rows[row_group][-1][12] = brand_label
        if independent_policy:
            review_lead = dict(lead, issues=lead.get("issues", []) + [q for q in batch.get("queue", []) if q.get("lead_id") == lead["lead_id"]])
            # Committed snapshots include conflicts known at that batch, including
            # earlier queue items omitted from the incremental packet itself.
            review = {k: lead[k] for k in ("white_label_status", "white_label_products")} if all(k in lead for k in ("white_label_status", "white_label_products")) else white_label_review(review_lead, batch["task"], master["sources"])
            output_rows[row_group][-1] += ["已核" if review["white_label_status"] == "verified" else "待核", "、".join(review["white_label_products"]) or "未取得本任务产品白牌证据"]
    source_rows = [["来源编号", "关联客户", "来源标题", "来源网址", "读取时间", "读取层级", "支持内容", "限制／资料日期"]]
    wanted = set(batch["source_ids"])
    for lead in leads:
        wanted.update(lead_source_ids(lead))
    for region in batch.get("region_research", []):
        wanted.update(regional_source_ids(region))
    for cell in batch.get("coverage", []):
        wanted.update(cell.get("source_ids", []))
    for sid in sorted(wanted):
        source = master["sources"][sid]
        linked = " ".join(x["lead_id"] for x in leads if sid in lead_source_ids(x))
        regional_links = ["地区预研：" + r["area"] for r in batch.get("region_research", []) if sid in regional_source_ids(r)]
        linked = " / ".join([linked] + regional_links).strip(" / ")
        source_rows.append([sid, linked, source["title"], source["url"], source["read_at"], source["level"], source["excerpt"], "\n".join("；".join(str(x) for x in source[k]) if isinstance(source[k], list) else str(source[k]) for k in ("limitations", "content_date") if source.get(k))])
    notes = [["项目", "内容"], ["批次编号", batch_id], ["原始需求", batch["task"]["request"]], ["目标国家", "、".join(batch["task"]["countries"])], ["目标产品", "、".join(batch["task"]["products"])], ["本轮结果", "可开发候选 %d；待核 %d；后台排除 %d" % (batch["counts"]["main"], batch["counts"]["candidate"], batch["counts"]["excluded"])], ["结果边界", "可开发客户为符合本次研究条件的候选，不代表采购意愿、采购权限或可达性已确认。本技能未外联。"], ["已知限制", "\n".join(batch["limitations"]) or "无额外记录"], ["接续事项", "\n".join(q["next_action"] for q in batch["queue"] if q.get("state") != "resolved") or "无记录"], ["查重提示", "\n".join("%s：%s，关系待复核，未自动合并" % (h["matching_key"], " / ".join(h["lead_ids"])) for h in batch["duplicate_hints"]) or "无匹配提示"], ["数据时间", batch["committed_at"]]]
    if batch["task"].get("supplementary_products"):
        notes.append(["附带拓展品类", "、".join(batch["task"]["supplementary_products"]) + "；同渠道顺带保留，本轮主线合格数不含仅匹配这些品类的商户"])
    profile = batch["task"].get("customer_profile", {})
    if independent_policy:
        counts = result_counts(leads, batch["task"])
        notes = [row if row[0] != "本轮结果" else ["本轮结果", "独立商户 %d；品牌店另列 %d；待核 %d；后台排除 %d" % tuple(counts[k] for k in ("independent_main", "brand_store_main", "candidate", "excluded"))] for row in notes]
        notes += [["白牌核实口径", "白牌单独列已核／待核；只有对应本任务产品的强证据支持已核，不影响独立商户产品准入。品牌店单列，不充独立商户阶段目标。"]]
    if batch.get("view") == "current":
        notes += [["导出视图", "当前有效资格；参考批次仅定位范围，不是当时冻结结果"], ["统计口径", "当前主库该范围全部资格的现状，新增数请查历史批次说明"]]
    elif batch.get("view") == "market_delivery":
        notes += [["导出视图", "截至参考批次的市场合并交付；仅合并该资格范围截至此批次的已保存市场证据"], ["统计口径", "本市场截至参考批次的去重结果，不是本批增量或当前全部范围资格；新增数请查历史批次说明"]]
    summary = batch.get("research_summary")
    market_context = batch.get("market_context") or (summary or {}).get("market_sequence", {}).get("active_market") or {}
    if market_context:
        state_labels = {"open": "未开始", "active": "采集中", "complete": "已完成登记边界", "blocked": "受阻未完成", "needs_research": "待预研", "deferred": "暂缓未完成"}
        notes += [["市场所属国家", market_context.get("country", "待核")], ["市场所属城市", market_context.get("city", "待核")], ["本表市场", market_context.get("market", "待定位")], ["市场编号", market_context.get("market_id", "未定位")], ["当前市场状态", state_labels.get(market_context.get("state"), market_context.get("state", "待核"))], ["市场采集边界", market_context.get("boundary", "未明确；不代表全市场穷尽")]]
    if summary:
        if not combined_view:
            notes += [["本轮阶段目标", "%s：%s" % (summary.get("policy", {}).get("target_metric", "未知"), summary.get("policy", {}).get("target_count", "未知"))], ["实际目标计数", str(summary.get("target_observed", "未记录"))], ["本主库新增合格主体", str(summary.get("counts", {}).get("new_qualified", "未记录"))]]
        if summary.get("scope_completion"):
            notes.append(["渠道覆盖完成情况", "阶段结果；仍有未研究类别或未完成路线" if summary["scope_completion"] == "partial" else "已完成本次登记的路线边界，不代表全城或互联网穷尽"])
            notes.append(["未完成路线", "、".join(summary.get("unfinished_routes", [])) or "无登记未完成路线"])
            notes.append(["待补渠道类别", "、".join(summary.get("unresolved_categories", [])) or "无登记待补类别"])
        if summary.get("stop_detail"):
            notes.append(["本轮停止说明", summary["stop_detail"]])
        for field in ("stop_reason", "attempts", "elapsed_minutes"):
            if field in summary:
                notes.append(["采集进度：" + field, str(summary[field])])
    policy_note = "独立商户须有目标产品及其他准入证据；白牌单独标记。品牌方排除；官方店／专卖店" + ("合格后另表收录" if profile.get("collect_brand_stores", False) else "本任务不收录") if independent_policy else "目标产品白牌经营有据；混合经营须有白牌证据；品牌方／官方店／专卖店排除" if not legacy else "历史规则，未按新门槛复核"
    notes += [["资格规则版本", batch.get("rules_version", "1.0.0（历史批次未重新核验）")], ["目标客户类型", "、".join(profile.get("buyer_types", [])) or "历史批次未单列"], ["品牌准入", policy_note], ["优先级说明", "A/B仅为已通过准入候选的研究交接优先级，不是成交概率；无评级不等于低价值。旧批次保留历史资格，不自动晋级。"]]
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
    if batch.get("schema_version", 1) >= 3:
        counts = batch["counts"]
        notes += [["增量统计", "新主体 %d；更新主体 %d；首次该范围合格 %d；同范围重新合格 %d；新增联系入口 %d；新增来源 %d" % tuple(counts.get(k, 0) for k in ("new_entities", "updated_entities", "first_qualified", "requalified", "new_contacts", "new_sources"))], ["资格范围", batch.get("scope_id", "")], ["活动新鲜度窗口", str(batch["task"].get("activity_max_age_days", 180)) + " 天；按实际活动日期核对，不按页面读取日"]]
        if not combined_view:
            if independent_policy:
                notes += [["本主库新增独立商户", str(counts.get("new_independent_qualified_entities", "未单列"))], ["本主库新增品牌店", str(counts.get("new_brand_store_entities", "未单列"))]]
            else:
                notes.append(["本主库新增合格客户", str(counts.get("new_qualified_entities", "历史未单列"))])
        else:
            notes = [row for row in notes if row[0] != "增量统计"]
        for cell in batch.get("coverage", []):
            notes.append(["渠道覆盖：" + cell["product"] + "／" + cell["channel"], "%s；尝试 %d 次；关联客户：%s；限制：%s" % ({"unsearched":"未搜索", "blocked":"受阻", "searched_zero":"已搜索无合格", "searched":"已发现合格"}[cell["status"]], cell["attempts"], "、".join(cell.get("discovered_lead_ids", [])) or "无", "；".join(cell.get("limitations", [])) or "无")])
        for cell in batch.get("coverage", []):
            for reference in cell.get("input_references", []):
                notes.append(["员工提供线索：" + cell["channel"], "文件：%s；工作表：%s；行：%s；仅发现出处，不是主体真实性或经营事实证据" % (reference["file"], reference.get("sheet", "未记录"), reference.get("row", "未记录"))])
    context = batch["task"].get("project_context", {})
    if context.get("additional_sources"):
        notes.append(["跨库查重边界", "另有登记的历史或补充来源；本表新增数仅相对当前JSON主库，未自动证明企业净新增。须对照原件核主体，不能把安装或路径登记当作已完成查重。"])
    sheets = [("可开发客户", output_rows["main"], widths)]
    if independent_policy:
        sheets.append(("品牌店", output_rows["brand_store"], widths))
    sheets += [("待核线索", output_rows["candidate"], widths), ("来源证据", source_rows, [18, 24, 34, 62, 28, 24, 80, 50]), ("采集说明", notes, [24, 100])]
    write_xlsx(output, sheets)
    return {"output": str(Path(output).resolve()), "batch_id": batch_id, "counts": batch["counts"], "sheets": len(sheets)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "snapshot", "validate", "commit", "dedupe", "export", "export-current"):
        sub = subs.add_parser(name)
        sub.add_argument("--workspace", required=True, help="Customer project directory, separate from installed skill")
        if name in {"snapshot", "export"}:
            sub.add_argument("--batch-id", required=True)
        if name in {"validate", "commit"}:
            sub.add_argument("--batch", required=True, help="Codex-authored evidence batch JSON")
        if name in {"export", "export-current"}:
            sub.add_argument("--output", required=True)
        if name == "export-current":
            scope = sub.add_mutually_exclusive_group(required=True)
            scope.add_argument("--batch-id")
            scope.add_argument("--scope-id")
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
            prepare_validation(packet, master)
            result = {"valid": True, "base_matches_current": packet["base_sha256"] == sha, "fact_truth_verified": False}
        elif args.command == "dedupe":
            master, _ = load_master(root)
            result = {"hints": duplicate_hints(master["leads"]), "automatically_merged": False}
        elif args.command == "export-current":
            result = export_current(root, args.output, args.scope_id, args.batch_id)
        else:
            result = export_batch(root, args.batch_id, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (LeadkitError, OSError, ValueError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.modules["leadkit"] = sys.modules[__name__]
    sys.exit(main())
