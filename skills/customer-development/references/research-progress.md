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

## 当前城市预研和市场目录

新发现任务默认collection_strategy=market_sequence，国家、城市和市场逐层排列，每批固定一个市场。region_size_ladder只保留旧任务兼容，不把旧计划静默改成市场目录。先用purpose=channel_research或route_reconnaissance的entry进行真实预研；此时可以尚无完整地图。之后运行`set-collection-plan --batch-id <ID> --input <PLAN.json>`，提交以下结构：

- checked_at：实际预研时间。
- channels：恰好覆盖offline_markets、wholesale_import_distributors、retail_malls_supermarkets、online_sellers、social、trade_associations六类。每项含category、status、detail、source_ids。status是applicable/needs_research/blocked/not_applicable；适用与不适用均需可读证据；blocked的source_ids留可读背景依据或空数组，不放failed_read；另有checked_at、next_trigger、attempt_ids（对应真实失败尝试）；该attempt的entry须为channel_research或route_reconnaissance且category对应该分类。未研究不能写不适用或无事实的受阻。
- contact_ecosystem：预研草稿可为空，但开始采商户前至少一项{kind,detail,source_ids}，须有可读的当地实际商业联系依据。全是unknown不能通过预研。
- routes：每项{route_id,channel_id,category,size,source_ids,size_source_ids,order,entrypoint,detail,next_action}；market_sequence另有market_id，必须对应目录内真实市场。channel_id先登记；size为large/medium/small/unknown，已知规模需size_source_ids，order为正整数，entrypoint为真实发现入口或明确有界查询，detail说明规模口径和适用范围。

market_sequence的计划还必须包含market_directory及preflight_city_id：

- market_directory.countries：`{country_id,name,order}`。
- market_directory.cities：`{city_id,country_id,name,order,source_ids}`。
- market_directory.markets：`{market_id,city_id,name,order,priority_basis,source_ids,size_source_ids,boundary}`。城市和市场有可读来源，市场顺序有据，规模未知可留空size_source_ids；boundary写实际市场／目录范围。
- market_directory.coverage：`{status:"partial"|"complete",detail,source_ids}`，默认partial。目录完整需对应来源，全球目录始终partial；不因已列一城或几市场就称全国／全市列全。
- preflight_city_id：当前城市的ID；channels与contact_ecosystem须实际适用该城市。
- batch_market_id：计划固定本批市场，不能靠改计划进入第二市场。初次选顺序中第一个未完成市场，完成后仅下一接续批可推进。

同父级order唯一，已有目录节点、名称、父级和顺序不能删除、改名或重排绕过未完成市场。新发现真实地区／市场可补登记，保留来源与完整性限制。

每条商户采集entry用purpose=merchant_collection并带route_id，可附market_id但须与route一致；只能进入本批当前市场。六类未初查完、缺联系生态或preflight_city_id不是当前城市时，不能采商户。同市场内路线按有据大型→中型→小型安排，未知规模先核、仍未知单列；同市场内可并行。旧region_size_ladder仍按原路线顺序，但新默认不能借旧策略跨市场跳采。预研purpose只用于真实渠道调查，不能伪装采商户绕过顺序。

路线进度用`route-progress --batch-id <ID> --route-id <ROUTE> --input <UPDATE.json>`。UPDATE包含state、detail、cursor（非空字符串，写实际页码/边界或无法取得游标的原因）、attempt_ids（字符串数组），state可active/complete/empty/blocked/skipped/deferred。complete/empty必须有实际商户采集尝试、directory_exhausted:true及明确的已发现目录边界；尚有未读/受阻条目不能标完。有发现商户的路线用complete，不写empty。受阻/跳过必须有真实尝试及next_trigger。暂缓使用deferred，另见下文；不能用一句“产出低”代替实际投入、缺口和替代路线依据。

已发现路线不得删除；同route_id不能换channel/category/entrypoint。修订已登记规模或order须plan顶层revision_detail和新增可读依据，旧计划留历史。声明完成的范围始终是本次实际登记并展开的目录边界，不能写全城采尽。

## 当前市场完成与推进

```text
python3 <SKILL_DIR>/scripts/leadkit_research.py market-progress --workspace <PROJECT> --batch-id <ID> --market-id <当前市场ID> --input <UPDATE.json>
```

UPDATE只接受`state:"complete"`，另须detail、boundary、cursor、`directory_exhausted:true`、attempt_ids及source_ids。必须是当前市场，当前城市预研已完成；该市场全部已登记路线为complete／empty、没有未读或受阻入口；完成依据须来自该市场真实可读的边界尝试，attempt_ids和source_ids对应。脚本保留原进度和写入时间，completion_scope为observed_directory_boundary。

这个字段只证明所声明的目录边界已处理，不能自动证明全市场商户已列全。Codex必须核目录／分区边界确实足以支持用户要求的当前市场完整采集，才登记完成；若只读完有限搜索结果或目录不完整，保留未完成和真实限制，不用命令通过代替全场证据。

