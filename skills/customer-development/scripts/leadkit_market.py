#!/usr/bin/env python3
"""Country/city/market delivery views over the one existing customer master.

No research, qualification changes, or second customer database. Frozen batches
remain unchanged. Unknown market membership is delivered separately.
"""
import argparse
import copy
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import leadkit as core

STATE_LABELS = {"open": "未开始", "active": "采集中", "complete": "已完成登记边界",
                "blocked": "受阻未完成", "needs_research": "待预研", "deferred": "暂缓未完成"}


def directory_maps(directory):
    core.require(isinstance(directory, dict), "Market directory must be an object")
    result = []
    for kind, key in (("countries", "country_id"), ("cities", "city_id"), ("markets", "market_id")):
        items = directory.get(kind, [])
        core.require(isinstance(items, list), "Directory %s must be an array" % kind)
        mapped = {}
        for item in items:
            core.require(isinstance(item, dict), "Directory items must be objects")
            core.valid_id(item.get(key), key)
            core.nonempty(item.get("name"), kind + ".name")
            core.require(item[key] not in mapped, "Duplicate directory ID: " + item[key])
            mapped[item[key]] = item
        result.append(mapped)
    countries, cities, markets = result
    core.require(all(x.get("country_id") in countries for x in cities.values()), "City has no registered country")
    core.require(all(x.get("city_id") in cities for x in markets.values()), "Market has no registered city")
    return countries, cities, markets


def _order(item):
    value = item.get("order")
    return (value if type(value) is int and value > 0 else float("inf"), item["name"])


def _commit_time(batch):
    value = batch.get("committed_at")
    core.nonempty(value, "batch.committed_at")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise core.LeadkitError("Invalid batch.committed_at timestamp") from exc
    core.require(result.tzinfo is not None, "batch.committed_at needs timezone")
    return result.astimezone(timezone.utc)


def _historical_entities(timeline):
    """Resolve only identity links present in committed snapshots by the cutoff."""
    entities = {}
    for _, batch in timeline:
        for lead in batch.get("lead_snapshots", []):
            entities[lead["lead_id"]] = {key: lead[key] for key in ("lead_id", "canonical_id") if key in lead}
    # A recorded relation may reference a historical entity without its own
    # snapshot; its identity is usable, but no current/future facts are borrowed.
    for entity in list(entities.values()):
        if entity.get("canonical_id"):
            entities.setdefault(entity["canonical_id"], {"lead_id": entity["canonical_id"]})
    return entities


def _entity_reviews(timeline, identities, sources):
    """Replay entity-wide decisions, including those saved under other scopes."""
    reviews, identity_sources = {}, {}
    for bid, batch in timeline:
        for lead in batch.get("lead_snapshots", []):
            key = core.canonical_entity(lead["lead_id"], identities)
            qualification = lead.get("qualifications", {}).get(batch.get("scope_id"), {})
            # Merged snapshots may inherit an old resolution. Only the current
            # qualification's explicit input can reverse an exclusion.
            facts = qualification.get("evidence_snapshot") if qualification.get("batch_id") == bid else lead
            if not facts:
                facts = {k: lead[k] for k in ("status", "exclusion") if k in lead}
            novel_identity = set(facts.get("identity_source_ids", [])) - identity_sources.get(key, set())
            identity_sources.setdefault(key, set()).update(facts.get("identity_source_ids", []))
            exclusion = facts.get("exclusion", {})
            if facts.get("status") == "excluded" and exclusion.get("code") in {"closed", "brand_owner"}:
                reviews[key] = {"excluded": True, "exclusion": copy.deepcopy(exclusion), "batch_id": bid,
                                "source_ids": set(exclusion.get("source_ids", []))}
                continue
            prior = reviews.get(key)
            if not prior or not prior["excluded"] or facts.get("status") == "excluded":
                continue
            resolution = facts.get("exclusion_resolution", {})
            novel = set(resolution.get("source_ids", [])) - set(prior["exclusion"].get("source_ids", []))
            supported = bool(resolution.get("detail")) and any(sources.get(s, {}).get("level") in core.STRONG for s in novel)
            if prior["exclusion"]["code"] == "closed":
                supported = supported and facts.get("entity_status") == "active" and any(
                    sources.get(s, {}).get("level") in core.STRONG for s in novel_identity)
            else:
                supported = supported and facts.get("brand_profile") in ({"independent", "white_label", "mixed"} | core.BRAND_STORES)
            if supported:
                prior.update(excluded=False, restored_at=(_commit_time(batch), bid))
                prior["source_ids"].update(resolution.get("source_ids", []))
    return reviews


