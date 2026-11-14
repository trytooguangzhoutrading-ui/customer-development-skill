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
STOP_REASONS = {"target_reached", "coverage_complete", "attempt_budget", "time_budget", "paths_exhausted", "access_blocked", "needs_business_reply", "user_stopped", "batch_complete"}
MAP_CATEGORIES = {"offline_markets", "wholesale_import_distributors", "retail_malls_supermarkets", "online_sellers", "social", "trade_associations"}
ENTRY_PURPOSES = {"channel_research", "route_reconnaissance", "merchant_collection"}
SIZE_ORDER = {"large": 0, "medium": 1, "small": 2}


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
    core.require(strategy in {"legacy", "region_size_ladder", "provided_leads"}, "Invalid collection_strategy")
    completion = task.get("completion_policy", "target_or_coverage")
    core.require(completion in {"coverage", "target_or_coverage"}, "Invalid completion_policy")
    return {"mode": mode, "target_count": target, "target_metric": metric, "work_budget": {"max_attempts": attempts, "max_minutes": minutes}, "collection_strategy": strategy, "completion_policy": completion}


def _time(value):
    core.iso_time(value, "research timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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


def _plan_status(log):
    plan = log.get("collection_plan")
    if not plan:
        return {"scope_completion": "partial", "collection_complete": False, "preflight_ready": False, "preflight_pending_categories": sorted(MAP_CATEGORIES), "unfinished_routes": [], "unresolved_categories": sorted(MAP_CATEGORIES), "next_route_id": None, "next_action": "先研究当地渠道地图和有证据的实际联系生态"}
    pending = [x["category"] for x in plan["channels"] if x["status"] == "needs_research" or (x["status"] == "blocked" and not x.get("blocked_attempts"))]
    preflight_ready = bool(plan["contact_ecosystem"]) and not pending
    unresolved = [x["category"] for x in plan["channels"] if x["status"] in {"needs_research", "blocked"}]
    covered = {r["category"] for r in plan["routes"].values()}
    missing = [x["category"] for x in plan["channels"] if x["status"] == "applicable" and x["category"] not in covered]
    unfinished = [r["route_id"] for r in plan["routes"].values() if r["state"] not in {"complete", "empty"}]
    complete = preflight_ready and not unresolved and not missing and not unfinished and bool(plan["routes"])
    available = [r for r in plan["routes"].values() if r["state"] in {"open", "active"}]
    available.sort(key=lambda r: (SIZE_ORDER.get(r["size"], 3), r["order"]))
    return {"scope_completion": "complete" if complete else "partial", "collection_complete": complete, "preflight_ready": preflight_ready, "preflight_pending_categories": sorted(pending), "unfinished_routes": unfinished, "unresolved_categories": sorted(set(unresolved + missing)), "next_route_id": available[0]["route_id"] if available and preflight_ready else None, "next_action": "先完成六类当地渠道初查和有证据的联系生态；受阻分类需真实失败记录及恢复条件" if not preflight_ready else "已处理本次实际发现的路线边界，可交付覆盖结果" if complete else "继续渠道地图中未完成的路线；未知规模先核实，不能冒充小型路线"}


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
        old_channels = {c["category"]: c for c in old["channels"]} if old else {}
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
            core.require(next(c for c in channels if c["category"] == item["category"])["status"] == "applicable", "A planned route needs an applicable map category")
            core.require(item.get("size") in {"large", "medium", "small", "unknown"}, "Route size must be observed, never inferred from missing data")
            core.refs(item.get("source_ids", []), "route.source_ids", sources, strong=True)
            core.refs(item.get("size_source_ids", []), "route.size_source_ids", sources, strong=True, required=item["size"] != "unknown")
            core.require(type(item.get("order")) is int and item["order"] > 0, "route.order must be positive integer")
            core.nonempty(item.get("entrypoint"), "route.entrypoint")
            core.nonempty(item.get("detail"), "route.detail")
            core.nonempty(item.get("next_action"), "route.next_action")
            route = {k: copy.deepcopy(v) for k, v in item.items() if k not in {"state", "progress", "progress_history"}}
            route["state"] = "open"
            if rid in old_routes:
                prior = old_routes[rid]
                core.require(all(prior.get(k) == route.get(k) for k in ("channel_id", "category", "entrypoint")), "Stable route_id cannot change its identity")
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
        if plan.get("revision_detail"):
            log["collection_plan"]["revision_detail"] = plan["revision_detail"]
        core.safe_write(folder / "research-log.json", log)
        return _plan_status(log)


def _route_gate(log, entry):
    if log["policy"]["collection_strategy"] != "region_size_ladder" or entry.get("purpose") != "merchant_collection":
        return
    plan = log.get("collection_plan")
    core.require(plan and _plan_status(log)["preflight_ready"], "Complete evidenced local channel/contact preflight before merchant collection")
    route = plan["routes"].get(entry.get("route_id"))
    core.require(route, "Merchant entry needs an observed route_id")
    core.require(route["channel_id"] == entry["channel_id"], "Entry channel_id must match its route")
    core.require(route["state"] in {"open", "active", "blocked"}, "Route is already finished or skipped")
    rank = SIZE_ORDER.get(route["size"], 3)
    earlier = [r["route_id"] for r in plan["routes"].values() if r["size"] in SIZE_ORDER and SIZE_ORDER[r["size"]] < rank and r["state"] in {"open", "active"}]
    core.require(not earlier, "Process executable larger routes first: " + ", ".join(earlier))


def route_progress(root, batch_id, route_id, update):
    core.require(isinstance(update, dict), "route progress must be an object")
    core.require(update.get("state") in {"active", "complete", "empty", "blocked", "skipped"}, "Invalid route progress state")
    core.nonempty(update.get("detail"), "Route progress needs discovered boundary/limitation detail")
    core.nonempty(update.get("cursor"), "Record actual page/directory boundary or unavailable cursor")
    core.string_list(update.get("attempt_ids", []), "route.attempt_ids")
    with core.locked(root):
        folder, baseline, master, sha, packet = _context(root, batch_id)
        log = _log(folder, baseline, packet, True)
        _mutable(master, batch_id, log)
        route = (log.get("collection_plan") or {}).get("routes", {}).get(route_id)
        core.require(route, "Unknown route_id")
        attempts = [log["attempts"].get(aid) for aid in update.get("attempt_ids", [])]
        core.require(all(a is not None and log["entries"][a["entry_id"]].get("route_id") == route_id for a in attempts), "Route progress must reference its own actual attempts")
        if update["state"] in {"complete", "empty", "blocked", "skipped"}:
            core.require(bool(attempts), "Closing/skipping a route needs actual attempt evidence")
        if update["state"] in {"complete", "empty"}:
            core.require(update.get("directory_exhausted") is True, "Completed route needs directory_exhausted=true for its actual observed boundary")
            core.require(any(a["status"] in {"read", "empty"} and log["entries"][a["entry_id"]].get("purpose") == "merchant_collection" for a in attempts), "Completed route needs actual merchant discovery attempts, not only channel background")
            core.require(not any(e.get("route_id") == route_id and e["state"] != "done" for e in log["entries"].values()), "Route still has unread or blocked entries")
            if update["state"] == "empty":
                core.require(not any(a.get("discovered_lead_ids") for a in log["attempts"].values() if log["entries"][a["entry_id"]].get("route_id") == route_id), "A route with discovered merchants is complete, not empty")
        if update["state"] in {"blocked", "skipped"}:
            core.nonempty(update.get("next_trigger"), "Unfinished route needs next_trigger")
        if route.get("progress"):
            route.setdefault("progress_history", []).append(route["progress"])
        route.update(state=update["state"], progress=dict(copy.deepcopy(update), recorded_at=core.now()))
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
    with core.locked(root):
        folder, baseline, master, sha, packet = _context(root, batch_id)
        core.require(bool(entry["products"]) and set(entry["products"]) <= set(packet["task"]["products"]), "Entry products outside task")
        core.require(entry["channel_id"] in _registry(root)["channels"], "Register a stable channel_id first")
        _link_context(entry, _sources(packet, master))
        log = _log(folder, baseline, packet, True)
        _mutable(master, batch_id, log)
        if not defer:
            core.require(len(log["attempts"]) < log["policy"]["work_budget"]["max_attempts"], "Attempt budget exhausted before execution; finish this round or save with --defer")
            elapsed = (_time(core.now()) - _time(log["started_at"])).total_seconds()
            core.require(elapsed < log["policy"]["work_budget"]["max_minutes"] * 60, "Time budget exhausted before execution; finish this round or save with --defer")
        stored = copy.deepcopy(entry)
        stored.pop("last_failure", None)  # Only actual attempts/carry-forward create this metadata.
        stored.pop("deferred", None)
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
            transient = {"state", "last_attempt_id", "prior_attempt_conditions", "last_failure", "deferred", "retry_condition"}
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
            if changed:
                core.safe_write(folder / "research-log.json", log)
            return {"entry_id": entry["entry_id"], "already_planned": True, "state": old["state"], "executable": not defer, "deferred": defer}
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
    return {"entry_id": entry["entry_id"], "already_planned": False, "state": "pending", "executable": not defer, "deferred": defer}


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
        core.require(len(log["attempts"]) < log["policy"]["work_budget"]["max_attempts"], "Attempt budget exhausted; finish this round")
        core.require((started - _time(log["started_at"])).total_seconds() < log["policy"]["work_budget"]["max_minutes"] * 60, "Attempt started after the time budget; finish this round")
        entry = log["entries"][attempt["entry_id"]]
        core.require(not entry.get("deferred"), "Deferred entry is not executable; run plan-entry without --defer immediately before the actual call")
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
        core.safe_write(folder / "research-log.json", log)
    return {"attempt_id": attempt["attempt_id"], "already_recorded": False, "entry_state": entry["state"]}


def _summarize(log, baseline, packet, master, as_of=None):
    policy = validate_policy(packet["task"])
    proposed = dict(master["leads"], **{l["lead_id"]: l for l in packet.get("leads", [])})
    canonical = lambda lid: core.canonical_entity(lid, proposed)
    old = {canonical(lid) for lid in baseline["lead_ids"] if lid in proposed}
    seen = {canonical(l["lead_id"]) for l in packet.get("leads", [])}
    qualified = {canonical(l["lead_id"]) for l in packet.get("leads", []) if l["status"] == "main" and not l.get("canonical_id")}
    new = qualified - old
    updated = seen & old
    scope = core.scope_key(packet["task"])
    new_scope = {lid for lid in qualified & old if master["leads"].get(lid, {}).get("qualifications", {}).get(scope, {}).get("status") != "main"}
    counts = {"new_qualified": len(new), "qualified": len(qualified), "updated": len(updated), "existing_qualified": len(qualified & old), "new_scope_qualified": len(new_scope)}
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
        group["discovered_ids"].update(canonical(lid) for lid in a.get("discovered_lead_ids", []) if lid in proposed)
    for cid, group in channels.items():
        found = group.pop("discovered_ids")
        group.update(unique_discovered=len(found), new_qualified=len(found & new), qualified=len(found & qualified), updated=len(found & updated))
        for key in ("read_entries", "blocked_entries"):
            group[key] = sorted(group[key])
    collection = _plan_status(log) if policy["collection_strategy"] == "region_size_ladder" else {}
    reason = None
    if collection.get("collection_complete"):
        reason = "coverage_complete"
    elif counts[metric] >= policy["target_count"] and policy["completion_policy"] == "target_or_coverage":
        reason = "target_reached"
    elif len(attempts) >= policy["work_budget"]["max_attempts"]:
        reason = "attempt_budget"
    elif elapsed >= policy["work_budget"]["max_minutes"]:
        reason = "time_budget"
    if log.get("finish"):
        reason = log["finish"]["reason"]
    pending = [e["entry_id"] for e in log["entries"].values() if e["state"] == "pending"]
    blocked = [e["entry_id"] for e in log["entries"].values() if e["state"] == "blocked"]
    result = {"recorded": True, "batch_id": packet["batch_id"], "policy": policy, "counts": counts, "target_observed": counts[metric], "target_reached": counts[metric] >= policy["target_count"], "remaining": max(0, policy["target_count"] - counts[metric]), "attempts": len(attempts), "elapsed_minutes": round(elapsed, 2), "should_stop": bool(reason), "stop_reason": reason, "stop_detail": (log.get("finish") or {}).get("detail", ""), "pending_entry_ids": pending, "blocked_entry_ids": blocked, "next_action": "保存实际成果并交付，说明未达目标与剩余事项" if reason else "继续已登记且可执行的未读入口" if pending else "登记下一实际入口；无可执行路径时记录结束原因", "channels": channels, "channel_counts_are_nonadditive": True, "research_log_sha256": core.digest_bytes(core.encoded(log)), "fact_truth_verified": False}
    result.update(collection)
    result["blocked_conditions"] = {eid: _last_failure(log, log["entries"][eid]) for eid in blocked}
    if reason:
        result["next_action"] = "保存实际成果并交付；明确剩余路线及本次覆盖边界" if collection.get("scope_completion") == "partial" else "保存实际成果并交付"
    return result


def status(root, batch_id, packet=None):
    with core.locked(root):
        folder, baseline, master, sha, packet = _context(root, batch_id, packet)
        log = _log(folder, baseline, packet)
        if log is None:
            result = {"recorded": False, "batch_id": batch_id, "policy": validate_policy(packet["task"]), "next_action": "登记实际可用渠道和待研究入口；尚未记录执行进度", "requires_reconciliation": sha != baseline["base_sha256"] and batch_id not in master["batches"]}
            if result["policy"]["collection_strategy"] == "region_size_ladder":
                result.update(_plan_status({}))
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
        if log["policy"]["collection_strategy"] == "region_size_ladder":
            if reason in {"batch_complete", "paths_exhausted", "coverage_complete"}:
                core.require(report["collection_complete"], "Collection remains partial; finish a budget/access checkpoint and preserve unfinished routes")
            if reason == "target_reached":
                core.require(log["policy"]["completion_policy"] == "target_or_coverage", "Coverage-driven task cannot stop only because a stage count was reached")
            if reason == "access_blocked":
                core.require(any(a["status"] in {"blocked", "needs_auth", "failed"} for a in log["attempts"].values()), "Access-blocked finish needs actual failed access attempts")
                routes = (log.get("collection_plan") or {}).get("routes", {}).values()
                core.require(not report["pending_entry_ids"] and not any(r["state"] in {"open", "active"} for r in routes), "Executable routes or unread entries remain; one failed source cannot end collection")
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
        core.require(validate_policy(packet["task"])["collection_strategy"] != "region_size_ladder", "Systematic collection requires observed research log and a truthful finish reason")
        return None
    core.require(log.get("finish"), "Record a truthful research finish reason before committing this round")
    summary = _summarize(log, baseline, packet, master)
    if log["finish"]["reason"] == "target_reached":
        core.require(summary["target_reached"], "Prepared subset no longer meets target; call finish with revision_detail and a truthful reason")
    if log["policy"]["collection_strategy"] == "region_size_ladder" and any(l["status"] == "main" for l in packet.get("leads", [])):
        core.require(summary["preflight_ready"], "Qualified merchants require evidenced local channel/contact preflight")
        sources = _sources(packet, master)
        for lead in packet["leads"]:
            if lead["status"] != "main":
                continue
            evidence = {sid for sid in core.lead_source_ids(lead) if sources[sid]["level"] in core.STRONG}
            traced = any(a["status"] == "read" and lead["lead_id"] in a.get("discovered_lead_ids", []) and evidence.intersection(a.get("source_ids", [])) and log["entries"][a["entry_id"]].get("purpose") == "merchant_collection" and log["entries"][a["entry_id"]].get("route_id") in log["collection_plan"]["routes"] for a in log["attempts"].values())
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
        if old_log.get("collection_plan"):
            log["collection_plan"] = copy.deepcopy(old_log["collection_plan"])
            log["collection_plan"]["inherited_from"] = from_id
            log["collection_plan_history"] = copy.deepcopy(old_log.get("collection_plan_history", []))
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
    return {"carried": True, "pending_or_blocked_entries": len(log["entries"]), "previously_read_entries": len(log["read_history"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("register-channel", "record-environment", "set-collection-plan", "route-progress", "plan-entry", "record-attempt", "status", "finish"):
        sub = commands.add_parser(name)
        sub.add_argument("--workspace", required=True)
        if name not in {"register-channel", "record-environment"}:
            sub.add_argument("--batch-id", required=True)
        if name in {"register-channel", "record-environment", "set-collection-plan", "route-progress", "plan-entry", "record-attempt"}:
            sub.add_argument("--input", required=True)
        if name == "route-progress":
            sub.add_argument("--route-id", required=True)
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
        elif args.command == "plan-entry":
            result = plan_entry(root, args.batch_id, payload, defer=args.defer)
        elif args.command == "record-attempt":
            result = record_attempt(root, args.batch_id, payload)
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