当前市场完成后，本批停止新增商户采集，将当前结果合入统一客户总表；finish可用market_complete，下一次workflow continue才推进到next_market_id。当前市场未完成时，预算结束、受阻、暂缓或数量达标均继续保留该市场。同城可沿用有效预研，跨城preflight_city_id不符时须先更新渠道与联系生态。已列队列完成但目录未证完整时仍需扩展目录，不宣称全城／全国完成。

## 逐次保存真实研究

命令均为`python3 <SKILL_DIR>/scripts/leadkit_research.py <命令> --workspace <PROJECT>`。

1. `plan-entry --batch-id <ID> --input <ENTRY.json>`登记待研究入口。必需entry_id、channel_id、kind、products、next_action；新系统采集还应明确purpose，商户采集带route_id。kind=query时填query；page填实际url；file填input_reference；目标href尚未显示但工具给了已见链接ID时用link，填parent_url、parent_source_id（已存可读母页来源且URL相符）、link_id（工具真实ID），不得从显示号码拼目标URL。link的最终地址在实际click结果来源中保存。可附cursor保存实际页码/游标，不能猜下一页链接。首个入口建立固定计时起点。默认plan-entry在调用前检查次数、时间及执行顺序（复用已有entry也检查），成功返回executable:true；每次实际调用前重新核该入口。只保存下一轮未读队列使用plan-entry --defer，返回executable:false，不能随后联网，也不能直接record-attempt；下一轮执行前用不带--defer的plan-entry重新检查并启用。受阻/已读入口重试还须新的retry_condition，不能借幂等返回重复访问。
2. 确认plan-entry成功后才执行实际搜索/读取；CLI非零或executable:false先停止实际访问，不继续网络操作，也不把本地校验失败计成网络尝试。每次成功执行预检由脚本写入精确registered_at，不能通过输入回填；真实started_at必须在其后。旧入口缺少该时间时，执行新访问前先重新plan-entry；旧日志不补造时间。组合工具编排必须显式检查退出码和返回的executable，再进入联网分支；未实现此分支时分开两次工具调用，不能把登记与联网放入无条件连续调用或并行队列。先将来源、候选和失败观察保存到batch.json或checkpoint。
3. `record-attempt --batch-id <ID> --input <ATTEMPT.json>`记录attempt_id、entry_id、status、started_at、ended_at、detail、source_ids、discovered_lead_ids。status为read/empty/blocked/needs_auth/failed；blocked/needs_auth/failed须填写真实的next_trigger（再次尝试需要发生的条件），不是任意稍后重试。时间为真实发生时间；read的网页记录须引用已保存来源，客户ID也须已保存。每条正式客户还须能关联本批有实际来源的read商户采集尝试；仅写来源记录而无采集归因不能通过新系统提交。相同attempt_id相同内容幂等，不重复增加次数。
4. `status --batch-id <ID>`查看实际计数、预算、待处理入口、受阻入口和下一动作；可以`--batch <草稿>`评估当前有效记录，不能靠未核main声明承诺达标。正式达标以prepare后的可提交集合为准。

读取失败可保留为sources.level=failed_read，并由失败attempt引用。它能说明访问受阻，不能证明商户经营事实：coverage的blocked/unsearched行可引用失败记录；searched/searched_zero、客户事实和queue.source_ids使用可读证据，受阻问题在detail说明失败attempt/source编号，不用失败页作解决依据。

每个实际查询或网页工具读取调用按一次尝试保存，包括同一页面的open(ref_id,lineno)续读、点击或重新抓取；一次工具请求中包含多个查询/页面时逐项计数。仅对已经返回的文本做本地截取不增加访问次数，也不增加客户数。页内续读在原URL上记录实际已见的行范围/段落位置为cursor，先登记相应entry，再执行读取；不能将多次open合成一次来减少预算。条数、页面数、目录总数与合格客户数分开。已读链接与未读入口保留；无新cursor的同一入口再次读取需记录实际改变的retry_condition，例如授权已恢复或新的可读版本。无变化的受阻路径不重试，改走当前市场已有可用来源，不能自动进入其他市场。历史来源沿用不伪装成新访问，也不回填虚构的尝试时间。

## 研究投入和路线暂缓

若执行者违反上述顺序，实际发生未登记访问，不补造事前登记，也不继续按较小账本放大预算。立即保存该次实际工具回执及真实起止时间，用`record-deviation --batch-id <ID> --input <JSON>`记入独立异常账本。JSON为`{deviation_id,kind:"unplanned_network",started_at,ended_at,detail,evidence:{path,sha256}}`；一个记录对应一次真实访问，path是工作区内单次原始回执文件的相对路径（禁止符号链接），sha256为原文件内容哈希。该命令不联网、不授予执行权限、不增加发现或合格归因；报告必须披露偏差，不能称该轮严格合规。实际预算包含正常attempts与deviations，status分别列ledger_attempts和unplanned_attempts；有偏差时protocol_conformant=false，无记录时为null，不自动证明执行合规。已提交批次不回写，下一批保留deviation_history但不把旧访问计为新轮尝试。仅恢复真实异常，不把普通资料缺口或程序校验错误伪造为网络访问。