def apply_entity_review(lead, review, batches):
    """Apply a global decision without importing another scope's product facts."""
    if not review:
        return
    lead["export_source_ids"] = sorted(set(lead.get("export_source_ids", [])) | review["source_ids"])
    if review["excluded"]:
        lead.update(status="excluded", exclusion=copy.deepcopy(review["exclusion"]), qualified_products=[], priority="")
        return
    bid = lead.get("qualification_batch_id")
    qualification_order = (_commit_time(batches[bid]), bid) if bid in batches else None
    if qualification_order is None or qualification_order < review["restored_at"]:
        if lead.get("status") in {"main", "candidate"} or lead.get("exclusion", {}).get("code") in {"closed", "brand_owner"}:
            lead.update(status="candidate", qualified_products=[], priority="")
            lead.pop("exclusion", None)
            lead["unknowns"] = core.append_unique(lead.get("unknowns", []), ["全主体排除已有明确恢复证据；本范围资格早于恢复，须重新核验，不能借其他范围事实自动恢复正式资格"])


def directory_tables(directory, grouped=None, active_market_id=None):
    """Reader-facing overview; aggregate canonical IDs, never sum market counts."""
    countries, cities, markets = directory_maps(directory)
    grouped = grouped or {}

    def entity_sets(mids):
        sets = {k: set() for k in ("independent", "brand_store", "candidate")}
        for mid in mids:
            for key in sets:
                sets[key].update(grouped.get(mid, {}).get(key, set()))
        sets["candidate"] -= sets["independent"] | sets["brand_store"]
        return sets

    def totals(mids):
        sets = entity_sets(mids)
        return [len(sets[k]) for k in ("independent", "brand_store", "candidate")]

    def qualified_total(mids):
        sets = entity_sets(mids)
        overlap = sets["independent"] & sets["brand_store"]
        return [len(sets["independent"] | sets["brand_store"]), "分类数可能重叠，不能相加；跨分类同主体 %d 个；各市场保留当时证据" % len(overlap)]

    country_rows = [["国家", "已登记城市数", "已登记市场数", "完成登记边界的市场", "独立商户", "品牌店", "待核主体", "目录范围", "合格主体去重总数", "分类计数口径"]]
    city_rows = [["国家", "城市／地区", "研究顺序", "已登记市场数", "完成登记边界的市场", "独立商户", "品牌店", "待核主体", "合格主体去重总数", "分类计数口径"]]
    market_rows = [["国家", "城市／地区", "市场／批发城", "市场顺序", "状态", "独立商户", "品牌店", "待核主体", "采集边界", "下一步", "合格主体去重总数", "分类计数口径"]]
    coverage = directory.get("coverage", {})
    coverage_note = coverage.get("detail") or "只列已登记范围，尚未确认完整国家地区及市场目录"
    for cid, country in sorted(countries.items(), key=lambda x: _order(x[1])):
        city_ids = [key for key, value in cities.items() if value["country_id"] == cid]
        mids = [key for key, value in markets.items() if value["city_id"] in city_ids]
        country_rows.append([country["name"], len(city_ids), len(mids), sum(markets[m].get("state") == "complete" for m in mids)] + totals(mids) + [coverage_note] + qualified_total(mids))
    for city_id, city in sorted(cities.items(), key=lambda x: (_order(countries[x[1]["country_id"]]), _order(x[1]))):
        mids = [key for key, value in markets.items() if value["city_id"] == city_id]
        country = countries[city["country_id"]]
        city_rows.append([country["name"], city["name"], city.get("order") or "待排序", len(mids), sum(markets[m].get("state") == "complete" for m in mids)] + totals(mids) + qualified_total(mids))
        for mid in sorted(mids, key=lambda key: _order(markets[key])):
            market = markets[mid]
            progress = market.get("progress") or {}
            state = market.get("state", "open")
            if mid == active_market_id and state == "open":
                state = "active"
            market_rows.append([country["name"], city["name"], market["name"], market.get("order") or "待规模核验", STATE_LABELS.get(state, state)] + totals([mid]) + [market.get("boundary", "待核"), progress.get("next_action") or market.get("next_action") or ("已完成登记边界" if state == "complete" else "核真实未读入口，完成当前市场后推进下一市场")] + qualified_total([mid]))
    return [("国家目录", country_rows, [20, 20, 20, 26, 18, 18, 18, 70, 24, 70]),
            ("城市目录", city_rows, [20, 26, 18, 22, 26, 18, 18, 18, 24, 70]),
            ("市场进度", market_rows, [20, 26, 34, 20, 24, 18, 18, 18, 55, 65, 24, 70])]


