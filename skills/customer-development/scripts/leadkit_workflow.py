#!/usr/bin/env python3
"""Local checkpoint, batch preparation and employee correction round trips.

No network calls, outreach, or third-party packages. Incomplete drafts never
become customer records until leadkit.commit accepts their evidence.
"""
import argparse
import copy
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import leadkit as core

HEADERS = ["客户编号", "商户名称（只读）", "联系入口编号（只读）", "人工备注", "需复核事项", "失效联系入口编号", "重新采集"]
META_HEADERS = ["项目", "值"]
GEO_KEYS = ("country", "city", "market", "address")
MANAGED_START = "<!-- customer-development:managed:start -->"
MANAGED_END = "<!-- customer-development:managed:end -->"


def merge_dict(base, changes):
    result = copy.deepcopy(base)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_dict(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _geo_merge(base, changes):
    """Keep one effective scope; explicit parent changes clear old children."""
    changes = copy.deepcopy(changes)
    if "country" in changes:
        core.nonempty(changes["country"], "country")
        if "countries" in changes:
            core.require(changes["countries"] == [changes["country"]], "Contradictory country/countries")
        changes["countries"] = [changes.pop("country")]
    result = merge_dict(base, changes)
    if "geo_scope" in changes:
        scopes = copy.deepcopy(changes["geo_scope"])
        core.require(isinstance(scopes, list) and scopes, "geo_scope must be a nonempty array")
        for scope in scopes:
            core.require(isinstance(scope, dict) and set(scope) <= set(GEO_KEYS), "Invalid geo_scope fields")
            core.nonempty(scope.get("country"), "geo_scope.country")
            for key, value in scope.items():
                core.nonempty(value, "geo_scope." + key)
        countries = list(dict.fromkeys(s["country"] for s in scopes))
        if "countries" in changes:
            core.require(isinstance(changes["countries"], list) and set(changes["countries"]) == set(countries), "Contradictory geo_scope/countries")
        for key in GEO_KEYS[1:]:
            if key in changes:
                core.require(all(s.get(key) == changes[key] for s in scopes), "Contradictory geo_scope/" + key)
    else:
        scopes = copy.deepcopy(base.get("geo_scope", []))
        if "countries" in changes or not scopes:
            countries = result.get("countries", [])
            core.require(isinstance(countries, list) and all(isinstance(c, str) and c.strip() for c in countries), "countries must be nonempty country strings")
            scopes = [{"country": country} for country in countries]
            if "countries" not in changes:
                for scope in scopes:
                    scope.update({k: base[k] for k in GEO_KEYS[1:] if base.get(k)})
        for index, key in enumerate(GEO_KEYS[1:], 1):
            if key in changes:
                core.require(scopes, "Choose countries before a narrower geography")
                core.nonempty(changes[key], key)
                for scope in scopes:
                    for child in GEO_KEYS[index + 1:]:
                        scope.pop(child, None)
                    scope[key] = changes[key]
        countries = list(dict.fromkeys(s["country"] for s in scopes))
    for key in ("country", "city", "market", "address", "geo_scope"):
        result.pop(key, None)
    if scopes:
        result["geo_scope"] = scopes
        result["countries"] = countries
        # Legacy fields are mirrors only, never an alternative interpretation.
        for key in GEO_KEYS[1:]:
            values = {s.get(key) for s in scopes}
            if len(values) == 1 and None not in values:
                result[key] = next(iter(values))
    return result


def _index(root):
    path = root / "execution" / "task-index.json"
    core.require(not path.is_symlink(), "Task index may not be a symlink")
    result = core.read_json(path) if path.exists() else {"schema_version": 1, "tasks": {}}
    core.require(result.get("schema_version") == 1 and isinstance(result.get("tasks"), dict), "Invalid task index")
    return result


def _activate(root, batch_id, **fields):
    """Caller holds the workspace lock; never touch customer master."""
    index = _index(root)
    stamp = core.now()
    task = index["tasks"].setdefault(batch_id, {"created_at": stamp})
    task.update(fields, updated_at=stamp)
    index.update(active_batch_id=batch_id, updated_at=stamp)
    core.safe_write(root / "execution" / "task-index.json", index)


def record_delivery(root, batch_id, output, view="batch"):
    """Optional post-export hook; call after the core export lock is released."""
    output = Path(output).expanduser().resolve()
    core.require(output.is_file(), "Delivery output must exist")
    with core.locked(root):
        master, _ = core.load_master(root)
        core.require(batch_id in master["batches"], "Delivery batch is not committed")
        delivery = {"batch_id": batch_id, "output": str(output), "view": view, "delivered_at": core.now()}
        index = _index(root)
        index.setdefault("tasks", {}).setdefault(batch_id, {})["latest_delivery"] = delivery
        index["latest_delivery"] = delivery
        # Exporting an old table must not steal an existing active task.
        index.setdefault("active_batch_id", batch_id)
        core.safe_write(root / "execution" / "task-index.json", index)
    return delivery


def snapshot(root, batch_id, overrides=None):
    """Company defaults then explicit task overrides; never infer a previous task."""
    import leadkit_project as project
    config_path, project_context = project.active_context(root)
    config = core.read_json(config_path)
    overrides = {} if overrides is None else overrides
    core.require(isinstance(overrides, dict), "Task overrides must be an object")
    defaults = config.get("task_defaults", {})
    core.require(isinstance(defaults, dict), "config.task_defaults must be an object")
    products = config.get("products", core.PRIMARY_PRODUCTS)
    buyers = config.get("default_buyer_types", core.DEFAULT_BUYER_TYPES)
    core.require(isinstance(products, list) and products and set(products) <= set(core.PRODUCTS), "Invalid company products")
    core.require(isinstance(buyers, list) and buyers and all(isinstance(x, str) and x.strip() for x in buyers), "Invalid company buyer types")
    supply = config.get("supplier_profile", config.get("supplier_constraints", {}))
    core.require(isinstance(supply, dict), "config.supplier_profile must be an object")
    brand_policy = config.get("brand_policy", "independent_product")
    task = {"products": products, "supplementary_products": copy.deepcopy(config.get("supplementary_products", [])), "customer_profile": {"buyer_types": buyers, "brand_policy": brand_policy, "collect_brand_stores": config.get("collect_brand_stores", brand_policy == "independent_product"), "supplier_constraints": supply}, "activity_max_age_days": config.get("activity_max_age_days", 180)}
    for key, default in {"mode": "new", "target_count": 20, "work_budget": {"max_attempts": 80, "max_minutes": 45}, "business_priorities": [], "preferred_buyer_types": [], "collection_strategy": "market_sequence", "completion_policy": "coverage"}.items():
        task[key] = copy.deepcopy(config.get(key, default))
    if "target_metric" in config:
        task["target_metric"] = config["target_metric"]
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
    task = _geo_merge({}, task)
    task = _geo_merge(_geo_merge(task, defaults), overrides)
    # An explicit product assignment is a scoped task, not an instruction to
    # carry the company's incidental categories into every specialist run.
    if "products" in overrides and "supplementary_products" not in overrides:
        task["supplementary_products"] = []
    elif "products" in defaults and "supplementary_products" not in defaults and "supplementary_products" not in overrides:
        task["supplementary_products"] = []
    if "supplementary_products" not in overrides and "supplementary_products" not in defaults:
        core.string_list(task.get("supplementary_products", []), "Company supplementary_products")
        task["supplementary_products"] = [p for p in task.get("supplementary_products", []) if p not in task["products"]]
    core.validate_product_scope(task)
    if "preferred_buyer_types" not in overrides and "preferred_buyer_types" not in defaults:
        allowed = task.get("customer_profile", {}).get("buyer_types", [])
        task["preferred_buyer_types"] = [buyer for buyer in task.get("preferred_buyer_types", []) if buyer in allowed]
    if task.get("mode") == "enrich":
        # A company-wide new-customer strategy is not an instruction to rescan
        # an entire city when the employee only wants known records repaired.
        for key, value in {"collection_strategy": "provided_leads", "completion_policy": "target_or_coverage", "target_metric": "updated"}.items():
            if key not in overrides and key not in defaults:
                task[key] = value
    if "target_count" in overrides and "completion_policy" not in overrides:
        task["completion_policy"] = "target_or_coverage"
    if "target_metric" not in task:
        task["target_metric"] = "updated" if task.get("mode") == "enrich" else "new_qualified"
    # Effective input hashes are generated from the binding, not task overrides.
    task["project_context"] = project_context
    import leadkit_research as research
    research.validate_policy(task)
    basic = getattr(core, "basic_snapshot", None)
    core.require(callable(basic), "Runtime requires basic_snapshot from leadkit 1.2")
    result = basic(root, batch_id)
    with core.locked(root):
        path = Path(result["batch_path"])
        packet = core.read_json(path)
        packet["task"] = merge_dict(packet["task"], task)
        core.safe_write(path, packet)
        _activate(root, batch_id)
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
        _activate(root, batch_id, checkpoint_sha256=digest)
    return dict(receipt, batch_path=str(path / "batch.json"), revision_path=str(revision))


def resume(root, batch_id):
    path = _draft_path(root, batch_id)
    with core.locked(root):
        master, sha = core.load_master(root)
        baseline = core.read_json(path / "snapshot.json")
        if batch_id in master["batches"]:
            _activate(root, batch_id)
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
        _activate(root, batch_id)
        # Expose checkpoint contents, never overwrite newer unsaved working edits.
        return {"batch_id": batch_id, "committed": False, "base_matches_current": sha == baseline["base_sha256"], "requires_reconciliation": sha != baseline["base_sha256"], "packet": packet, "working_path": str(path / "batch.json")}


def _configuration_review(root, task):
    """Compare real defaults with a saved task without migrating either one.

    A specialist request can intentionally differ from company defaults. A file
    hash or version mismatch is reported separately, never as a scope change.
    """
    import leadkit_project as project
    found = project.guard_json_workspace(root)
    bound_root, binding = found if found else (root, {})
    config_path = project._path(bound_root, binding.get("config_path", "config.json"), "config_path")
    task_context = task.get("project_context", {})
    versions = {"runtime": core.VERSION, "binding": binding.get("rules_version"),
                "task_binding": task_context.get("rules_version"),
                "task_runtime": task_context.get("runtime_rules_version")}
    result = {"available": config_path.is_file(), "config_path": str(config_path),
              "task_scope": {key: copy.deepcopy(task.get(key, [])) for key in ("products", "supplementary_products")},
              "rules_versions": versions, "scope_changed_automatically": False}
    if not result["available"]:
        result.update(review_required=True, product_scope_differs=None,
                      next_action="当前公司配置不可读；先恢复配置再核对范围。已保存任务和历史客户未改动。")
        return result
    config = core.read_json(config_path)
    defaults = config.get("task_defaults", {})
    core.require(isinstance(defaults, dict), "config.task_defaults must be an object")
    current = {"products": copy.deepcopy(defaults.get("products", config.get("products", core.PRIMARY_PRODUCTS))),
               "supplementary_products": copy.deepcopy(defaults.get("supplementary_products", [] if "products" in defaults else config.get("supplementary_products", [])))}
    # Match snapshot's filtering of inherited incidental products, without
    # changing explicit task defaults or rewriting historical configuration.
    if "supplementary_products" not in defaults:
        core.string_list(current["supplementary_products"], "Company supplementary_products")
        current["supplementary_products"] = [p for p in current["supplementary_products"] if p not in current["products"]]
    core.validate_product_scope(current)
    differences = {}
    for key, values in current.items():
        saved = result["task_scope"][key]
        core.string_list(saved, "Saved task." + key)
        if set(saved) != set(values):
            differences[key] = {"task": saved, "current_defaults": values,
                                "only_in_task": sorted(set(saved) - set(values)),
                                "only_in_defaults": sorted(set(values) - set(saved))}
    profile_defaults = defaults.get("customer_profile", {})
    core.require(isinstance(profile_defaults, dict), "config.task_defaults.customer_profile must be an object")
    current_brand = profile_defaults.get("brand_policy", config.get("brand_policy", "independent_product"))
    current_policy = {"brand_policy": current_brand,
                      "collect_brand_stores": profile_defaults.get("collect_brand_stores", config.get("collect_brand_stores", current_brand == "independent_product")),
                      "collection_strategy": defaults.get("collection_strategy", config.get("collection_strategy", "market_sequence"))}
    task_policy = {"brand_policy": core.brand_policy(task),
                   "collect_brand_stores": task.get("customer_profile", {}).get("collect_brand_stores", False),
                   "collection_strategy": task.get("collection_strategy", "legacy")}
    policy_differences = {key: {"task": task_policy[key], "current_defaults": value}
                          for key, value in current_policy.items() if task_policy[key] != value}
    versions["config_declared"] = config.get("rules_version")
    mismatches = {key: value for key, value in versions.items() if key != "runtime" and value is not None and value != core.VERSION}
    recorded_sha = task_context.get("config_sha256")
    file_changed = core.digest_bytes(config_path.read_bytes()) != recorded_sha if recorded_sha else None
    result.update(current_defaults=current, product_scope_differs=bool(differences), differences=differences,
                  task_policy=task_policy, current_policy=current_policy, policy_differences=policy_differences,
                  policy_scope_differs=bool(policy_differences),
                  configuration_file_changed_since_task=file_changed,
                  versions_differ_from_runtime=mismatches, review_required=bool(differences or policy_differences or mismatches or file_changed),
                  interpretation="任务与默认范围的差异可能来自专项要求；配置文件哈希或版本差异不单独证明业务范围变化。",
                  next_action="明确继续时沿用已保存任务范围、品牌政策、市场策略与原预算；本次要求采用新规则时，用 snapshot 建立新任务并明确地区，不改写旧任务或历史资格。" if differences or policy_differences or mismatches or file_changed else "当前默认与任务范围及政策一致；按活动任务接续。")
    return result


def status(root, batch_id=None):
    """Resolve an explicit selection or active pointer; never guess among tasks."""
    with core.locked(root):
        master, _ = core.load_master(root)
        index = _index(root)
        candidates = []
        for path in sorted((root / "research").iterdir()):
            if path.is_dir() and not path.is_symlink() and core.BATCH_RE.fullmatch(path.name) and (path / "batch.json").is_file():
                packet = core.read_json(path / "batch.json")
                task = master["batches"].get(path.name, packet).get("task", {})
                candidates.append({"batch_id": path.name, "committed": path.name in master["batches"], "request": task.get("request", "")})
        known = {c["batch_id"] for c in candidates}
        if batch_id is not None:
            core.valid_id(batch_id, "batch_id", core.BATCH_RE)
            core.require(batch_id in known, "Unknown task batch_id")
        else:
            active = index.get("active_batch_id")
            if active in known:
                batch_id = active
            else:
                unfinished = [c for c in candidates if not c["committed"]]
                options = unfinished or candidates
                if len(options) == 1:
                    batch_id = options[0]["batch_id"]
                elif options:
                    return {"selection_required": True, "candidates": options, "next_action": "Choose one batch_id; no task was resumed"}
                else:
                    return {"selection_required": False, "task_found": False, "next_action": "Create a task snapshot from the employee request"}
        saved = master["batches"].get(batch_id)
        packet = core.read_json(root / "research" / batch_id / "batch.json")
        result = {"task_found": True, "selection_required": False, "batch_id": batch_id, "committed": saved is not None,
                  "task": copy.deepcopy(saved["task"] if saved else packet["task"]), "latest_delivery": index["tasks"].get(batch_id, {}).get("latest_delivery"),
                  "next_action": "start a continuation batch" if saved else "resume saved research"}
        result["configuration_review"] = _configuration_review(root, result["task"])
    try:
        import leadkit_research as research
    except ModuleNotFoundError as exc:
        if exc.name != "leadkit_research":
            raise
    else:
        result["research"] = research.status(root, batch_id)
    return result


def continue_task(root, batch_id=None, new_batch_id=None):
    selected = status(root, batch_id)
    if selected.get("selection_required") or not selected.get("task_found"):
        return selected
    batch_id = selected["batch_id"]
    if not selected["committed"]:
        core.require(new_batch_id is None, "Uncommitted task resumes its existing batch; new-batch-id applies after commit")
        resumed = resume(root, batch_id)
        return dict(resumed, continued_from=batch_id, created=False, research=selected.get("research"),
                    configuration_review=_configuration_review(root, resumed["packet"]["task"]))
    with core.locked(root):
        master, _ = core.load_master(root)
        previous = copy.deepcopy(master["batches"][batch_id])
        task = copy.deepcopy(previous["task"])
        # Adding new defaults must not silently alter a saved task's gates.
        task.setdefault("collection_strategy", "legacy")
        task.setdefault("completion_policy", "target_or_coverage")
        old_queue_ids = {q["issue_id"] for q in previous.get("queue", [])}
        queue = [copy.deepcopy(q) for q in master.get("queue", []) if q["issue_id"] in old_queue_ids and q.get("state") != "resolved"]
        if new_batch_id is None:
            # Generated ID is only a name; selection always came from status().
            new_batch_id = "continue-" + secrets.token_hex(8)
    progress = selected.get("research", {})
    remaining = progress.get("remaining")
    metric = progress.get("policy", {}).get("target_metric", task.get("target_metric", "updated" if task.get("mode") == "enrich" else "new_qualified"))
    task["target_metric"] = metric
    if remaining is None:
        baseline = core.read_json(root / "research" / batch_id / "snapshot.json")
        qualified = [x for x in previous.get("lead_snapshots", []) if core.is_independent_main(x, task) and not x.get("canonical_id")]
        count_key = "new_independent_qualified_entities" if core.brand_policy(task) == "independent_product" else "new_qualified_entities"
        counts = {"new_qualified": previous.get("counts", {}).get(count_key, sum(x["lead_id"] not in baseline["lead_ids"] for x in qualified)),
                  "qualified": len(qualified), "updated": previous.get("counts", {}).get("updated_entities", 0)}
        remaining = max(0, task.get("target_count", 20) - counts[metric])
    if isinstance(remaining, int) and not isinstance(remaining, bool) and remaining > 0:
        task["target_count"] = remaining
    task["mode"] = "continue"
    created = snapshot(root, new_batch_id, task)
    packet = core.read_json(created["batch_path"])
    packet["region_research"] = previous.get("region_research", [])
    packet["queue"] = queue
    packet["continuation_of"] = batch_id
    packet["limitations"] = ["接续上一批次的任务范围及未结事项；地区研究为历史缓存，执行时按原核验时间复核。研究预算按新批次重新计算。"]
    checkpoint(root, packet)
    with core.locked(root):
        _activate(root, new_batch_id, continuation_of=batch_id)
    carried = None
    try:
        import leadkit_research as research
    except ModuleNotFoundError as exc:
        if exc.name != "leadkit_research":
            raise
    else:
        carried = research.carry_forward(root, batch_id, new_batch_id)
    result = dict(created, created=True, committed=False, continued_from=batch_id, packet=packet, carried_research=carried,
                  next_action="Research the continuation batch, checkpoint progress, validate and commit before exporting", budget_policy="per_batch",
                  configuration_review=selected["configuration_review"])
    if task["collection_strategy"] == "legacy":
        result["strategy_note"] = "历史任务保留原准入规则和策略；采用当前新规则时另建 market_sequence 任务，先补国家、城市、市场目录及顺序、当地渠道和联系生态，再逐市场采集。"
    return result


def _atomic_text(path, text):
    core.require(not path.is_symlink(), "Refusing symlink text file")
    handle, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def setup_project(root, dedicated=False):
    """Opt-in project instructions, only in an explicitly dedicated workspace."""
    core.require(dedicated, "setup-project requires --dedicated-workspace; choose the employee customer-research workspace")
    import leadkit_project as project
    bound_root, binding = project.locate(root)
    if binding is None:
        project.bind(root, {"kind": "json", "path": "execution/master.json"})
        bound_root, binding = project.locate(root)
    core.require(bound_root == root.resolve(), "Set project instructions at the bound project root: " + str(bound_root))
    report = project.inspect(root)
    core.require(report["ready"], "Restore missing bound project files before setup")
    with core.locked(root):
        path = root / "AGENTS.md"
        core.require(not path.is_symlink(), "AGENTS.md may not be a symlink")
        original = path.read_text(encoding="utf-8") if path.exists() else ""
        block = "\n".join([
            MANAGED_START,
            '## 客户采集专用项目',
            '员工用自然语言提出采集地区、产品、客户类型、数量或继续要求时，内部使用已安装的 customer-development skill，无需员工重复技能名称。',
            '先读取该技能 SKILL.md 和项目根 .customer-development-project.json；用 leadkit_project.py inspect 定位唯一主库、公司配置、当前有效规则及补充查重源。换窗口仍使用同一绑定，不新建空库，也不把旧批规则当当前规则。',
            'JSON主库按已绑定工作区 execution/task-index.json 定位任务并用 workflow continue接续；Excel主库按原表稳定ID、记录和断点增量处理，不迁移或初始化第二套JSON主库。',
            '新增客户必须对照主库和全部补充查重源；仅安装技能不共享历史数据，缺少文件不得宣称企业净新增。',
            '新采集任务先按国家→城市／地区→批发市场／批发城建立有来源的目录并完成当前城市渠道与联系生态预研，再按有据的市场优先顺序逐个采集场内商户；目录只说明已研究范围，不把局部目录当作全国完整目录。历史任务保留其已冻结策略，新规则另建任务接续。',
            '当前市场的已观察目录和所有已见入口完成后，才在下一批推进第二、第三市场；预算用尽继续当前市场，未读、受阻或暂缓均不算完成，不静默跳市场。每个市场独立交付 Excel，独立商户与品牌店分列；市场归属待核的主体保留待核，不冒入场内正式客户。',
            '员工主要接收最终客户 Excel、数量与必要限制；内部步骤和校验保存在本地。未核实证据不得伪造成客户，必须执行技能的准备、资格、去重和提交校验。',
            '此项目只做客户研究，不发送消息；员工纠错后按当前状态导出新表，历史交付保持不变。',
            MANAGED_END,
        ])
        count_start, count_end = original.count(MANAGED_START), original.count(MANAGED_END)
        core.require(count_start == count_end and count_start <= 1, "Ambiguous managed block; preserve AGENTS.md and repair its markers first")
        if count_start:
            start, end = original.index(MANAGED_START), original.index(MANAGED_END) + len(MANAGED_END)
            core.require(start < end - len(MANAGED_END), "Invalid managed block order")
            updated = original[:start] + block + original[end:]
        else:
            updated = original + ("\n\n" if original and not original.endswith("\n\n") else "") + block + "\n"
        backup = None
        if updated != original:
            if original:
                backup = root / ("AGENTS.md.before-customer-development-" + core.digest_bytes(original.encode("utf-8"))[:16])
                if backup.exists():
                    core.require(backup.read_text(encoding="utf-8") == original, "Instruction backup mismatch")
                else:
                    _atomic_text(backup, original)
            _atomic_text(path, updated)
    return {"instructions_path": str(path), "backup_path": str(backup) if backup else None, "changed": updated != original, "next_action": "Employees can enter natural customer-research requests in this project"}


def _subset(packet, leads, master_ids=(), master_sources=None):
    result = copy.deepcopy(packet)
    result["leads"] = copy.deepcopy(leads)
    ids = {x.get("lead_id") for x in leads if isinstance(x, dict)}
    sources = dict(master_sources or {})
    sources.update({s["source_id"]: s for s in packet.get("sources", []) if isinstance(s, dict) and s.get("source_id")})
    def supports(lead, product):
        try:
            return core.is_independent_main(lead, packet["task"]) and product in core.matched_products(lead, packet["task"], sources)
        except (KeyError, TypeError, AttributeError):
            # Malformed candidates are isolated by validate(), never promoted
            # while computing a provisional subset's coverage.
            return False
    for cell in result.get("coverage", []):
        if isinstance(cell, dict):
            cell["discovered_lead_ids"] = [x for x in cell.get("discovered_lead_ids", []) if x in ids]
            if cell.get("status") == "searched":
                found = set(cell["discovered_lead_ids"])
                qualified = any(x.get("lead_id") in found and supports(x, cell.get("product")) for x in leads if isinstance(x, dict))
                if not qualified:
                    cell["status"] = "searched_zero"
                    cell.setdefault("limitations", []).append("本准备文件仅统计保留记录，当前格无合格客户；完整原始覆盖见 original.json。")
    result["queue"] = [q for q in result.get("queue", []) if isinstance(q, dict) and (not q.get("lead_id") or q["lead_id"] in ids | set(master_ids))]
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
            shared = _subset(packet, [], master["leads"], master["sources"])
            shared_sources = core.validate_packet(shared, master)
            core.validate_issues(packet["queue"], shared_sources, "queue")
        except (core.LeadkitError, ValueError, TypeError, KeyError) as exc:
            errors.append(str(exc))
        raw_leads = packet.get("leads", [])
        if not isinstance(raw_leads, list):
            errors.append("leads must be an array")
            raw_leads = []
        if not errors:
            duplicates = {x.get("lead_id") for x in raw_leads if isinstance(x, dict) and isinstance(x.get("lead_id"), str) and sum(isinstance(y, dict) and y.get("lead_id") == x.get("lead_id") for y in raw_leads) > 1}
            pending, failures, selected = {}, {}, set()
            for index, lead in enumerate(raw_leads):
                if not isinstance(lead, dict):
                    failures[index] = "Lead must be an object"
                elif not isinstance(lead.get("lead_id"), str):
                    failures[index] = "lead_id must be a stable ASCII ID"
                elif lead.get("lead_id") in duplicates:
                    failures[index] = "Repeated lead_id: reconcile rows instead of choosing one"
                else:
                    pending[index] = lead

            def accept(indices):
                trial = [raw_leads[i] for i in sorted(selected | set(indices))]
                try:
                    validate(_subset(packet, trial, master["leads"], master["sources"]), master)
                except (core.LeadkitError, ValueError, TypeError, KeyError) as exc:
                    for i in indices:
                        failures[i] = str(exc)
                    return False
                selected.update(indices)
                for i in indices:
                    pending.pop(i, None)
                    failures.pop(i, None)
                return True

            # The complete relationship graph must be assessed first. A valid
            # packet is never reduced merely because its aliases appear later.
            if not failures and accept(list(pending)):
                pass
            else:
                # Connected canonical/alias rows are qualified together. Only
                # validated groups become trusted input for later retries.
                while pending:
                    before = len(pending)
                    by_id = {lead.get("lead_id"): i for i, lead in pending.items() if isinstance(lead.get("lead_id"), str)}
                    links = {i: {i} for i in pending}
                    for i, lead in pending.items():
                        target = by_id.get(lead.get("canonical_id")) if isinstance(lead.get("canonical_id"), str) else None
                        if target is not None:
                            links[i].add(target)
                            links[target].add(i)
                    unseen = set(pending)
                    groups = []
                    while unseen:
                        group, stack = set(), [min(unseen)]
                        while stack:
                            i = stack.pop()
                            if i not in group:
                                group.add(i)
                                stack.extend(links[i] - group)
                        unseen -= group
                        groups.append(sorted(group))
                    for group in groups:
                        accept(group)
                    if len(pending) == before:
                        # A broken alias must not poison a valid independent
                        # canonical row, nor supply it a duplicate exemption.
                        for i in list(pending):
                            accept([i])
                    if len(pending) == before:
                        break
            accepted = [raw_leads[i] for i in sorted(selected)]
            rejected = [{"index": i, "lead_id": lead.get("lead_id") if isinstance(lead, dict) else None, "error": failures.get(i, "Unresolved relationship dependency"), "record": copy.deepcopy(lead)} for i, lead in enumerate(raw_leads) if i not in selected]
        else:
            rejected = [{"index": i, "lead_id": x.get("lead_id") if isinstance(x, dict) else None, "error": "Shared validation blocked", "record": copy.deepcopy(x)} for i, x in enumerate(raw_leads)]
        prepared = _subset(packet, accepted, master["leads"], master["sources"]) if not errors else copy.deepcopy(packet)
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


def _apply_corrections(root, path, preview_id):
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
                issue = {"issue_id": "CORR-" + core.digest_bytes(core.encoded([preview_id, change["lead_id"]]))[:20], "kind": "employee_correction" if needs_review else "contact_correction", "detail": change["review_detail"] or ("员工标记联系入口失效，需补核可用入口" if invalid else "员工要求重新采集"), "state": "open", "next_action": "依据员工纠错重新核验客户及联系入口，补证后明确关闭本问题", "attempts": 0, "next_trigger": "next_collection", "source_ids": [], "decision_origin": "user", "created_at": stamp, "blocks_qualification": needs_review}
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


def corrections_apply(root, path, preview_id, output=None, scope_id=None, batch_id=None):
    core.require(not (scope_id and batch_id), "Choose scope-id or batch-id, not both")
    result = _apply_corrections(root, path, preview_id)
    result.update(applied=True, next_action="Export a new current view with leadkit export-current; frozen batch exports preserve their original facts")
    if output is not None:
        try:
            exported = core.export_current(root, output, scope_id=scope_id, batch_id=batch_id)
        except (core.LeadkitError, OSError, ValueError, TypeError, KeyError) as exc:
            result.update(export_succeeded=False, export_error=str(exc), next_action="Corrections are already applied. Retry export-current after fixing the output/scope error; do not modify the frozen batch")
        else:
            result.update(export_succeeded=True, current_export=exported, next_action="Deliver the new current-state workbook; retain old batch workbooks as history")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("snapshot", "checkpoint", "resume", "status", "continue", "record-delivery", "setup-project", "prepare", "correction-template", "corrections-preview", "corrections-apply"):
        sub = subs.add_parser(name)
        sub.add_argument("--workspace", required=True)
        if name in {"snapshot", "resume", "record-delivery"}:
            sub.add_argument("--batch-id", required=True)
        if name in {"status", "continue", "corrections-apply"}:
            sub.add_argument("--batch-id")
        if name == "continue":
            sub.add_argument("--new-batch-id")
        if name == "setup-project":
            sub.add_argument("--dedicated-workspace", action="store_true", help="Explicitly designate this initialized workspace for employee customer research")
        if name == "record-delivery":
            sub.add_argument("--output", required=True)
            sub.add_argument("--view", choices=["batch", "current"], default="batch")
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
            sub.add_argument("--output", help="Write a fresh current-state Excel after applying corrections")
            sub.add_argument("--scope-id")
    args = parser.parse_args()
    try:
        root = core.workspace_path(args.workspace)
        if args.command == "snapshot":
            result = snapshot(root, args.batch_id, core.read_json(args.overrides) if args.overrides else None)
        elif args.command == "checkpoint":
            result = checkpoint(root, core.read_json(args.batch))
        elif args.command == "resume":
            result = resume(root, args.batch_id)
        elif args.command == "status":
            result = status(root, args.batch_id)
        elif args.command == "continue":
            result = continue_task(root, args.batch_id, args.new_batch_id)
        elif args.command == "setup-project":
            result = setup_project(root, args.dedicated_workspace)
        elif args.command == "record-delivery":
            result = record_delivery(root, args.batch_id, args.output, args.view)
        elif args.command == "prepare":
            result = prepare(root, core.read_json(args.batch), args.output_dir)
        elif args.command == "correction-template":
            result = correction_template(root, args.output)
        elif args.command == "corrections-preview":
            result = corrections_preview(root, args.input)
        else:
            result = corrections_apply(root, args.input, args.preview_id, args.output, args.scope_id, args.batch_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (core.LeadkitError, OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