新商户动作的entry可带work_kind：discovery（发现新商户）、qualification（资格补证）、contact（联系补充）；subject_ids关联已在主库或草稿保存的对象。分类描述动作目的，不改变purpose、路线顺序或资格。新任务应按实际动作填写；旧记录缺失时显示unclassified，不回填推测分类。实际发现仍由attempt.discovered_lead_ids记录，只能引用真实已保存候选；目录总数不是该数组。

status的research_effort按overall、by_route、by_work_kind和by_subject显示投入、读取结果及去重发现；allocation_review提示实际集中程度、未展开和已暂缓路线。数据用于Codex在目录边界、继续深挖同一主体或切换路线时审视：是否还有更有希望发现新主体或闭合缺口的真实入口。它不设固定转化率，不按分数自动判定客户，也不自动暂停或换路线。未归类的旧动作与没有归因的候选如实显示，不能把已有资料当新发现。

暂缓尚未完成路线的route-progress使用state=deferred。除detail、cursor、attempt_ids外，填写next_trigger（何时恢复）、next_action（恢复后读哪个真实入口）、allocation_reason（本路线实际投入、未闭合原因及替代依据）、alternative_route_id（计划内真实且open/active的另一条路线；market_sequence必须在同一当前市场）。保留该路线全部已读、未读和失败记录；deferred不代表访问受阻、无客户或完成覆盖。需要恢复时显式记录state=active及实际理由，不重置轮次预算。暂缓较大路线后可转向同市场内其他可执行路线；不能跨市场跳采，不能通过互相暂缓制造“全部完成”。

## 目标与结束

mode=new/continue默认以new_qualified为目标，即本轮前主库不存在、当前通过准入且规范去重后的独立主体数。independent_product下品牌店分类计数，不充独立商户目标。enrich默认以updated为目标；qualified可用于用户明确要求本批全部合格条数的任务。新增联系方式、既有候选晋级或重复别名不冒充新增合格主体。每条任务的target_count、target_metric和work_budget在开始前落盘；已开始后要改变政策应另建任务，不偷改计时。

新发现默认completion_policy=coverage，target_count=20是阶段观察目标，达到20不自动结束市场或城市。员工显式数量可使用target_or_coverage，达到本次目标可交付。work_budget默认max_attempts=80、max_minutes=45，是每轮资源上限，可被需求覆盖。预算到达仍有路线未完成时必须写partial、当前市场及接续位置，不自动跳下一市场。地图未查类别、受阻路线、未读入口不能被batch_complete/paths_exhausted掩盖。指定名单补证mode=enrich默认provided_leads，不强迫另建全城渠道地图。

顺序是checkpoint → workflow prepare → 对有效子集research finish → core commit → export → 回读并登记交付。finish命令：

```text
python3 <SKILL_DIR>/scripts/leadkit_research.py finish --workspace <PROJECT> --batch-id <ID> --batch <PREPARED.json> --reason <REASON> --detail <真实说明>
```

REASON为target_reached/coverage_complete/market_complete/attempt_budget/time_budget/paths_exhausted/access_blocked/needs_business_reply/user_stopped/batch_complete。target_reached仅适用target_or_coverage；coverage_complete须地图、目录范围及已发现路线确已处理；market_complete须当前市场有实际market-progress完成记录，不代表全城完成。预算须与实际计数一致；access_blocked须有真实失败且当前市场没有可执行路线／未读入口，不能用一个网站失败替整批收尾，更不能当作切换下一市场的许可。其他原因也须符合实际；新系统任务未完成时不可用batch_complete/paths_exhausted伪称完成。预检后目标不足不能写已达标。未提交的结束依据需要纠正时附`--revision-detail <修订原因>`，保留原结束历史且不重置起始时间。已提交记录冻结，另建接续批。

commit保存摘要及日志hash；来源和执行明细在私有工作区，不进入开源技能包。渠道贡献包含同主体多渠道交叉，不能相加成总新增，也不作为成交率。没有账本的旧批兼容读取，但不编造历史进度。market_sequence及region_size_ladder任务必须有账本与真实结束记录，不能省略来绕过检查。所有“新增”计数相对该主库；另有外部历史库时须读取原件匹配，路径登记不等于跨库查重完成。

## 接续

workflow status/continue负责定位活动任务，research负责继承未读/受阻入口和已读历史。未提交恢复保持原计时；已提交接续开启新轮预算，继承原画像与范围；market_sequence恢复当前未完成市场，只有原市场已完成才进入下一市场，跨城先重核预研；数量目标未满接续剩余量，已满再说继续则启动下一轮，coverage模式继续未完路线而非以阶段数量关闭任务。受阻入口携带last_failure（原批次、attempt、失败详情、来源、时间和next_trigger），status的blocked_conditions显示条件；接续保留原发生时间，不新增尝试。受阻入口仍需实际条件变化才能再执行。结束一批不表示全城采尽，保存队列不等于启动定时服务。