def bundle_views(master, batch_id):
    core.require(batch_id in master["batches"], "Unknown committed batch")
    reference = master["batches"][batch_id]
    sequence = reference.get("research_summary", {}).get("market_sequence") or {}
    directory = sequence.get("directory")
    core.require(directory is not None, "Market delivery needs a committed market_sequence directory")
    countries, cities, markets = directory_maps(directory)
    core.require(bool(reference.get("scope_id")), "Market delivery needs a frozen qualification scope")
    cutoff = _commit_time(reference)
    timeline = sorted([(bid, batch) for bid, batch in master["batches"].items()
                       if batch.get("committed_at") and _commit_time(batch) <= cutoff],
                      key=lambda pair: (_commit_time(pair[1]), pair[0]))
    identities = _historical_entities(timeline)
    reviews = _entity_reviews(timeline, identities, master["sources"])
    relevant = [(bid, b) for bid, b in timeline
                if b.get("scope_id") == reference["scope_id"]
                and b.get("task", {}).get("collection_strategy") == "market_sequence"]
    per_market, unassigned = {}, {}

    def remember(records, key, lead):
        previous = records.get(key)
        if previous and previous.get("recorded_lead_id") == key and lead["recorded_lead_id"] != key:
            return
        # Prefer the canonical record's own decision over an older duplicate
        # candidate. Updates to that canonical record still replace its old row.
        records[key] = lead

    for bid, batch in relevant:
        seq = batch.get("research_summary", {}).get("market_sequence") or {}
        active = seq.get("batch_market_id") or seq.get("active_market_id")
        core.require(active is None or active in markets, "Committed batch market is missing from the directory")
        if active is not None:
            # A completed/blocked attempt with zero merchant rows is still a
            # market result and must receive its own workbook and boundary.
            per_market.setdefault(active, {})
        for original in batch.get("lead_snapshots", []):
            lead = copy.deepcopy(original)
            assignment = lead.get("market_assignment") or {}
            mid = assignment.get("market_id")
            located = mid in markets and assignment.get("relation") == "within"
            if lead["status"] == "main":
                core.require(located and mid == active, "Qualified record cannot be assigned to a guessed or different market")
            key = core.canonical_entity(lead["lead_id"], identities)
            if lead.get("status") == "excluded" and lead.get("canonical_id") and lead.get("exclusion", {}).get("code") == "duplicate":
                # Identity consolidation is not a withdrawal of the canonical
                # merchant's separately evidenced qualification or branch.
                continue
            lead["recorded_lead_id"] = lead["lead_id"]
            lead["lead_id"] = key
            lead["qualification_batch_id"] = bid
            if located:
                remember(per_market.setdefault(mid, {}), key, lead)
                unassigned.pop(key, None)
            elif not any(key in records for records in per_market.values()):
                remember(unassigned, key, lead)

    for records in list(per_market.values()) + [unassigned]:
        for key, lead in records.items():
            apply_entity_review(lead, reviews.get(key), master["batches"])

    def view(records, context):
        value = copy.deepcopy(reference)
        value["lead_snapshots"] = list(records.values())
        value["view"] = "market_delivery"
        value["source_ids"] = []
        value["coverage"] = []
        value["region_research"] = []
        value["queue"] = []
        value["duplicate_hints"] = []
        value["counts"] = core.result_counts(list(records.values()), value["task"])
        value["research_summary"] = {"market_sequence": {"active_market": context}, "scope_completion": "partial"}
        value["market_context"] = context
        value["limitations"] = ["按所选批次及之前的市场证据合并交付，未重新联网；跨市场同主体使用截止该批次已保存的身份关系去重，不借用此后更正。", "国家城市目录仅代表所登记范围，市场完成只指记录的采集边界。"]
        return value

    views, grouped = {}, {}
    for mid, records in per_market.items():
        market = markets[mid]
        city = cities[market["city_id"]]
        country = countries[city["country_id"]]
        context = {"country_id": country["country_id"], "city_id": city["city_id"], "market_id": mid,
                   "country": country["name"], "city": city["name"], "market": market["name"], "state": market.get("state", "open"), "boundary": market.get("boundary", "待核")}
        views[mid] = view(records, context)
        grouped[mid] = {"independent": set(), "brand_store": set(), "candidate": set()}
        for key, lead in records.items():
            if lead["status"] == "main":
                grouped[mid][core.collection_segment(lead, reference["task"])].add(key)
            elif lead["status"] == "candidate":
                grouped[mid]["candidate"].add(key)
    return directory, views, view(unassigned, {"market": "待市场定位"}), grouped


