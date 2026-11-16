#!/usr/bin/env python3
"""Local research progress, observed tool readiness and bounded task policies.

This module never searches the web, probes an account, or sends a message. Codex
records actual observations; structural checks do not certify those observations.
"""
import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import leadkit as core

CHANNEL_KINDS = {"search", "website", "map", "market", "ecommerce", "social", "trade", "association", "rfq", "provided", "related"}
READINESS = {"readable", "needs_auth", "blocked", "untested"}
ATTEMPT_STATES = {"read", "empty", "blocked", "needs_auth", "failed"}
STOP_REASONS = {"target_reached", "coverage_complete", "market_complete", "scope_review_required", "attempt_budget", "time_budget", "paths_exhausted", "access_blocked", "needs_business_reply", "user_stopped", "batch_complete"}
MAP_CATEGORIES = {"offline_markets", "wholesale_import_distributors", "retail_malls_supermarkets", "online_sellers", "social", "trade_associations"}
ENTRY_PURPOSES = {"channel_research", "route_reconnaissance", "merchant_collection"}
WORK_KINDS = {"discovery", "qualification", "contact"}
SIZE_ORDER = {"large": 0, "medium": 1, "small": 2}
SYSTEMATIC_STRATEGIES = {"region_size_ladder", "market_sequence"}


def validate_policy(task):
    mode = task.get("mode", "new")
    core.require(mode in {"new", "continue", "enrich"}, "task.mode must be new/continue/enrich")
    target = task.get("target_count", 20)
    core.require(type(target) is int and target > 0, "target_count must be a positive integer")
    metric = task.get("target_metric", "updated" if mode == "enrich" else "new_qualified")
    core.require(metric in {"new_qualified", "qualified", "updated"}, "Invalid target_metric")
    budget = task.get("work_budget", {})
    core.require(isinstance(budget, dict), "work_budget must be an object")
    core.require(set(budget) <= {"max_attempts", "max_minutes"}, "Unknown work_budget field")
    attempts, minutes = budget.get("max_attempts", 80), budget.get("max_minutes", 45)
    core.require(type(attempts) is int and attempts > 0, "max_attempts must be a positive integer")
    core.require(type(minutes) in {int, float} and 0 < minutes <= 1440, "max_minutes must be 0..1440")
    strategy = task.get("collection_strategy", "legacy")
    core.require(strategy in {"legacy", "region_size_ladder", "market_sequence", "provided_leads"}, "Invalid collection_strategy")
    completion = task.get("completion_policy", "target_or_coverage")
    core.require(completion in {"coverage", "target_or_coverage"}, "Invalid completion_policy")
    return {"mode": mode, "target_count": target, "target_metric": metric, "work_budget": {"max_attempts": attempts, "max_minutes": minutes}, "collection_strategy": strategy, "completion_policy": completion}


