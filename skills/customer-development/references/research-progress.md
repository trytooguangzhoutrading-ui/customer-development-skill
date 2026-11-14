# 研究进度、渠道可用性与停止条件

这些记录由Codex在后台维护，员工只需输入业务需求。脚本不联网；可用性及证据必须来自本机实际调用，不把工具名称存在或配置写入当作来源已经接通。

## 首次环境与渠道

先建snapshot，再登记实际观察。`leadkit_research.py record-environment --workspace <PROJECT> --input <JSON>`接收search、web_read、local_python三个键；每项为`{status,checked_at,detail}`。status为available/unavailable/untested，checked_at含时区；detail写实际工具结果或尚未测试的原因。不要先写“成功”再试工具。

`register-channel --workspace <PROJECT> --input <JSON>`接收：

```json
{
  "channel_id": "public_web",
  "name": "公开商户网页",
  "kind": "website",
  "readiness": {
    "status": "untested",
    "checked_at": "2026-11-14T08:00:00+08:00",
    "detail": "格式示例，实际运行需填真实观察时间和结果"
  },
  "alternatives": []
}
```

kind可为search/website/map/market/ecommerce/social/trade/association/rfq/provided/related。readiness.status为readable/needs_auth/blocked/untested，可附实际observed_url。channel_id稳定，重复任务沿用；不能仅换名称把同一路线当新渠道。一次URL可读不代表整个网站、账户或所有分页都可读；授权访问权仍由实际环境提供。付费海关等未接通就保持未测试或需授权，不预填可读。

## 渠道地图和规模路线

新发现任务默认collection_strategy=region_size_ladder。先用purpose=channel_research或route_reconnaissance的entry进行真实预研；此时可以尚无完整地图。之后运行`set-collection-plan --batch-id <ID> --input <PLAN.json>`，提交以下结构：

- checked_at：实际预研时间。
- channels：恰好覆盖offline_markets、wholesale_import_distributors、retail_malls_supermarkets、online_sellers、social、trade_associations六类。每项含category、status、detail、source_ids。status是applicable/needs_research/blocked/not_applicable；适用与不适用均需可读证据；blocked的source_ids留可读背景依据或空数组，不放failed_read；另有checked_at、next_trigger、attempt_ids（对应真实失败尝试）；该attempt的entry须为channel_research或route_reconnaissance且category对应该分类。未研究不能写不适用或无事实的受阻。
- contact_ecosystem：预研草稿可为空，但开始采商户前至少一项{kind,detail,source_ids}，须有可读的当地实际商业联系依据。全是unknown不能通过预研。
- routes：每项{route_id,channel_id,category,size,source_ids,size_source_ids,order,entrypoint,detail,next_action}。channel_id先登记；size为large/medium/small/unknown，已知规模需size_source_ids，order为正整数，entrypoint为真实发现入口或明确有界查询，detail说明规模口径和适用范围。

路线按已知大型→中型→小型推进，未知规模先预研，仍未知时单列在已知可执行路线之后。同层级可并行。商户采集entry用purpose=merchant_collection并带route_id；六类中任何一类仍needs_research、没有联系生态或还有未处理的大路线时，不能开始该商户路线。先完成各类初查和联系生态再从大到小展开；仅有独立站和Zalo样本不算城市渠道预研完成。预研purpose只用于实际渠道调查，不能冒充客户采集绕过顺序。

路线进度用`route-progress --batch-id <ID> --route-id <ROUTE> --input <UPDATE.json>`。UPDATE包含state、detail、cursor（非空字符串，写实际页码/边界或无法取得游标的原因）、attempt_ids（字符串数组），state可open/active/complete/empty/blocked/skipped。complete/empty必须有实际商户采集尝试、directory_exhausted:true及明确的已发现目录边界；尚有未读/受阻条目不能标完。有发现商户的路线用complete，不写empty。受阻/跳过必须有真实尝试及next_trigger，不能只因为产出少就跳过。

已发现路线不得删除；同route_id不能换channel/category/entrypoint。修订已登记规模或order须plan顶层revision_detail和新增可读依据，旧计划留历史。声明完成的范围始终是本次实际登记并展开的目录边界，不能写全城采尽。

## 逐次保存真实研究

命令均为`python3 <SKILL_DIR>/scripts/leadkit_research.py <命令> --workspace <PROJECT>`。