def _component(identifier, name):
    safe = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name).strip(" .")[:70]
    # Filesystems limit bytes, not Unicode character count. Leave room for the
    # workbook extension even with long stable IDs and Chinese market names.
    limit = 240 - len(identifier.encode("utf-8")) - 1
    while len(safe.encode("utf-8")) > limit:
        safe = safe[:-1]
    return identifier + "-" + (safe or "未命名")


def single_view(master, batch_id):
    """One canonical entity per row, retaining separately evidenced memberships."""
    directory, views, unassigned, _ = bundle_views(master, batch_id)
    reference = master["batches"][batch_id]
    countries, cities, markets = directory_maps(directory)
    timeline = sorted([(bid, b) for bid, b in master["batches"].items() if b.get("committed_at") and _commit_time(b) <= _commit_time(reference)], key=lambda pair: (_commit_time(pair[1]), pair[0]))
    identities = _historical_entities(timeline)
    alias_evidence, alias_locations = {}, {}
    for bid, batch in timeline:
        for lead in batch.get("lead_snapshots", []):
            if not lead.get("canonical_id"):
                continue
            canonical = core.canonical_entity(lead["lead_id"], identities)
            alias_evidence.setdefault(canonical, set()).update(lead.get("relationship_evidence", {}).get("source_ids", []) + lead.get("exclusion", {}).get("source_ids", []))
            assignment = lead.get("market_assignment") or {}
            mid = assignment.get("market_id")
            if batch.get("scope_id") == reference["scope_id"] and mid in markets and assignment.get("relation") == "within" and any(master["sources"].get(s, {}).get("level") in core.STRONG for s in assignment.get("source_ids", [])):
                city = cities[markets[mid]["city_id"]]
                relation = {"country": countries[city["country_id"]]["name"], "city": city["name"], "market": markets[mid]["name"], "market_id": mid, "relation": "within", "source_ids": copy.deepcopy(assignment["source_ids"]), "detail": assignment.get("detail", "已归并别名的场内位置证据"), "status": "duplicate", "qualification_batch_id": bid}
                alias_locations.setdefault(canonical, {})[mid] = relation
    observations = {}
    for view in list(views.values()) + [unassigned]:
        context = view.get("market_context", {})
        for lead in view["lead_snapshots"]:
            observations.setdefault(lead["lead_id"], []).append((lead, context))
    merged = []
    for lid, records in observations.items():
        def rank(record):
            lead = record[0]
            source_batch = master["batches"].get(lead.get("qualification_batch_id"), reference)
            return (lead.get("status") == "main", _commit_time(source_batch), lead.get("recorded_lead_id") == lid)
        representative = copy.deepcopy(max(records, key=rank)[0])
        latest = max(records, key=lambda record: _commit_time(master["batches"].get(record[0].get("qualification_batch_id"), reference)))[0]
        if latest.get("status") == "excluded" and latest.get("exclusion", {}).get("code") in {"brand_owner", "closed"}:
            representative = copy.deepcopy(latest)
        relations, evidence = [], set()
        segments = set()
        for lead, context in sorted(records, key=rank):
            evidence.update(core.lead_source_ids(lead))
            evidence.update(lead.get("export_source_ids", []))
            if lead.get("status") == "main" or representative.get("status") != "main":
                segments.add(core.collection_segment(lead, reference["task"]))
            assignment = lead.get("market_assignment") or {}
            if assignment.get("relation") == "within" and context.get("market_id") == assignment.get("market_id") and any(master["sources"].get(s, {}).get("level") in core.STRONG for s in assignment.get("source_ids", [])):
                relation = {key: context[key] for key in ("country", "city", "market", "market_id") if key in context}
                relation.update(relation="within", source_ids=copy.deepcopy(assignment["source_ids"]), detail=assignment.get("detail", "有据场内位置"), status=lead["status"], collection_segment=core.collection_segment(lead, reference["task"]), qualification_batch_id=lead.get("qualification_batch_id"))
                relations.append(relation)
        evidence.update(alias_evidence.get(lid, set()))
        for mid, relation in alias_locations.get(lid, {}).items():
            existing = next((r for r in relations if r.get("market_id") == mid), None)
            if existing:
                existing["source_ids"] = core.append_unique(existing["source_ids"], relation["source_ids"])
            else:
                relations.append(relation)
        representative.update(market_relations=relations, collection_segments=sorted(segments), export_source_ids=sorted(evidence))
        merged.append(representative)
    result = copy.deepcopy(reference)
    result["task"]["output_layout"] = "single_sheet"
    result.update(view="market_delivery", lead_snapshots=merged, counts=core.result_counts(merged, result["task"]), source_ids=[], queue=[], coverage=[], duplicate_hints=[])
    result["limitations"] = core.append_unique(reference.get("limitations", []), ["同一主体合为一行，保留截止批次有据的全部市场关系；正式／待核取原记录，不通过拼接缺失事实晋级。", "品牌店仍与独立商户分类，未知市场保留待定位；各市场研究顺序与断点不因输出合表改变。"])
    sequence = reference.get("research_summary", {}).get("market_sequence", {})
    active_id = sequence.get("batch_market_id") or sequence.get("active_market_id")
    if active_id in views:
        result["market_context"] = copy.deepcopy(views[active_id]["market_context"])
    return result