def _time(value):
    core.iso_time(value, "research timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _attempt_count(log):
    # Deviations are actual calls, never normal pre-registered attempts.
    return len(log["attempts"]) + len(log.get("deviations", {}))


def _deviation_evidence(root, evidence):
    core.require(isinstance(evidence, dict), "Deviation needs an evidence file and sha256")
    core.nonempty(evidence.get("path"), "deviation.evidence.path")
    relative = Path(evidence["path"])
    core.require(not relative.is_absolute() and ".." not in relative.parts, "Deviation evidence must be inside the workspace")
    path = root / relative
    child = root
    for part in relative.parts:
        child = child / part
        core.require(not child.is_symlink(), "Deviation evidence may not use symlinks")
    core.require(path.is_file() and root.resolve() in path.resolve().parents, "Deviation evidence file is missing or outside workspace")
    core.require(evidence.get("sha256") == core.digest_bytes(path.read_bytes()), "Deviation evidence hash mismatch")


def record_deviation(root, batch_id, deviation):
    """Account for one already-observed unplanned call without laundering it."""
    core.require(isinstance(deviation, dict), "Deviation must be an object")
    core.require(set(deviation) == {"deviation_id", "kind", "started_at", "ended_at", "detail", "evidence"}, "Deviation fields must describe one actual call and its raw evidence only")
    core.valid_id(deviation.get("deviation_id"), "deviation_id")
    core.require(deviation.get("kind") == "unplanned_network", "Only actual unplanned_network deviations are supported")
    core.nonempty(deviation.get("detail"), "deviation.detail")
    started, ended = _time(deviation.get("started_at")), _time(deviation.get("ended_at"))
    core.require(ended >= started and ended <= datetime.now(timezone.utc), "Deviation time is reversed or in the future")
    _deviation_evidence(root, deviation.get("evidence"))
    with core.locked(root):
        folder, baseline, master, sha, packet = _context(root, batch_id)
        log = _log(folder, baseline, packet)
        core.require(log is not None, "Deviation needs an existing round; do not invent its start time")
        previous = log.get("deviations", {}).get(deviation["deviation_id"])
        if previous:
            core.require(previous == deviation, "deviation_id is immutable")
            return {"deviation_id": deviation["deviation_id"], "already_recorded": True, "network_performed": False}
        _mutable(master, batch_id, log)
        core.require(started >= _time(log["started_at"]), "Deviation predates this round")
        # Different actual calls can return byte-identical errors. The receipt
        # and its actual time interval identify an accidental replay; a payload
        # hash alone cannot identify a network call.
        core.require(not any(Path(d["evidence"]["path"]) == Path(deviation["evidence"]["path"])
                             and _time(d["started_at"]) == started and _time(d["ended_at"]) == ended
                             for d in log.get("deviations", {}).values()), "Same raw call receipt and time interval already recorded")
        log.setdefault("deviations", {})[deviation["deviation_id"]] = copy.deepcopy(deviation)
        core.safe_write(folder / "research-log.json", log)
        return {"deviation_id": deviation["deviation_id"], "already_recorded": False, "attempts": _attempt_count(log), "network_performed": False, "protocol_conformant": False}


def _url(value):
    core.nonempty(value, "observed URL")
    u = urlsplit(value)
    core.require(u.scheme in {"https", "http"} and u.hostname and not u.username and not u.password, "Use an observed public HTTP(S) URL without credentials")


def _registry(root):
    path = root / "execution" / "channel-registry.json"
    if path.exists():
        core.require(not path.is_symlink(), "Channel registry may not be a symlink")
        return core.read_json(path)
    return {"schema_version": 1, "channels": {}, "environment": {}, "history": []}


def register_channel(root, channel):
    core.require(isinstance(channel, dict), "Channel must be an object")
    cid = channel.get("channel_id")
    core.valid_id(cid, "channel_id")
    core.nonempty(channel.get("name"), "channel.name")
    core.require(channel.get("kind") in CHANNEL_KINDS, "Invalid channel.kind")
    readiness = channel.get("readiness", {})
    core.require(isinstance(readiness, dict) and readiness.get("status") in READINESS, "Invalid channel.readiness")
    core.iso_time(readiness.get("checked_at"), "readiness.checked_at")
    core.nonempty(readiness.get("detail"), "readiness.detail must describe an actual observation or the reason untested")
    if readiness.get("observed_url"):
        _url(readiness["observed_url"])
    core.string_list(channel.get("alternatives", []), "channel.alternatives")
    with core.locked(root):
        core.load_master(root)
        registry = _registry(root)
        old = registry["channels"].get(cid)
        if old:
            core.require(old["kind"] == channel["kind"], "A stable channel_id cannot change kind")
            if old == channel:
                return {"channel_id": cid, "already_recorded": True}
            registry["history"].append({"channel_id": cid, "previous": old, "changed_at": core.now()})
        registry["channels"][cid] = copy.deepcopy(channel)
        core.safe_write(root / "execution" / "channel-registry.json", registry)
    return {"channel_id": cid, "already_recorded": False, "observed_readiness": readiness["status"], "network_probe_performed_by_script": False}


def record_environment(root, observations):
    core.require(isinstance(observations, dict) and set(observations) == {"search", "web_read", "local_python"}, "Environment needs search, web_read and local_python observations")
    for name, observation in observations.items():
        core.require(isinstance(observation, dict) and observation.get("status") in {"available", "unavailable", "untested"}, "Invalid environment status: " + name)
        core.iso_time(observation.get("checked_at"), name + ".checked_at")
        core.nonempty(observation.get("detail"), name + ".detail")
    with core.locked(root):
        core.load_master(root)
        registry = _registry(root)
        if registry["environment"]:
            registry["history"].append({"environment": registry["environment"], "changed_at": core.now()})
        registry["environment"] = copy.deepcopy(observations)
        core.safe_write(root / "execution" / "channel-registry.json", registry)
    return {"recorded": True, "network_probe_performed_by_script": False}


def _context(root, batch_id, packet=None):
    core.valid_id(batch_id, "batch_id", core.BATCH_RE)
    folder = root / "research" / batch_id
    core.require(folder.is_dir() and not folder.is_symlink(), "Create snapshot before recording research")
    baseline = core.read_json(folder / "snapshot.json")
    master, sha = core.load_master(root)
    if packet is None:
        if batch_id in master["batches"]:
            batch = master["batches"][batch_id]
            packet = {"batch_id": batch_id, "base_sha256": baseline["base_sha256"], "task": batch["task"], "leads": batch["lead_snapshots"], "sources": []}
        else:
            packet = core.read_json(folder / "batch.json")
    core.require(packet.get("batch_id") == batch_id and packet.get("base_sha256") == baseline["base_sha256"], "Research packet does not match original snapshot")
    return folder, baseline, master, sha, packet


def _log(folder, baseline, packet, create=False):
    path = folder / "research-log.json"
    if path.exists():
        core.require(not path.is_symlink(), "Research log may not be a symlink")
        log = core.read_json(path)
        core.require(log["batch_id"] == packet["batch_id"] and log["base_sha256"] == baseline["base_sha256"], "Research log baseline mismatch")
        core.require(log["policy"] == validate_policy(packet["task"]), "Research policy changed after work started; create a new batch for new limits")
        if log.get("scope_id"):
            core.require(log["scope_id"] == core.scope_key(packet["task"]), "Research scope changed after work started; create a new batch")
        return log
    if not create:
        return None
    return {"schema_version": 1, "batch_id": packet["batch_id"], "base_sha256": baseline["base_sha256"], "scope_id": core.scope_key(packet["task"]), "started_at": core.now(), "policy": validate_policy(packet["task"]), "entries": {}, "attempts": {}, "read_history": [], "finish": None}


def _mutable(master, batch_id, log):
    core.require(batch_id not in master["batches"], "Committed research is frozen; continue in a new batch")
    core.require(not log.get("finish"), "Research round is finished; continue in a new batch")


def _entry_identity(entry):
    if entry.get("kind") == "link":
        # Tool-visible link IDs are scoped to the observed parent page, not the
        # guessed destination or a newly named source/channel/purpose.
        return core.digest_bytes(core.encoded({"kind": "link", "parent_url": entry["parent_url"], "link_id": str(entry["link_id"]).strip()}))
    return core.digest_bytes(core.encoded({key: entry.get(key) for key in ("channel_id", "kind", "url", "query", "input_reference", "cursor", "scope", "purpose")}))


def _link_context(entry, sources):
    if entry.get("kind") != "link":
        return
    _url(entry.get("parent_url"))
    core.valid_id(entry.get("parent_source_id"), "entry.parent_source_id")
    core.refs([entry["parent_source_id"]], "link.parent_source_id", sources, strong=True)
    core.require(sources[entry["parent_source_id"]]["url"] == entry["parent_url"], "Link parent_url must match its recorded readable parent source")
    lid = entry.get("link_id")
    core.require((type(lid) is int and lid >= 0) or (isinstance(lid, str) and bool(lid.strip())), "link_id must be an observed nonempty string or nonnegative integer")


def _failure_metadata(batch_id, attempt):
    """Keep observed recovery conditions without relabeling old work as new."""
    result = {key: copy.deepcopy(attempt[key]) for key in ("attempt_id", "status", "detail", "next_trigger", "started_at", "ended_at")}
    result.update(batch_id=batch_id, source_ids=copy.deepcopy(attempt.get("source_ids", [])))
    return result


def _last_failure(log, entry):
    """Also recover metadata from logs written before last_failure was added."""
    attempts = [a for a in log["attempts"].values() if a["entry_id"] == entry["entry_id"] and a["status"] in {"blocked", "needs_auth", "failed"}]
    if attempts:
        last = next((a for a in attempts if a["attempt_id"] == entry.get("last_attempt_id")), None)
        if last is None:
            last = max(attempts, key=lambda a: (_time(a["ended_at"]), _time(a["started_at"]), a["attempt_id"]))
        return _failure_metadata(log["batch_id"], last)
    return copy.deepcopy(entry.get("last_failure"))


def _retry_gate(log, entry, condition, enforce_planned=False):
    previous = [a for a in log["attempts"].values() if a["entry_id"] == entry["entry_id"]]
    prior_conditions = set(entry.get("prior_attempt_conditions", [])) | {a.get("retry_condition", "") for a in previous}
    if prior_conditions or previous:
        core.nonempty(condition, "Repeated entry needs an actual changed retry_condition")
        core.require(condition not in prior_conditions, "Retry condition has not changed")
    if enforce_planned and entry.get("retry_condition"):
        core.require(condition == entry["retry_condition"], "Attempt must describe the planned changed retry condition")


def _sources(packet, master):
    return dict(master["sources"], **{s["source_id"]: s for s in packet.get("sources", [])})


def _directory_maps(directory):
    return tuple({x[key]: x for x in directory.get(group, [])} for group, key in
                 (("countries", "country_id"), ("cities", "city_id"), ("markets", "market_id")))


def _ordered_markets(directory):
    countries, cities, markets = _directory_maps(directory)
    return sorted(markets.values(), key=lambda market: (
        countries[cities[market["city_id"]]["country_id"]]["order"],
        cities[market["city_id"]]["order"], market["order"], market["market_id"]))


def _market_sequence(log):
    plan = log.get("collection_plan") or {}
    directory = plan.get("market_directory", {"countries": [], "cities": [], "markets": []})
    countries, cities, markets = _directory_maps(directory)
    active_id = plan.get("batch_market_id")
    market = markets.get(active_id)
    active = None
    if market:
        city = cities[market["city_id"]]
        country = countries[city["country_id"]]
        active = dict(copy.deepcopy(market), country=country["name"], country_id=country["country_id"],
                      city=city["name"], market=market["name"])
    unfinished = [m["market_id"] for m in _ordered_markets(directory) if m.get("state") != "complete"]
    complete = bool(market and market.get("state") == "complete")
    queue_complete = bool(markets) and not unfinished
    directory_complete = queue_complete and directory.get("coverage", {}).get("status") == "complete" and all(
        any(m["city_id"] == cid for m in markets.values()) for cid in cities) and all(
        any(c["country_id"] == country_id for c in cities.values()) for country_id in countries)
    return {"directory": copy.deepcopy(directory), "active_market_id": active_id,
            "active_market": active, "active_market_complete": complete,
            "needs_scope_review": bool(market and market.get("state") == "review_required"),
            "next_market_id": unfinished[0] if complete and unfinished else None,
            "remaining_market_ids": unfinished,
            "recorded_queue_complete": queue_complete, "directory_complete": directory_complete,
            "needs_directory_expansion": queue_complete and not directory_complete,
            "preflight_city_id": plan.get("preflight_city_id"),
            "batch_market_fixed": True}


def _validate_market_directory(directory, old, task, sources):
    core.require(isinstance(directory, dict), "market_sequence requires market_directory")
    result = {}
    old_maps = _directory_maps(old or {})
    identities = (("countries", "country_id", ("name",)),
                  ("cities", "city_id", ("country_id", "name")),
                  ("markets", "market_id", ("city_id", "name")))
    for (group, id_key, stable), old_nodes in zip(identities, old_maps):
        items = directory.get(group)
        core.require(isinstance(items, list), "market_directory." + group + " must be an array")
        nodes, orders = {}, set()
        for item in items:
            core.require(isinstance(item, dict), "Market directory node must be an object")
            ident = item.get(id_key)
            core.valid_id(ident, id_key)
            core.require(ident not in nodes, "Duplicate market directory ID: " + str(ident))
            core.nonempty(item.get("name"), id_key + ".name")
            core.require(type(item.get("order")) is int and item["order"] > 0, id_key + ".order must be positive")
            parent = item.get("country_id") if group == "cities" else item.get("city_id") if group == "markets" else None
            core.require((parent, item["order"]) not in orders, "Directory sibling order must be unique")
            orders.add((parent, item["order"]))
            core.refs(item.get("source_ids", []), id_key + ".source_ids", sources, strong=True, required=group != "countries")
            if group == "countries":
                allowed = task.get("countries", [])
                core.require(item["name"] in allowed or "全球" in allowed, "Directory country is outside this task")
            elif group == "cities":
                core.require(parent in {c["country_id"] for c in result["countries"]}, "City needs an observed parent country")
                parent_name = next(c["name"] for c in result["countries"] if c["country_id"] == parent)
                relevant_scopes = [scope for scope in core.task_scopes(task) if scope["country"] in {parent_name, "全球"}]
                core.require(any(not scope.get("city") or scope["city"] == item["name"] for scope in relevant_scopes), "Directory city is outside this task")
            else:
                core.require(parent in {c["city_id"] for c in result["cities"]}, "Market needs an observed parent city")
                core.nonempty(item.get("priority_basis"), "market.priority_basis must explain evidence-backed order")
                core.nonempty(item.get("boundary"), "market.boundary must describe the observed market/directory scope")
                core.refs(item.get("size_source_ids", []), "market.size_source_ids", sources, strong=True, required=False)
            node = {k: copy.deepcopy(v) for k, v in item.items() if k not in {"state", "progress", "progress_history"}}
            if ident in old_nodes:
                prior = old_nodes[ident]
                core.require(all(prior.get(k) == node.get(k) for k in stable + ("order",)), "Saved directory identity and sequence are fixed; do not relabel or reorder markets")
                if group == "markets" and prior.get("state") == "complete":
                    core.require(prior.get("boundary") == node.get("boundary"), "A completed market's observed boundary is frozen; use a new explicit review task for expanded scope")
                node.update({k: copy.deepcopy(prior[k]) for k in ("state", "progress", "progress_history") if k in prior})
            elif group == "markets":
                node["state"] = "open"
            nodes[ident] = node
        core.require(set(old_nodes) <= set(nodes), "Do not delete observed countries, cities or markets")
        result[group] = list(nodes.values())
    coverage = copy.deepcopy(directory.get("coverage", {"status": "partial", "detail": "Only observed countries, cities and markets are listed; completeness is not established", "source_ids": []}))
    core.require(isinstance(coverage, dict) and coverage.get("status") in {"partial", "complete"}, "directory.coverage.status must be partial/complete")
    core.nonempty(coverage.get("detail"), "directory.coverage.detail")
    core.refs(coverage.get("source_ids", []), "directory.coverage.source_ids", sources, strong=True, required=coverage["status"] == "complete")
    if coverage["status"] == "complete":
        core.require("全球" not in task.get("countries", []) and {c["name"] for c in result["countries"]} == set(task.get("countries", [])), "Directory completeness requires the task's explicit countries; a global directory is partial")
        core.require(bool(result["markets"]) and all(any(c["country_id"] == n["country_id"] for c in result["cities"]) for n in result["countries"]) and all(any(m["city_id"] == c["city_id"] for m in result["markets"]) for c in result["cities"]), "A complete hierarchy cannot leave a listed country or city unexplored")
    result["coverage"] = coverage
    return result


def _plan_status(log):
    plan = log.get("collection_plan")
    if not plan:
        result = {"scope_completion": "partial", "collection_complete": False, "preflight_ready": False, "preflight_pending_categories": sorted(MAP_CATEGORIES), "unfinished_routes": [], "unresolved_categories": sorted(MAP_CATEGORIES), "next_route_id": None, "next_action": "先研究当地渠道地图和有证据的实际联系生态"}
        if log.get("policy", {}).get("collection_strategy") == "market_sequence":
            result["market_sequence"] = _market_sequence(log)
        return result
    pending = [x["category"] for x in plan["channels"] if x["status"] == "needs_research" or (x["status"] == "blocked" and not x.get("blocked_attempts"))]
    preflight_ready = bool(plan["contact_ecosystem"]) and not pending
    unresolved = [x["category"] for x in plan["channels"] if x["status"] in {"needs_research", "blocked"}]
    covered = {r["category"] for r in plan["routes"].values()}
    missing = [x["category"] for x in plan["channels"] if x["status"] == "applicable" and x["category"] not in covered]
    unfinished = [r["route_id"] for r in plan["routes"].values() if r["state"] not in {"complete", "empty"}]
    complete = preflight_ready and not unresolved and not missing and not unfinished and bool(plan["routes"])
    available = [r for r in plan["routes"].values() if r["state"] in {"open", "active"}]
    available.sort(key=lambda r: (SIZE_ORDER.get(r["size"], 3), r["order"]))
    result = {"scope_completion": "complete" if complete else "partial", "collection_complete": complete, "preflight_ready": preflight_ready, "preflight_pending_categories": sorted(pending), "unfinished_routes": unfinished, "unresolved_categories": sorted(set(unresolved + missing)), "next_route_id": available[0]["route_id"] if available and preflight_ready else None, "next_action": "先完成六类当地渠道初查和有证据的联系生态；受阻分类需真实失败记录及恢复条件" if not preflight_ready else "已处理本次实际发现的路线边界，可交付覆盖结果" if complete else "继续渠道地图中未完成的路线；未知规模先核实，不能冒充小型路线"}
    if log.get("policy", {}).get("collection_strategy") == "market_sequence":
        sequence = _market_sequence(log)
        active = sequence["active_market"]
        city_ready = bool(active and plan.get("preflight_city_id") == active["city_id"])
        ready = preflight_ready and city_ready
        current_routes = [r for r in available if r.get("market_id") == sequence["active_market_id"]]
        done = sequence["directory_complete"]
        result.update(market_sequence=sequence, preflight_ready=ready, city_preflight_ready=city_ready,
                      scope_completion="complete" if done else "partial", collection_complete=done,
                      next_route_id=current_routes[0]["route_id"] if current_routes and ready and not sequence["active_market_complete"] else None,
                      next_action="当前市场已完成；将该市场结果合入客户总表，下一批才进入下一市场" if sequence["active_market_complete"] else
                                  "当前市场需范围复核且仍未完成；可继续本市场调查，或交接给用户决定是否另建调整范围任务，不自动进入下一市场" if sequence["needs_scope_review"] else
                                  "先完成当前城市的渠道与联系预研" if not ready else "继续当前市场的未读商户；预算结束仍接续本市场")
    return result


def collection_plan(root, batch_id, plan):
    """Record the observed local channel map; this does not perform reconnaissance."""
    core.require(isinstance(plan, dict), "collection plan must be an object")
    core.iso_time(plan.get("checked_at"), "collection_plan.checked_at")
    channels, contacts, routes = plan.get("channels"), plan.get("contact_ecosystem"), plan.get("routes")
    core.require(isinstance(channels, list) and {x.get("category") for x in channels} == MAP_CATEGORIES and len(channels) == len(MAP_CATEGORIES), "Map all six channel categories; use needs_research for unresearched categories")
    core.require(isinstance(contacts, list), "contact_ecosystem must be an array; empty preflight remains partial")
    core.require(isinstance(routes, list), "routes must be an array")
    with core.locked(root):
        folder, baseline, master, sha, packet = _context(root, batch_id)
        log = _log(folder, baseline, packet, True)
        _mutable(master, batch_id, log)
        sources = _sources(packet, master)
        old = log.get("collection_plan")
        market_mode = log["policy"]["collection_strategy"] == "market_sequence"
        directory, batch_market_id = None, None
        if market_mode:
            directory = _validate_market_directory(plan.get("market_directory"), (old or {}).get("market_directory"), packet["task"], sources)
            _, cities, markets = _directory_maps(directory)
            preflight_city = plan.get("preflight_city_id")
            core.require(preflight_city is None or preflight_city in cities, "preflight_city_id must refer to an observed directory city")
            batch_market_id = (old or {}).get("batch_market_id")
            if not batch_market_id:
                batch_market_id = next((m["market_id"] for m in _ordered_markets(directory) if m.get("state") != "complete"), None)
            core.require(plan.get("batch_market_id", batch_market_id) == batch_market_id, "A batch is fixed to one market; advance only with workflow continue after market completion")
            if batch_market_id and markets[batch_market_id].get("state") != "complete":
                first = next(m["market_id"] for m in _ordered_markets(directory) if m.get("state") != "complete")
                core.require(first == batch_market_id, "New directory items cannot silently jump ahead of the active market")
        reuse_city = not market_mode or (old or {}).get("preflight_city_id") == plan.get("preflight_city_id")
        old_channels = {c["category"]: c for c in old["channels"]} if old and reuse_city else {}
        recorded_channels = []
        for item in channels:
            recorded = copy.deepcopy(item)
            recorded.pop("blocked_attempts", None)
            state = item.get("status")
            core.require(state in {"applicable", "needs_research", "blocked", "not_applicable"}, "Invalid map category status")
            core.nonempty(item.get("detail"), "Map category needs actual findings or unknown/blocked explanation")
            core.refs(item.get("source_ids", []), "collection_plan.channel.source_ids", sources, strong=state in {"applicable", "not_applicable"}, required=state in {"applicable", "not_applicable"})
            if state == "blocked":
                core.iso_time(item.get("checked_at"), "blocked category.checked_at")
                core.nonempty(item.get("next_trigger"), "Blocked category needs next_trigger")
                core.string_list(item.get("attempt_ids", []), "blocked category.attempt_ids")
                core.require(bool(item.get("attempt_ids")), "Blocked category needs actual failed preflight attempt_ids")
                inherited = {a["attempt_id"]: a for a in old_channels.get(item["category"], {}).get("blocked_attempts", [])}
                failures = []
                for aid in item["attempt_ids"]:
                    attempt = log["attempts"].get(aid)
                    if attempt:
                        entry = log["entries"][attempt["entry_id"]]
                        core.require(attempt["status"] in {"blocked", "needs_auth", "failed"} and entry.get("purpose") in {"channel_research", "route_reconnaissance"} and entry.get("category") == item["category"], "Blocked category must reference its own actual failed preflight attempt")
                        failures.append(dict(_failure_metadata(batch_id, attempt), entry_id=attempt["entry_id"]))
                    else:
                        core.require(aid in inherited, "Blocked category references missing actual preflight attempt")
                        failures.append(copy.deepcopy(inherited[aid]))
                recorded["blocked_attempts"] = failures
            recorded_channels.append(recorded)
        for item in contacts:
            core.nonempty(item.get("kind"), "contact_ecosystem.kind")
            core.require(item["kind"].strip().casefold() not in {"unknown", "untested", "needs_research", "未核", "未知", "待核", "未研究"}, "Unknown contact ecology does not complete preflight")
            core.nonempty(item.get("detail"), "contact_ecosystem.detail")
            core.refs(item.get("source_ids", []), "contact_ecosystem.source_ids", sources, strong=True)
        registry = _registry(root)["channels"]
        old_routes = old["routes"] if old else {}
        mapped = {}
        for item in routes:
            rid = item.get("route_id")
            core.valid_id(rid, "route_id")
            core.require(rid not in mapped, "Duplicate route_id")
            core.require(item.get("channel_id") in registry, "Route needs a registered channel_id")
            core.require(item.get("category") in MAP_CATEGORIES, "Invalid route category")
            route_city = markets.get(item.get("market_id"), {}).get("city_id") if market_mode else None
            if not market_mode or route_city == plan.get("preflight_city_id"):
                core.require(next(c for c in channels if c["category"] == item["category"])["status"] == "applicable", "A planned route needs an applicable map category")
            core.require(item.get("size") in {"large", "medium", "small", "unknown"}, "Route size must be observed, never inferred from missing data")
            core.refs(item.get("source_ids", []), "route.source_ids", sources, strong=True)
            core.refs(item.get("size_source_ids", []), "route.size_source_ids", sources, strong=True, required=item["size"] != "unknown")
            core.require(type(item.get("order")) is int and item["order"] > 0, "route.order must be positive integer")
            core.nonempty(item.get("entrypoint"), "route.entrypoint")
            core.nonempty(item.get("detail"), "route.detail")
            core.nonempty(item.get("next_action"), "route.next_action")
            if market_mode:
                core.require(item.get("market_id") in markets, "Market route needs an observed market_id")
                core.require(rid in old_routes or markets[item["market_id"]].get("state") != "complete", "Cannot append an unread route to a completed market boundary; start an explicit review task")
            route = {k: copy.deepcopy(v) for k, v in item.items() if k not in {"state", "progress", "progress_history"}}
            route["state"] = "open"
            if rid in old_routes:
                prior = old_routes[rid]
                core.require(all(prior.get(k) == route.get(k) for k in ("channel_id", "category", "entrypoint", "market_id")), "Stable route_id cannot change its identity")
                if any(prior.get(k) != route.get(k) for k in ("size", "order")):
                    core.nonempty(plan.get("revision_detail"), "Changing observed route size/order needs revision_detail")
                    revised_sources = set(route.get("size_source_ids", []) + route.get("source_ids", []))
                    old_sources = set(prior.get("size_source_ids", []) + prior.get("source_ids", []))
                    core.require(bool(revised_sources - old_sources), "Changing observed route size/order needs new evidence")
                route.update({k: copy.deepcopy(prior[k]) for k in ("state", "progress", "progress_history") if k in prior})
            mapped[rid] = route
        core.require(set(old_routes) <= set(mapped), "Do not delete observed routes; record their actual progress or limitations")
        if old:
            log.setdefault("collection_plan_history", []).append(old)
        log["collection_plan"] = {"checked_at": plan["checked_at"], "scope": copy.deepcopy(core.task_scopes(packet["task"])), "channels": recorded_channels, "contact_ecosystem": copy.deepcopy(contacts), "routes": mapped}
        if market_mode:
            log["collection_plan"].update(market_directory=directory, batch_market_id=batch_market_id, preflight_city_id=plan.get("preflight_city_id"))
        if plan.get("revision_detail"):
            log["collection_plan"]["revision_detail"] = plan["revision_detail"]
        core.safe_write(folder / "research-log.json", log)
        return _plan_status(log)


def _route_gate(log, entry):
    if log["policy"]["collection_strategy"] not in SYSTEMATIC_STRATEGIES or entry.get("purpose") != "merchant_collection":
        return
    plan = log.get("collection_plan")
    core.require(plan and _plan_status(log)["preflight_ready"], "Complete evidenced local channel/contact preflight before merchant collection")
    route = plan["routes"].get(entry.get("route_id"))
    core.require(route, "Merchant entry needs an observed route_id")
    core.require(route["channel_id"] == entry["channel_id"], "Entry channel_id must match its route")
    market_mode = log["policy"]["collection_strategy"] == "market_sequence"
    if market_mode:
        sequence = _market_sequence(log)
        core.require(not sequence["active_market_complete"], "This batch's market is complete; save its delivery and continue in a new batch before the next market")
        core.require(route.get("market_id") == sequence["active_market_id"], "Merchant collection is restricted to the active market; finish its observed boundary before the next market")
        core.require(entry.get("market_id", route["market_id"]) == route["market_id"], "Entry market_id must match its route")
    core.require(route["state"] in {"open", "active", "blocked"}, "Route is not executable in its current state; deferred routes must be explicitly resumed")
    rank = SIZE_ORDER.get(route["size"], 3)
    earlier = [r["route_id"] for r in plan["routes"].values() if (not market_mode or r.get("market_id") == route.get("market_id")) and r["size"] in SIZE_ORDER and SIZE_ORDER[r["size"]] < rank and r["state"] in {"open", "active"}]
    core.require(not earlier, "Process executable larger routes first: " + ", ".join(earlier))


def route_progress(root, batch_id, route_id, update):
    core.require(isinstance(update, dict), "route progress must be an object")
    core.require(update.get("state") in {"active", "complete", "empty", "blocked", "skipped", "deferred"}, "Invalid route progress state")
    core.nonempty(update.get("detail"), "Route progress needs discovered boundary/limitation detail")
    core.nonempty(update.get("cursor"), "Record actual page/directory boundary or unavailable cursor")
    core.string_list(update.get("attempt_ids", []), "route.attempt_ids")
    with core.locked(root):
        folder, baseline, master, sha, packet = _context(root, batch_id)
        log = _log(folder, baseline, packet, True)
        _mutable(master, batch_id, log)
        route = (log.get("collection_plan") or {}).get("routes", {}).get(route_id)
        core.require(route, "Unknown route_id")
        market_mode = log["policy"]["collection_strategy"] == "market_sequence"
        if market_mode:
            sequence = _market_sequence(log)
            core.require(route.get("market_id") == sequence["active_market_id"] and not sequence["active_market_complete"], "Only the active unfinished market can change merchant route progress")
        attempts = [log["attempts"].get(aid) for aid in update.get("attempt_ids", [])]
        core.require(all(a is not None and log["entries"][a["entry_id"]].get("route_id") == route_id for a in attempts), "Route progress must reference its own actual attempts")
        if update["state"] in {"complete", "empty", "blocked", "skipped", "deferred"}:
            core.require(bool(attempts), "Closing/skipping a route needs actual attempt evidence")
        if update["state"] in {"complete", "empty"}:
            core.require(update.get("directory_exhausted") is True, "Completed route needs directory_exhausted=true for its actual observed boundary")
            core.require(any(a["status"] in {"read", "empty"} and log["entries"][a["entry_id"]].get("purpose") == "merchant_collection" for a in attempts), "Completed route needs actual merchant discovery attempts, not only channel background")
            core.require(not any(e.get("route_id") == route_id and e["state"] != "done" for e in log["entries"].values()), "Route still has unread or blocked entries")
            if update["state"] == "empty":
                core.require(not any(a.get("discovered_lead_ids") for a in log["attempts"].values() if log["entries"][a["entry_id"]].get("route_id") == route_id), "A route with discovered merchants is complete, not empty")
        if update["state"] in {"blocked", "skipped", "deferred"}:
            core.nonempty(update.get("next_trigger"), "Unfinished route needs next_trigger")
        if update["state"] == "deferred":
            core.require(_plan_status(log)["preflight_ready"], "Deferral needs completed preflight before selecting an executable alternative")
            core.nonempty(update.get("next_action"), "Deferred route needs a concrete next_action")
            core.nonempty(update.get("allocation_reason"), "Deferred route needs an allocation_reason comparing actual work and remaining opportunities")
            core.require(update.get("directory_exhausted") is not True, "Deferred route is unfinished, not exhausted")
            core.require(route["state"] not in {"complete", "empty"}, "A completed route cannot be relabeled deferred")
            core.require(any(log["entries"][a["entry_id"]].get("purpose") == "merchant_collection" for a in attempts), "Deferral needs actual merchant discovery or verification attempts")
            routes = log["collection_plan"]["routes"]
            alternative_id = update.get("alternative_route_id")
            core.valid_id(alternative_id, "alternative_route_id")
            alternative = routes.get(alternative_id)
            core.require(alternative_id != route_id and alternative and alternative["state"] in {"open", "active"}, "Deferred route needs a different observed executable alternative_route_id")
            if market_mode:
                core.require(alternative.get("market_id") == route.get("market_id"), "Deferral may switch routes inside the active market, never jump to another market")
            rank = SIZE_ORDER.get(alternative["size"], 3)
            earlier = [r["route_id"] for r in routes.values() if r["route_id"] != route_id and (not market_mode or r.get("market_id") == route.get("market_id")) and r["size"] in SIZE_ORDER and SIZE_ORDER[r["size"]] < rank and r["state"] in {"open", "active"}]
            core.require(not earlier, "Alternative route remains behind executable larger routes: " + ", ".join(earlier))
        if route.get("progress"):
            route.setdefault("progress_history", []).append(route["progress"])
        route.update(state=update["state"], progress=dict(copy.deepcopy(update), recorded_at=core.now()))
        core.safe_write(folder / "research-log.json", log)
        return _plan_status(log)


def market_progress(root, batch_id, market_id, update):
    """Record an evidenced boundary completion or a nonblocking scope review."""
    core.valid_id(market_id, "market_id")
    core.require(isinstance(update, dict) and update.get("state") in {"complete", "review_required"}, "market-progress supports evidenced state=complete/review_required only")
    review = update["state"] == "review_required"
    for key in ("detail", "boundary", "cursor"):
        core.nonempty(update.get(key), "market progress." + key)
    if review:
        core.require(update.get("directory_exhausted") is False, "Market scope review needs directory_exhausted=false; it does not complete the market")
    else:
        core.require(update.get("directory_exhausted") is True, "Market completion needs directory_exhausted=true for its observed boundary")
    core.string_list(update.get("attempt_ids", []), "market progress.attempt_ids")
    with core.locked(root):
        folder, baseline, master, sha, packet = _context(root, batch_id)
        log = _log(folder, baseline, packet, True)
        _mutable(master, batch_id, log)
        core.require(log["policy"]["collection_strategy"] == "market_sequence", "market-progress requires market_sequence")
        report = _plan_status(log)
        sequence = report.get("market_sequence", {})
        core.require(sequence.get("active_market_id") == market_id, "Only this batch's active market can change market progress")
        if review:
            core.require(not sequence["active_market_complete"], "A completed market cannot be relabeled review_required")
        else:
            core.require(report["preflight_ready"], "Market completion requires current-city preflight")
        sources = _sources(packet, master)
        core.refs(update.get("source_ids", []), "market completion.source_ids", sources, strong=True)
        plan = log["collection_plan"]
        routes = {rid: r for rid, r in plan["routes"].items() if r.get("market_id") == market_id}
        if not review:
            core.require(routes and all(r["state"] in {"complete", "empty"} for r in routes.values()), "Market still has uncompleted, blocked, skipped or deferred routes")
            core.require(not any(e.get("route_id") in routes and e["state"] != "done" for e in log["entries"].values()), "Market still has unread or blocked observed entries")
        attempts = [log["attempts"].get(aid) for aid in update.get("attempt_ids", [])]
        core.require(attempts and all(a and log["entries"][a["entry_id"]].get("route_id") in routes for a in attempts), "Market progress needs its own real boundary attempts")
        if review:
            core.require(all(log["entries"][a["entry_id"]].get("purpose") in {"route_reconnaissance", "merchant_collection"} for a in attempts), "Market scope review needs market reconnaissance or merchant attempts")
            read_sources = {sid for a in attempts if a["status"] == "read" for sid in a.get("source_ids", [])}
            core.require(set(update["source_ids"]) <= read_sources, "Market scope review sources must all be linked to actual readable attempts")
        else:
            core.require(any(a["status"] == "read" and set(a.get("source_ids", [])) & set(update["source_ids"]) for a in attempts), "Market completion sources must be linked to an actual readable boundary attempt")
        market = next(m for m in plan["market_directory"]["markets"] if m["market_id"] == market_id)
        if market.get("progress"):
            market.setdefault("progress_history", []).append(market["progress"])
        progress = dict(copy.deepcopy(update), recorded_at=core.now(),
                        completion_scope="partial" if review else "observed_directory_boundary")
        if review:
            progress["review_batch_id"] = batch_id
        market.update(state=update["state"], progress=progress)
        core.safe_write(folder / "research-log.json", log)
        return _plan_status(log)


def plan_entry(root, batch_id, entry, defer=False):
    """Check execution eligibility immediately before a call, or save only a queue item."""
    core.require(type(defer) is bool, "defer must be a boolean")
    core.require(isinstance(entry, dict), "Entry must be an object")
    core.valid_id(entry.get("entry_id"), "entry_id")
    core.valid_id(entry.get("channel_id"), "channel_id")
    core.require(entry.get("kind") in {"query", "page", "file", "link"}, "Entry kind must be query/page/file/link")
    if entry["kind"] == "page":
        _url(entry.get("url"))
    elif entry["kind"] == "query":
        core.nonempty(entry.get("query"), "entry.query")
    elif entry["kind"] == "file":
        core.validate_input_reference(entry.get("input_reference"))
    core.string_list(entry.get("products"), "entry.products")
    core.nonempty(entry.get("next_action"), "entry.next_action")
    if "work_kind" in entry:
        core.require(isinstance(entry["work_kind"], str) and entry["work_kind"] in WORK_KINDS, "entry.work_kind must be discovery/qualification/contact")
    subject_ids = entry.get("subject_ids", [])
    core.string_list(subject_ids, "entry.subject_ids")
    core.require(len(subject_ids) == len(set(subject_ids)), "entry.subject_ids must not repeat IDs")
    for lid in subject_ids:
        core.valid_id(lid, "entry.subject_ids")
    with core.locked(root):
        folder, baseline, master, sha, packet = _context(root, batch_id)
        known_ids = set(master["leads"]) | {l["lead_id"] for l in packet.get("leads", [])}
        core.require(set(subject_ids) <= known_ids, "entry.subject_ids references missing lead; save the candidate first")
        core.require(bool(entry["products"]) and set(entry["products"]) <= set(packet["task"]["products"]), "Entry products outside task")
        core.require(entry["channel_id"] in _registry(root)["channels"], "Register a stable channel_id first")
        _link_context(entry, _sources(packet, master))
        log = _log(folder, baseline, packet, True)
        _mutable(master, batch_id, log)
        if not defer:
            core.require(_attempt_count(log) < log["policy"]["work_budget"]["max_attempts"], "Attempt budget exhausted before execution; finish this round or save with --defer")
            elapsed = (_time(core.now()) - _time(log["started_at"])).total_seconds()
            core.require(elapsed < log["policy"]["work_budget"]["max_minutes"] * 60, "Time budget exhausted before execution; finish this round or save with --defer")
        stored = copy.deepcopy(entry)
        stored.pop("last_failure", None)  # Only actual attempts/carry-forward create this metadata.
        stored.pop("deferred", None)
        # This is runtime metadata, not an employee-supplied source timestamp.
        # A successful executable re-plan refreshes it, including activation of
        # an entry that was previously queued with --defer.
        stored["registered_at"] = core.now()
        if defer:
            stored["deferred"] = True
        if stored["kind"] == "link":
            stored["link_id"] = str(stored["link_id"]).strip()
        stored.setdefault("scope", copy.deepcopy(core.task_scopes(packet["task"])))
        stored.setdefault("purpose", "merchant_collection")
        core.require(stored["purpose"] in ENTRY_PURPOSES, "Invalid entry purpose")
        if "category" in stored:
            core.require(stored["category"] in MAP_CATEGORIES, "Invalid entry category")
        core.require(stored["scope"] == core.task_scopes(packet["task"]), "Entry scope must record this task's explicit geographic scope")
        stored["state"] = "pending"
        old = log["entries"].get(entry["entry_id"])
        if old:
            transient = {"state", "last_attempt_id", "prior_attempt_conditions", "last_failure", "deferred", "retry_condition", "registered_at"}
            core.require({k: v for k, v in old.items() if k not in transient} == {k: v for k, v in stored.items() if k not in transient}, "entry_id already describes different work")
            if not defer:
                _route_gate(log, stored)
                _retry_gate(log, old, stored.get("retry_condition", old.get("retry_condition", "")))
            changed = False
            if "retry_condition" in stored and old.get("retry_condition") != stored["retry_condition"]:
                old["retry_condition"] = stored["retry_condition"]
                changed = True
            if defer:
                old["deferred"] = True
                changed = True
            elif old.pop("deferred", None):
                changed = True
            if not defer:
                old["registered_at"] = stored["registered_at"]
                changed = True
            if changed:
                core.safe_write(folder / "research-log.json", log)
            return {"entry_id": entry["entry_id"], "already_planned": True, "state": old["state"], "executable": not defer, "deferred": defer, "registered_at": old.get("registered_at")}
        if not defer:
            _route_gate(log, stored)
        identity = _entry_identity(stored)
        core.require(not any(_entry_identity(x) == identity for x in log["entries"].values()), "Same entry already planned; reuse its stable entry_id")
        history = [x for x in log["read_history"] if x["entry_identity"] == identity]
        if history:
            condition = stored.get("retry_condition")
            core.nonempty(condition, "Previously processed entry needs a changed retry_condition")
            used = {c for x in history for c in x.get("attempt_conditions", [""])}
            core.require(condition not in used, "Retry condition has not changed")
            stored["prior_attempt_conditions"] = sorted(used)
        log["entries"][entry["entry_id"]] = stored
        core.safe_write(folder / "research-log.json", log)
    return {"entry_id": entry["entry_id"], "already_planned": False, "state": "pending", "executable": not defer, "deferred": defer, "registered_at": stored["registered_at"]}


def record_attempt(root, batch_id, attempt):
    core.require(isinstance(attempt, dict), "Attempt must be an object")
    core.valid_id(attempt.get("attempt_id"), "attempt_id")
    core.valid_id(attempt.get("entry_id"), "entry_id")
    core.require(attempt.get("status") in ATTEMPT_STATES, "Invalid attempt.status")
    started, ended = _time(attempt.get("started_at")), _time(attempt.get("ended_at"))
    core.require(ended >= started and ended <= datetime.now(timezone.utc), "Attempt time is reversed or in the future")
    core.nonempty(attempt.get("detail"), "attempt.detail")
    core.string_list(attempt.get("source_ids", []), "attempt.source_ids")
    core.string_list(attempt.get("discovered_lead_ids", []), "attempt.discovered_lead_ids")
    if attempt["status"] in {"blocked", "needs_auth", "failed"}:
        core.nonempty(attempt.get("next_trigger"), "Failed access needs an actual next_trigger, such as an available alternative or changed access")
    with core.locked(root):
        folder, baseline, master, sha, packet = _context(root, batch_id)
        log = _log(folder, baseline, packet, True)
        if attempt["attempt_id"] in log["attempts"]:
            core.require(log["attempts"][attempt["attempt_id"]] == attempt, "attempt_id is immutable")
            return {"attempt_id": attempt["attempt_id"], "already_recorded": True}
        _mutable(master, batch_id, log)
        core.require(attempt["entry_id"] in log["entries"], "Plan the entry before recording its attempt")
        core.require(started >= _time(log["started_at"]), "Attempt predates this round; do not relabel historical work")
        core.require(_attempt_count(log) < log["policy"]["work_budget"]["max_attempts"], "Attempt budget exhausted; finish this round")
        core.require((started - _time(log["started_at"])).total_seconds() < log["policy"]["work_budget"]["max_minutes"] * 60, "Attempt started after the time budget; finish this round")
        entry = log["entries"][attempt["entry_id"]]
        core.require(not entry.get("deferred"), "Deferred entry is not executable; run plan-entry without --defer immediately before the actual call")
        core.require(entry.get("registered_at"), "Legacy entry needs a fresh plan-entry before recording a new attempt")
        core.require(started >= _time(entry["registered_at"]), "Attempt predates execution registration; record the actual unplanned call as a deviation")
        _route_gate(log, entry)
        _retry_gate(log, entry, attempt.get("retry_condition", ""), enforce_planned=True)
        if attempt["status"] == "read" and entry["kind"] != "file":
            core.require(bool(attempt.get("source_ids")), "A read web entry needs an actual recorded source")
        sources = dict(master["sources"], **{s["source_id"]: s for s in packet.get("sources", [])})
        _link_context(entry, sources)
        core.require(set(attempt.get("source_ids", [])) <= set(sources), "Attempt references missing source; save evidence first")
        if attempt["status"] == "read":
            core.require(all(sources[s]["level"] != "failed_read" for s in attempt.get("source_ids", [])), "Failed reading cannot be recorded as read")
            if entry["kind"] == "link":
                core.require(any(sid != entry["parent_source_id"] for sid in attempt["source_ids"]), "A read link needs the actual click result source; its parent page alone is insufficient")
        known_ids = set(master["leads"]) | {l["lead_id"] for l in packet.get("leads", [])}
        core.require(set(entry.get("subject_ids", [])) <= known_ids, "Attempt subject_ids references missing lead")
        core.require(set(attempt.get("discovered_lead_ids", [])) <= known_ids, "Attempt references missing lead; save candidate first")
        if attempt["status"] == "empty":
            core.require(not attempt.get("discovered_lead_ids"), "An empty attempt cannot report discovered merchants")
        log["attempts"][attempt["attempt_id"]] = copy.deepcopy(attempt)
        entry.update(state="done" if attempt["status"] in {"read", "empty"} else "blocked", last_attempt_id=attempt["attempt_id"])
        if attempt["status"] in {"blocked", "needs_auth", "failed"}:
            entry["last_failure"] = _failure_metadata(batch_id, attempt)
        route = (log.get("collection_plan") or {}).get("routes", {}).get(entry.get("route_id"))
        if route and route["state"] == "open":
            route["state"] = "active"
        if route and log["policy"]["collection_strategy"] == "market_sequence" and entry.get("purpose") == "merchant_collection":
            for market in log["collection_plan"]["market_directory"]["markets"]:
                if market["market_id"] == route["market_id"] and market["state"] == "open":
                    market["state"] = "active"
        core.safe_write(folder / "research-log.json", log)
    return {"attempt_id": attempt["attempt_id"], "already_recorded": False, "entry_state": entry["state"]}


def _research_effort(log, proposed, old, seen, qualified):
    """Describe recorded allocation without judging truth or choosing routes.

    Attribution can overlap across routes and subjects. Missing optional labels
    stay unclassified; rejected/missing records are never counted as entities.
    """
    canonical = lambda lid: core.canonical_entity(lid, proposed)

    def group():
        return {"attempts": 0, "read": 0, "empty": 0, "failed_access": 0,
                "status_counts": {state: 0 for state in sorted(ATTEMPT_STATES)},
                "purpose_counts": {purpose: 0 for purpose in sorted(ENTRY_PURPOSES)},
                "discovered_ids": set()}

    def add(bucket, attempt, entry, found):
        bucket["attempts"] += 1
        state = attempt["status"]
        bucket["status_counts"][state] += 1
        if state in {"read", "empty"}:
            bucket[state] += 1
        else:
            bucket["failed_access"] += 1
        purpose = entry.get("purpose", "merchant_collection")
        bucket["purpose_counts"][purpose] += 1
        bucket["discovered_ids"].update(found)

    def final(bucket):
        found = bucket.pop("discovered_ids")
        bucket.update(unique_discovered=len(found), new_discovered=len(found - old),
                      existing_discovered=len(found & old), qualified_discovered=len(found & qualified),
                      new_qualified_discovered=len((found & qualified) - old),
                      discovered_ids=sorted(found))
        return bucket

    routes = (log.get("collection_plan") or {}).get("routes", {})
    route_groups = {rid: group() for rid in routes}
    work_groups = {kind: group() for kind in sorted(WORK_KINDS | {"unclassified"})}
    subjects, overall, missing_ids, read_attributed = {}, group(), set(), set()
    unassigned_subject_attempts = 0
    for attempt in log["attempts"].values():
        entry = log["entries"][attempt["entry_id"]]
        raw_found = set(attempt.get("discovered_lead_ids", []))
        missing_ids.update(raw_found - set(proposed))
        found = {canonical(lid) for lid in raw_found if lid in proposed} if attempt["status"] == "read" else set()
        raw_subjects = set(entry.get("subject_ids", []))
        missing_ids.update(raw_subjects - set(proposed))
        subject_ids = {canonical(lid) for lid in raw_subjects if lid in proposed}
        if attempt["status"] == "read":
            read_attributed.update(found | subject_ids)
        kind = entry.get("work_kind", "unclassified")
        if kind not in WORK_KINDS:
            kind = "unclassified"
        rid = entry.get("route_id") if entry.get("route_id") in routes else "__unrouted__"
        add(overall, attempt, entry, found)
        add(route_groups.setdefault(rid, group()), attempt, entry, found)
        add(work_groups[kind], attempt, entry, found)
        if kind in {"qualification", "contact"} and not subject_ids:
            unassigned_subject_attempts += 1
        for lid in subject_ids:
            # Work on one merchant does not attribute other discovered merchants to it.
            add(subjects.setdefault(lid, group()), attempt, entry, found & {lid})
    final(overall)
    for rid, bucket in route_groups.items():
        final(bucket)
        if rid in routes:
            bucket.update(state=routes[rid]["state"], size=routes[rid]["size"],
                          has_saved_progress=bool(routes[rid].get("progress")))
    for buckets in (work_groups, subjects):
        for bucket in buckets.values():
            final(bucket)
    total = _attempt_count(log)
    route_rank = sorted((rid for rid in route_groups if rid in routes),
                        key=lambda rid: (-route_groups[rid]["attempts"], rid))
    subject_rank = sorted(subjects, key=lambda lid: (-subjects[lid]["attempts"], lid))

    def largest(keys, groups, label):
        if not keys or not groups[keys[0]]["attempts"]:
            return None
        key = keys[0]
        return {label: key, "attempts": groups[key]["attempts"],
                "share_of_all_attempts": round(groups[key]["attempts"] / total, 4)}

    unstarted = sorted(rid for rid, route in routes.items()
                       if route["state"] in {"open", "active"}
                       and not route_groups[rid]["purpose_counts"]["merchant_collection"])
    deferred = sorted(rid for rid, route in routes.items() if route["state"] == "deferred")
    review = {"advisory_only": True,
              "largest_route": largest(route_rank, route_groups, "route_id"),
              "largest_subject": largest(subject_rank, subjects, "subject_id"),
              "routes_without_merchant_attempts_this_round": unstarted,
              "deferred_route_ids": deferred,
              "unclassified_attempts": work_groups["unclassified"]["attempts"],
              "verification_attempts_without_subject_ids": unassigned_subject_attempts,
              "review_basis": "Compare actual discoveries and qualification gaps with remaining observed routes; allocation figures do not prove a route is ineffective or authorize automatic deferral."}
    saved = {"unique_entities_in_batch": len(seen), "new_entities_in_batch": len(seen - old),
             "existing_entities_in_batch": len(seen & old),
             "entities_in_batch_without_read_attribution": sorted(seen - read_attributed),
             "entities_in_batch_without_discovery_attribution": sorted(seen - set(overall["discovered_ids"]))}
    effort = {"overall": overall, "by_route": route_groups, "by_work_kind": work_groups,
              "by_subject": subjects, "saved_records": saved,
              "unresolved_record_ids": sorted(missing_ids), "attribution_counts_are_nonadditive": True,
              "unplanned_attempts": len(log.get("deviations", {})),
              "scope": "Registered attempts in this round; unplanned calls are reported separately and consume budget without discovery attribution. Classification is recorded, not inferred; qualified counts require prepare and factual review."}
    return effort, review


def _summarize(log, baseline, packet, master, as_of=None):
    policy = validate_policy(packet["task"])
    proposed = dict(master["leads"], **{l["lead_id"]: l for l in packet.get("leads", [])})
    canonical = lambda lid: core.canonical_entity(lid, proposed)
    old = {canonical(lid) for lid in baseline["lead_ids"] if lid in proposed}
    seen = {canonical(l["lead_id"]) for l in packet.get("leads", [])}
    qualified = {canonical(l["lead_id"]) for l in packet.get("leads", []) if core.is_independent_main(l, packet["task"]) and not l.get("canonical_id")}
    brands = {canonical(l["lead_id"]) for l in packet.get("leads", []) if l.get("status") == "main" and core.collection_segment(l, packet["task"]) == "brand_store" and not l.get("canonical_id")}
    new = qualified - old
    updated = seen & old
    scope = core.scope_key(packet["task"])
    def prior_independent(lid):
        previous = master["leads"].get(lid, {}).get("qualifications", {}).get(scope, {})
        return previous.get("status") == "main" and (core.brand_policy(packet["task"]) == "white_label_required" or previous.get("collection_segment") == "independent")
    new_scope = {lid for lid in qualified & old if not prior_independent(lid)}
    counts = {"new_qualified": len(new), "qualified": len(qualified), "updated": len(updated), "existing_qualified": len(qualified & old), "new_scope_qualified": len(new_scope)}
    if core.brand_policy(packet["task"]) == "independent_product":
        counts.update(independent_main=len(qualified), brand_store_main=len(brands), new_brand_store_entities=len(brands - old))
    metric = policy["target_metric"]
    current = _time(as_of or core.now())
    end = _time(log["finish"]["finished_at"]) if log.get("finish") else current
    elapsed = max(0, (end - _time(log["started_at"])).total_seconds() / 60)
    attempts = list(log["attempts"].values())
    sources = dict(master["sources"], **{s["source_id"]: s for s in packet.get("sources", [])})
    channels = {e["channel_id"]: {"attempts": 0, "discovered_ids": set(), "read_entries": set(), "blocked_entries": set(), "pending_entries": []} for e in log["entries"].values()}
    for entry in log["entries"].values():
        _link_context(entry, sources)
        if entry["state"] == "pending":
            channels[entry["channel_id"]]["pending_entries"].append(entry["entry_id"])
        elif entry["state"] == "done":
            channels[entry["channel_id"]]["read_entries"].add(entry["entry_id"])
        else:
            channels[entry["channel_id"]]["blocked_entries"].add(entry["entry_id"])
    for a in attempts:
        core.require(set(a.get("source_ids", [])) <= set(sources), "Research attempt references source missing from commit")
        # Rejected records remain in the external research log, never become qualified counts.
        cid = log["entries"][a["entry_id"]]["channel_id"]
        group = channels[cid]
        group["attempts"] += 1
        if a["status"] == "read":
            group["discovered_ids"].update(canonical(lid) for lid in a.get("discovered_lead_ids", []) if lid in proposed)
    for cid, group in channels.items():
        found = group.pop("discovered_ids")
        group.update(unique_discovered=len(found), new_qualified=len(found & new), qualified=len(found & qualified), updated=len(found & updated))
        for key in ("read_entries", "blocked_entries"):
            group[key] = sorted(group[key])
    collection = _plan_status(log) if policy["collection_strategy"] in SYSTEMATIC_STRATEGIES else {}
    reason = None
    if collection.get("collection_complete"):
        reason = "coverage_complete"
    elif collection.get("market_sequence", {}).get("active_market_complete"):
        reason = "market_complete"
    elif counts[metric] >= policy["target_count"] and policy["completion_policy"] == "target_or_coverage":
        reason = "target_reached"
    elif _attempt_count(log) >= policy["work_budget"]["max_attempts"]:
        reason = "attempt_budget"
    elif elapsed >= policy["work_budget"]["max_minutes"]:
        reason = "time_budget"
    if log.get("finish"):
        reason = log["finish"]["reason"]
    pending = [e["entry_id"] for e in log["entries"].values() if e["state"] == "pending"]
    blocked = [e["entry_id"] for e in log["entries"].values() if e["state"] == "blocked"]
    result = {"recorded": True, "batch_id": packet["batch_id"], "policy": policy, "counts": counts, "target_observed": counts[metric], "target_reached": counts[metric] >= policy["target_count"], "remaining": max(0, policy["target_count"] - counts[metric]), "attempts": _attempt_count(log), "ledger_attempts": len(attempts), "unplanned_attempts": len(log.get("deviations", {})), "protocol_conformant": False if log.get("deviations") else None, "elapsed_minutes": round(elapsed, 2), "should_stop": bool(reason), "stop_reason": reason, "stop_detail": (log.get("finish") or {}).get("detail", ""), "pending_entry_ids": pending, "blocked_entry_ids": blocked, "next_action": "保存实际成果并交付，说明未达目标与剩余事项" if reason else "继续已登记且可执行的未读入口" if pending else "登记下一实际入口；无可执行路径时记录结束原因", "channels": channels, "channel_counts_are_nonadditive": True, "research_log_sha256": core.digest_bytes(core.encoded(log)), "fact_truth_verified": False}
    result.update(collection)
    result["research_effort"], result["allocation_review"] = _research_effort(log, proposed, old, seen, qualified)
    result["blocked_conditions"] = {eid: _last_failure(log, log["entries"][eid]) for eid in blocked}
    if reason:
        result["next_action"] = ("保存阶段成果及范围复核依据，交由用户决定；当前市场仍未完成，继续时仍调查本市场，调整范围须另建任务" if reason == "scope_review_required" else
                                 "保存实际成果并交付；明确剩余路线及本次覆盖边界" if collection.get("scope_completion") == "partial" else "保存实际成果并交付")
    return result


def status(root, batch_id, packet=None):
    with core.locked(root):
        folder, baseline, master, sha, packet = _context(root, batch_id, packet)
        log = _log(folder, baseline, packet)
        if log is None:
            result = {"recorded": False, "batch_id": batch_id, "policy": validate_policy(packet["task"]), "next_action": "登记实际可用渠道和待研究入口；尚未记录执行进度", "requires_reconciliation": sha != baseline["base_sha256"] and batch_id not in master["batches"]}
            if result["policy"]["collection_strategy"] in SYSTEMATIC_STRATEGIES:
                result.update(_plan_status({"policy": result["policy"]}))
            return result
        committed_summary = master["batches"].get(batch_id, {}).get("research_summary")
        result = copy.deepcopy(committed_summary) if committed_summary else _summarize(log, baseline, packet, master)
        result["requires_reconciliation"] = sha != baseline["base_sha256"] and batch_id not in master["batches"]
        result["committed"] = batch_id in master["batches"]
        return result


def finish(root, batch_id, reason, detail, packet=None, revision_detail=None):
    core.require(reason in STOP_REASONS, "Invalid stop_reason")
    core.nonempty(detail, "finish.detail")
    with core.locked(root):
        folder, baseline, master, sha, packet = _context(root, batch_id, packet)
        log = _log(folder, baseline, packet, True)
        core.require(batch_id not in master["batches"], "Committed research is frozen; continue in a new batch")
        previous = log.get("finish")
        if previous and previous["reason"] == reason and previous["detail"] == detail:
            return {"finished": True, "batch_id": batch_id, "stop_reason": reason, "already_finished": True}
        if previous:
            core.nonempty(revision_detail, "Finished research needs revision_detail to correct its finish reason after review")
        report = _summarize(log, baseline, packet, master)
        elapsed = (_time(previous["finished_at"] if previous else core.now()) - _time(log["started_at"])).total_seconds() / 60
        supported = {"target_reached": report["target_reached"], "attempt_budget": report["attempts"] >= log["policy"]["work_budget"]["max_attempts"], "time_budget": elapsed >= log["policy"]["work_budget"]["max_minutes"]}
        if reason in supported:
            core.require(supported[reason], "Declared finish reason is not supported by current progress")
        if reason == "coverage_complete":
            core.require(report.get("collection_complete"), "Collection remains partial; coverage completion requires an observed collection plan")
        if reason == "market_complete":
            core.require(report.get("market_sequence", {}).get("active_market_complete"), "Market completion requires an evidenced market-progress record")
        if reason == "scope_review_required":
            sequence = report.get("market_sequence", {})
            progress = (sequence.get("active_market") or {}).get("progress", {})
            core.require(sequence.get("needs_scope_review") and progress.get("review_batch_id") == batch_id,
                         "Scope-review finish requires an evidenced review_required market-progress record from this round")
        if log["policy"]["collection_strategy"] in SYSTEMATIC_STRATEGIES:
            if reason in {"batch_complete", "paths_exhausted", "coverage_complete"}:
                core.require(report["collection_complete"], "Collection remains partial; finish a budget/access checkpoint and preserve unfinished routes")
            if reason == "target_reached":
                core.require(log["policy"]["completion_policy"] == "target_or_coverage", "Coverage-driven task cannot stop only because a stage count was reached")
            if reason == "access_blocked":
                core.require(any(a["status"] in {"blocked", "needs_auth", "failed"} for a in log["attempts"].values()), "Access-blocked finish needs actual failed access attempts")
                routes = (log.get("collection_plan") or {}).get("routes", {}).values()
                pending = report["pending_entry_ids"]
                if log["policy"]["collection_strategy"] == "market_sequence":
                    active = report["market_sequence"]["active_market_id"]
                    routes = [r for r in routes if r.get("market_id") == active]
                    route_ids = {r["route_id"] for r in routes}
                    pending = [eid for eid in pending if log["entries"][eid].get("route_id") in route_ids or log["entries"][eid].get("purpose") != "merchant_collection"]
                core.require(not pending and not any(r["state"] in {"open", "active", "deferred"} for r in routes), "Executable routes, deferred routes or unread entries remain; one failed source cannot end collection")
        if previous:
            log.setdefault("finish_history", []).append({"previous": previous, "revision_detail": revision_detail, "revised_at": core.now()})
        log["finish"] = {"reason": reason, "detail": detail, "finished_at": previous["finished_at"] if previous else core.now()}
        core.safe_write(folder / "research-log.json", log)
    return {"finished": True, "batch_id": batch_id, "stop_reason": reason}


def summary_for_commit(root, packet, master):
    """Pure read hook. Caller already holds the workspace lock."""
    folder = root / "research" / packet["batch_id"]
    baseline = core.read_json(folder / "snapshot.json")
    log = _log(folder, baseline, packet)
    if log is None:
        core.require(validate_policy(packet["task"])["collection_strategy"] not in SYSTEMATIC_STRATEGIES, "Systematic collection requires observed research log and a truthful finish reason")
        return None
    core.require(log.get("finish"), "Record a truthful research finish reason before committing this round")
    for deviation in log.get("deviations", {}).values():
        _deviation_evidence(root, deviation["evidence"])
    summary = _summarize(log, baseline, packet, master)
    if log["finish"]["reason"] == "target_reached":
        core.require(summary["target_reached"], "Prepared subset no longer meets target; call finish with revision_detail and a truthful reason")
    if log["policy"]["collection_strategy"] == "market_sequence":
        active = summary.get("market_sequence", {}).get("active_market")
        sources = _sources(packet, master)
        for lead in packet.get("leads", []):
            assignment = lead.get("market_assignment")
            if assignment is not None:
                core.require(isinstance(assignment, dict) and active and assignment.get("market_id") == active["market_id"], "A market batch cannot contain another market's assigned customer")
            if lead.get("status") == "main":
                core.require(active and isinstance(assignment, dict) and assignment.get("relation") == "within", "Formal market customer needs market_assignment relation=within for the active market")
                core.nonempty(assignment.get("detail"), "market_assignment.detail")
                core.refs(assignment.get("source_ids", []), "market_assignment.source_ids", sources, strong=True)
                core.require(core.normalized(lead.get("country", "")) == core.normalized(active["country"]) and core.normalized(lead.get("city", "")) == core.normalized(active["city"]), "Formal market customer must have the active market's country and city; preserve original address separately")
    if log["policy"]["collection_strategy"] in SYSTEMATIC_STRATEGIES and any(l["status"] == "main" for l in packet.get("leads", [])):
        core.require(summary["preflight_ready"], "Qualified merchants require evidenced local channel/contact preflight")
        sources = _sources(packet, master)
        for lead in packet["leads"]:
            if lead["status"] != "main":
                continue
            evidence = {sid for sid in core.lead_source_ids(lead) if sources[sid]["level"] in core.STRONG}
            traced = False
            for attempt in log["attempts"].values():
                entry = log["entries"][attempt["entry_id"]]
                linked_ids = set(attempt.get("discovered_lead_ids", [])) | set(entry.get("subject_ids", []))
                if (attempt["status"] == "read" and lead["lead_id"] in linked_ids
                        and evidence.intersection(attempt.get("source_ids", []))
                        and entry.get("purpose") == "merchant_collection"
                        and entry.get("route_id") in log["collection_plan"]["routes"]
                        and (log["policy"]["collection_strategy"] != "market_sequence" or
                             log["collection_plan"]["routes"][entry["route_id"]].get("market_id") == summary["market_sequence"]["active_market_id"])):
                    traced = True
                    break
            core.require(traced, "Qualified merchant needs a source-linked merchant_collection attempt in this batch: " + lead["lead_id"])
    return summary


def carry_forward(root, from_id, to_id):
    """Carry unfinished entries and immutable read history, with a fresh round budget."""
    with core.locked(root):
        old_folder, old_baseline, master, sha, old_packet = _context(root, from_id)
        folder, baseline, master, sha, packet = _context(root, to_id)
        core.require(core.scope_key(old_packet["task"]) == core.scope_key(packet["task"]), "Carry-forward requires the same task scope; new geography/products need new research")
        old_log = _log(old_folder, old_baseline, old_packet)
        if old_log is None:
            return {"carried": False, "reason": "no_previous_log"}
        core.require(not (folder / "research-log.json").exists(), "New research round already has a log")
        log = _log(folder, baseline, packet, True)
        log["parent_batch_id"] = from_id
        log["deviation_history"] = copy.deepcopy(old_log.get("deviation_history", []))
        log["deviation_history"].extend(dict(copy.deepcopy(d), batch_id=from_id) for d in old_log.get("deviations", {}).values())
        if old_log.get("collection_plan"):
            log["collection_plan"] = copy.deepcopy(old_log["collection_plan"])
            log["collection_plan"]["inherited_from"] = from_id
            log["collection_plan_history"] = copy.deepcopy(old_log.get("collection_plan_history", []))
            if log["policy"]["collection_strategy"] == "market_sequence":
                previous_market = _market_sequence(old_log)
                if previous_market["active_market_complete"]:
                    log["collection_plan"]["batch_market_id"] = previous_market["next_market_id"]
                    log["collection_plan"]["market_transition"] = {"from_market_id": previous_market["active_market_id"], "to_market_id": previous_market["next_market_id"], "after_batch_id": from_id, "recorded_at": core.now()}
        log["read_history"] = copy.deepcopy(old_log.get("read_history", []))
        for entry in old_log["entries"].values():
            conditions = sorted(set(entry.get("prior_attempt_conditions", [])) | {a.get("retry_condition", "") for a in old_log["attempts"].values() if a["entry_id"] == entry["entry_id"]})
            if entry["state"] == "done":
                log["read_history"].append({"entry_identity": _entry_identity(entry), "entry_id": entry["entry_id"], "batch_id": from_id, "attempt_conditions": conditions})
            else:
                carry = copy.deepcopy(entry)
                failure = _last_failure(old_log, entry)
                if failure:
                    carry["last_failure"] = failure
                carry.pop("last_attempt_id", None)
                carry.pop("retry_condition", None)
                carry["prior_attempt_conditions"] = conditions
                carry["state"] = entry["state"]
                log["entries"][entry["entry_id"]] = carry
        core.safe_write(folder / "research-log.json", log)
    result = {"carried": True, "pending_or_blocked_entries": len(log["entries"]), "previously_read_entries": len(log["read_history"])}
    if log["policy"]["collection_strategy"] == "market_sequence":
        result["market_sequence"] = _market_sequence(log)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("register-channel", "record-environment", "set-collection-plan", "route-progress", "market-progress", "plan-entry", "record-attempt", "record-deviation", "status", "finish"):
        sub = commands.add_parser(name)
        sub.add_argument("--workspace", required=True)
        if name not in {"register-channel", "record-environment"}:
            sub.add_argument("--batch-id", required=True)
        if name in {"register-channel", "record-environment", "set-collection-plan", "route-progress", "market-progress", "plan-entry", "record-attempt", "record-deviation"}:
            sub.add_argument("--input", required=True)
        if name == "route-progress":
            sub.add_argument("--route-id", required=True)
        if name == "market-progress":
            sub.add_argument("--market-id", required=True)
        if name == "plan-entry":
            sub.add_argument("--defer", action="store_true", help="Save an unread queue item only; does not authorize a tool call")
        if name in {"status", "finish"}:
            sub.add_argument("--batch")
        if name == "finish":
            sub.add_argument("--reason", required=True, choices=sorted(STOP_REASONS))
            sub.add_argument("--detail", required=True)
            sub.add_argument("--revision-detail")
    args = parser.parse_args()
    try:
        root = core.workspace_path(args.workspace)
        payload = core.read_json(args.input) if hasattr(args, "input") else None
        packet = core.read_json(args.batch) if getattr(args, "batch", None) else None
        if args.command == "register-channel":
            result = register_channel(root, payload)
        elif args.command == "record-environment":
            result = record_environment(root, payload)
        elif args.command == "set-collection-plan":
            result = collection_plan(root, args.batch_id, payload)
        elif args.command == "route-progress":
            result = route_progress(root, args.batch_id, args.route_id, payload)
        elif args.command == "market-progress":
            result = market_progress(root, args.batch_id, args.market_id, payload)
        elif args.command == "plan-entry":
            result = plan_entry(root, args.batch_id, payload, defer=args.defer)
        elif args.command == "record-attempt":
            result = record_attempt(root, args.batch_id, payload)
        elif args.command == "record-deviation":
            result = record_deviation(root, args.batch_id, payload)
        elif args.command == "status":
            result = status(root, args.batch_id, packet)
        else:
            result = finish(root, args.batch_id, args.reason, args.detail, packet, args.revision_detail)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (core.LeadkitError, OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