1. `plan-entry --batch-id <ID> --input <ENTRY.json>`登记待研究入口。必需entry_id、channel_id、kind、products、next_action；新系统采集还应明确purpose，商户采集带route_id。kind=query时填query；page填实际url；file填input_reference；目标href尚未显示但工具给了已见链接ID时用link，填parent_url、parent_source_id（已存可读母页来源且URL相符）、link_id（工具真实ID），不得从显示号码拼目标URL。link的最终地址在实际click结果来源中保存。可附cursor保存实际页码/游标，不能猜下一页链接。首个入口建立固定计时起点。默认plan-entry在调用前检查次数、时间及执行顺序（复用已有entry也检查），成功返回executable:true；每次实际调用前重新核该入口。只保存下一轮未读队列使用plan-entry --defer，返回executable:false，不能随后联网，也不能直接record-attempt；下一轮执行前用不带--defer的plan-entry重新检查并启用。受阻/已读入口重试还须新的retry_condition，不能借幂等返回重复访问。
2. 确认plan-entry成功后才执行实际搜索/读取；CLI非零或executable:false先停止实际访问，不继续网络操作，也不把本地校验失败计成网络尝试。先将来源、候选和失败观察保存到batch.json或checkpoint。
3. `record-attempt --batch-id <ID> --input <ATTEMPT.json>`记录attempt_id、entry_id、status、started_at、ended_at、detail、source_ids、discovered_lead_ids。status为read/empty/blocked/needs_auth/failed；blocked/needs_auth/failed须填写真实的next_trigger（再次尝试需要发生的条件），不是任意稍后重试。时间为真实发生时间；read的网页记录须引用已保存来源，客户ID也须已保存。每条正式客户还须能关联本批有实际来源的read商户采集尝试；仅写来源记录而无采集归因不能通过新系统提交。相同attempt_id相同内容幂等，不重复增加次数。
4. `status --batch-id <ID>`查看实际计数、预算、待处理入口、受阻入口和下一动作；可以`--batch <草稿>`评估当前有效记录，不能靠未核main声明承诺达标。正式达标以prepare后的可提交集合为准。

读取失败可保留为sources.level=failed_read，并由失败attempt引用。它能说明访问受阻，不能证明商户经营事实：coverage的blocked/unsearched行可引用失败记录；searched/searched_zero、客户事实和queue.source_ids使用可读证据，受阻问题在detail说明失败attempt/source编号，不用失败页作解决依据。

一个查询或页面调用按一次实际尝试保存；同一返回页的段落提取不虚增成多个客户。条数、页面数、目录总数与合格客户数分开。已读链接与未读入口保留；同一入口再次读取需记录实际改变的retry_condition，例如授权已恢复或新的可读版本。无变化的受阻路径不重试，改走已有可用来源。历史来源沿用不伪装成新访问，也不回填虚构的尝试时间。

## 目标与结束

mode=new/continue默认以new_qualified为目标，即本轮前主库不存在、当前通过准入且规范去重后的主体数。enrich默认以updated为目标；qualified可用于用户明确要求本批全部合格条数的任务。新增联系方式、既有候选晋级或重复别名不冒充新增合格主体。每条任务的target_count、target_metric和work_budget在开始前落盘；已开始后要改变政策应另建任务，不偷改计时。

新发现默认completion_policy=coverage，target_count=20是阶段观察目标，达到20不自动结束城市。员工显式数量可使用target_or_coverage，达到本次目标可交付。work_budget默认max_attempts=80、max_minutes=45，是每轮资源上限，可被需求覆盖。预算到达仍有路线未完成时必须写partial及接续位置。地图未查类别、受阻路线、未读入口不能被batch_complete/paths_exhausted掩盖。指定名单补证mode=enrich默认provided_leads，不强迫另建全城渠道地图。

顺序是checkpoint → workflow prepare → 对有效子集research finish → core commit → export → 回读并登记交付。finish命令：

```text
python3 <SKILL_DIR>/scripts/leadkit_research.py finish --workspace <PROJECT> --batch-id <ID> --batch <PREPARED.json> --reason <REASON> --detail <真实说明>
```

REASON为target_reached/coverage_complete/attempt_budget/time_budget/paths_exhausted/access_blocked/needs_business_reply/user_stopped/batch_complete。target_reached仅适用target_or_coverage；coverage_complete须地图及已发现路线确已处理。预算须与实际计数一致；access_blocked须有真实失败且没有可执行路线/未读入口，不能用一个网站失败替整批收尾。其他原因也须符合实际；新系统任务未完成时不可用batch_complete/paths_exhausted伪称完成。预检后目标不足不能写已达标。未提交的结束依据需要纠正时附`--revision-detail <修订原因>`，保留原结束历史且不重置起始时间。已提交记录冻结，另建接续批。

commit保存摘要及日志hash；来源和执行明细在私有工作区，不进入开源技能包。渠道贡献包含同主体多渠道交叉，不能相加成总新增，也不作为成交率。没有账本的旧批兼容读取，但不编造历史进度。新region_size_ladder任务必须有账本与真实结束记录，不能省略来绕过检查。所有“新增”计数相对该主库；另有外部历史库时须读取原件匹配，路径登记不等于跨库查重完成。

## 接续

workflow status/continue负责定位活动任务，research负责继承未读/受阻入口和已读历史。未提交恢复保持原计时；已提交接续开启新轮预算，继承原画像与范围；数量目标未满接续剩余量，已满再说继续则启动下一轮，coverage模式继续未完路线而非以阶段数量关闭任务。受阻入口携带last_failure（原批次、attempt、失败详情、来源、时间和next_trigger），status的blocked_conditions显示条件；接续保留原发生时间，不新增尝试。受阻入口仍需实际条件变化才能再执行。结束一批不表示全城采尽，保存队列不等于启动定时服务。