def export_single(root, batch_id, output):
    master, _ = core.load_master(root)
    return core.export_single_sheet(master, single_view(master, batch_id), batch_id, output)


def export_bundle(root, batch_id, output_dir):
    master, _ = core.load_master(root)
    directory, views, unassigned, grouped = bundle_views(master, batch_id)
    destination = Path(output_dir).expanduser().absolute()
    core.require(not destination.exists() and not destination.is_symlink(), "Choose a new delivery directory; previous deliveries are immutable")
    destination = destination.resolve()
    destination.mkdir(parents=True)
    overview = destination / "00-地区与市场进度.xlsx"
    core.write_xlsx(overview, directory_tables(directory, grouped))
    files = [str(overview)]
    for mid, view in views.items():
        view["task"]["output_layout"] = "legacy"
        c = view["market_context"]
        folder = destination / _component(c["country_id"], c["country"]) / _component(c["city_id"], c["city"])
        folder.mkdir(parents=True, exist_ok=True)
        output = folder / (_component(mid, c["market"]) + ".xlsx")
        core.export_view(master, view, batch_id, output)
        files.append(str(output))
    if unassigned["lead_snapshots"]:
        unassigned["task"]["output_layout"] = "legacy"
        output = destination / "待市场定位.xlsx"
        core.export_view(master, unassigned, batch_id, output)
        files.append(str(output))
    return {"output_dir": str(destination), "files": files, "market_workbooks": len(views),
            "unassigned_records": len(unassigned["lead_snapshots"]), "master_modified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    export.add_argument("--workspace", required=True)
    export.add_argument("--batch-id", required=True)
    destination = export.add_mutually_exclusive_group(required=True)
    destination.add_argument("--output", help="One Excel file with one customer sheet")
    destination.add_argument("--output-dir", help="Explicit compatibility export: separate legacy market workbooks")
    overview = sub.add_parser("overview")
    overview.add_argument("--plan", required=True, help="JSON directory or object containing market_directory; no customer master required")
    overview.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        if args.command == "export":
            result = export_single(core.workspace_path(args.workspace), args.batch_id, args.output) if args.output else export_bundle(core.workspace_path(args.workspace), args.batch_id, args.output_dir)
        else:
            data = core.read_json(args.plan)
            target = Path(args.output).expanduser().absolute()
            core.require(not target.exists(), "Choose a new overview filename")
            core.write_xlsx(target, directory_tables(data.get("market_directory", data)))
            result = {"output": str(target), "customer_database_created": False}
        print(json.dumps(result, ensure_ascii=False))
    except (core.LeadkitError, OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
