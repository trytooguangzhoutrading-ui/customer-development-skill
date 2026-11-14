# 本地运行与数据契约 · 1.3

脚本只校验和保存已研究的记录，不联网、不证明来源真实。Codex须实际阅读证据并判断归属。Python 3.9+标准库即可。优先用snapshot生成结构，参照[虚构示例](../assets/batch-example.json)，不要猜字段。全部JSON用UTF-8；员工不负责填写JSON。

## 命令与版本

`<SKILL_DIR>`是本SKILL.md所在目录，`<PROJECT>`是独立客户工作区；路径有空格需加引号。Windows可用`py -3`。

```text
python3 <SKILL_DIR>/scripts/leadkit.py init --workspace <PROJECT>
python3 <SKILL_DIR>/scripts/leadkit_workflow.py snapshot --workspace <PROJECT> --batch-id <ID>
python3 <SKILL_DIR>/scripts/leadkit_workflow.py checkpoint --workspace <PROJECT> --batch <BATCH.json>
python3 <SKILL_DIR>/scripts/leadkit_workflow.py prepare --workspace <PROJECT> --batch <BATCH.json> --output-dir <PREPARED_DIR>
python3 <SKILL_DIR>/scripts/leadkit.py commit --workspace <PROJECT> --batch <PREPARED_BATCH.json>
python3 <SKILL_DIR>/scripts/leadkit.py export --workspace <PROJECT> --batch-id <ID> --output <OUTPUT.xlsx>
```

prepare返回实际待提交文件路径，不臆测文件名。共享任务错误先修复；单条隔离须保留被拒记录和修复队列，不能声称所有记录完成。`validate`只检查结构与证据引用，commit另检查基线、合并及重复关系。命令的精确可选参数见`--help`；断点、配置、纠错见[persistence.md](persistence.md)。

新提交使用`schema_version:3`；主库仍为schema1，向后兼容读取。安装不迁移客户数据。已提交旧批次可按原payload幂等重试，导出保留历史资格；旧未提交包需新快照与实际补证，不能只改版本号。同ID不同内容拒绝提交。导出不覆盖原文件。

## 批次与任务

顶层必需：`schema_version`、`batch_id`、`base_sha256`、`task`、`region_research`、`sources`、`leads`、`queue`、`coverage`、`limitations`。除task外的研究集合均为数组，limitations为字符串数组。base_sha256来自snapshot，不通过改hash跳过冲突。

batch_id为1–80位ASCII字母/数字/下划线/短横线，以字母或数字开头；lead_id/source_id还允许点和冒号，始终稳定且不分配给其他实体。

| task字段 | 契约 |
| --- | --- |
| request | 原始需求，非空字符串 |
| countries | 非空字符串数组，全球用`["全球"]`；与客户国家统一写法 |
| products | 六类固定产品中的非空子集；音箱先规范为蓝牙音响 |
| requirements | 必须满足的条件字符串数组；不把优先偏好混入 |
| customer_profile | `{buyer_types:[...],brand_policy:"white_label_required",supplier_constraints:{...}}`；未知供货条件不填为事实 |
| geo_scope | 范围对象数组，每项`country`及可选`city`、`market`、`address`；省略时从countries与旧city/market/address规范化 |
| activity_max_age_days | 活动证据评估窗口，默认180，允许1–730；只影响A评级，不直接淘汰B |
| allow_channel_candidates | 布尔值，默认false；允许相邻渠道候选，不豁免main门槛 |

mode为new/continue/enrich；target_count为正整数，默认20；target_metric为new_qualified/qualified/updated，新采及继续默认new_qualified，补证默认updated。work_budget:{max_attempts:80,max_minutes:45}是每轮上限，次数正整数，分钟大于0且不超过1440，员工明确要求覆盖默认。运行账本与结束顺序见[research-progress.md](research-progress.md)。数量是目标而非最低交付承诺。供货条件包括已确认的产品、价格带、MOQ和供货方式，未确认不猜。任务资格按规范化范围存储，与原始需求措辞、数量目标分开；实体在另一产品或地域任务不匹配不抹掉已有资格。

business_priorities为按先后排列的可选枚举：supplier_fit、product_coverage、buyer_type、procurement_access；preferred_buyer_types保存本轮偏好的画像类型。偏好不改变准入，也不混入requirements。新项目默认business_priorities=[buyer_type]，优先批发商、进口商、分销商、当地市场商户，并限制在本轮允许画像内；有显式偏好时按其可证指标排序，再按A/B，并列稳定。显式清空偏好后按A/B优先，旧配置不强制改写。

task.collection_strategy为region_size_ladder（新发现默认）、provided_leads（指定名单补证）或legacy（旧任务兼容）。task.completion_policy为coverage或target_or_coverage，未指定数量的新发现默认coverage，20只作阶段观察目标；显式数量可采用target_or_coverage。新系统采集必须保存渠道地图、实际联系生态和执行账本，不能删策略字段模拟旧任务绕过。字段与命令见[research-progress.md](research-progress.md)。

## 地区预研与来源

region_research每项：`country,area,status,checked_at,languages,markets,communication_channels,ecommerce_platforms,source_ids,limitations`。

- status为complete/partial/blocked；checked_at为含时区ISO时间。复用历史预研保留旧日期。
- 非blocked须有语言和可读原文来源。每名main须有适用地区预研；城市任务用region.city或area明确对应城市，无城市的市场任务须在area或markets列出该市场。不要求每个门牌独立预研，但同国其他城市不能替代本任务范围。
- 渠道数组每项`name,source_ids`；市场另有kind和size（large/medium/small/unknown），已知规模另需size_source_ids。
- 空渠道数组、partial或blocked须写具体限制。允许先做必要预研，不能用complete声称穷尽当地渠道。

sources每项必填`source_id,url,title,read_at,level,excerpt`；url为不含凭证的HTTP(S)地址，read_at是实际读取的含时区ISO时间，excerpt保留必要证据或标明转述。可附content_date、publisher、limitations。旧ID不可改内容；重读页面新建来源ID。

| level | 使用 |
| --- | --- |
| current_original | 本轮实际读到的原文 |
| third_party_original | 读到第三方目录/贸易等原文，不冒充商户自述 |
| cached_original | 完整历史缓存；必填limitations，不能证明当前活动 |
| search_snippet | 发现线索，不独立支撑正式资格 |
| failed_read | 访问失败记录，不作事实证据 |

所有source_ids引用必须真实存在且不重复。可引用主库历史来源。层级校验只判断声明，不能代替实际阅读。无网址的导入名单先待核，不捏造网址：discovered_by可使用`input_reference:{file,sheet?,row?,sha256?}`，并在同渠道coverage的input_references数组记录相同对象；此时发现来源的source_ids允许空。文件出处只证明从哪里发现，不能代替main的事实门槛。

## 客户及准入

每条lead必填`lead_id,name,country,status,reason,discovered_by`（发现渠道的精确结构见下文覆盖部分），status是**本次任务资格**：main/candidate/excluded，不是全局经营状态。可填city、market、address、market_location、aliases和人工初始备注；研究更新保留原manual_notes/manual_owner/sales_stage。

main需要以下所有条件；不能用分数抵消缺项：

| 字段 | 要求 |
| --- | --- |
| identity_source_ids | 可读的主体依据 |
| location_source_ids | 实际经营地域依据，送货范围不等于地址 |
| buyer_type / buyer_type_source_ids | 类型属于本轮画像，且有可读证据 |
| product_evidence | `[{product,detail,source_ids}]`，至少一种本轮目标产品有可读依据 |
| brand_profile | white_label或mixed；三类品牌排除也须有证据 |
| white_label_evidence | 同样`[{product,detail,source_ids}]`；须与上述**同一种本轮目标产品**相交 |
| contacts | 至少一个可归属、实际可发起联系且有证据的业务入口 |
| requirement_checks | 每条task.requirements对应`{requirement,status,detail,source_ids}`；main必须matched |
| supplier_fit | 已知供货约束对应`{constraint,status,detail,source_ids}`；unknown可保留，不得有conflict |

requirement_checks/supplier_fit的status为matched/unknown/conflict。匹配、冲突都需要依据，不能仅宣告状态。

细地域任务用`location_match:{scope_index:0,relation:"within",detail,source_ids}`。客户city/market/address须匹配该scope；不同名称须用`aliases:{city/market/address:任务规范名}`并附真实对应依据。不能用一个自由文本“within”绕过异地事实；同城不等于场内，商圈周边不自动算市场内。范围不明为candidate，已证不符记录本任务排除。

brand_profile可为white_label、mixed、brand_owner、official_store、brand_exclusive、unknown。名称像品牌不能下事实排除；无Logo/无品牌字段不能证明白牌。

excluded需`exclusion:{code,origin,detail,source_ids}`。code为brand_owner/official_store/brand_exclusive/outside_scope/product_mismatch/buyer_mismatch/closed/user_excluded/duplicate；origin为evidence或user。事实排除要可读证据，用户明确排除需user_instruction保留指令，不能把模型判断伪装成用户决定。证据不足使用candidate。

非excluded记录不得携带旧exclusion对象。重新纳入被全局排除的主体时，须提供exclusion_resolution:{detail,source_ids}，引用新读取且能解释旧决定为何不再适用的可读证据；成功后旧决定进history，不保留相反的有效状态。仅增加联系方式不能解除品牌排除。

## 联系方式与去重

contact必填`kind,value,role,attribution,source_ids`，value始终文本。kind支持phone、email、website、form、chat、whatsapp、zalo、facebook、instagram、telegram、tiktok、youtube、linkedin、other。

- phone/email须有真实可归属入口与可读证据；格式正常不表示已验证可达。电话不推WhatsApp/Zalo。
- form需`action:"form",action_evidence,action_source_ids`；社交/chat等需`action:"message"`及同样动作证据。展示主页不足。
- WhatsApp另需whatsapp_source_ids和whatsapp_evidence。value可为完整号码、https://wa.me/<数字>、https://wa.me/message/<商家短码>或https://api.whatsapp.com/send?phone=<数字>。链接必须实际观察且归属商户，不从号码拼造；普通WhatsApp首页不接受。website永不单独支撑main。
- status为active/historical/invalid；reachability仅untested/unavailable。本技能不拨打、不试发消息。无效、历史、已知不可用入口不支撑main。
- 规范化用于同实体同入口匹配：保留原始写法和value_variants，不猜国家码、缺失号码或邮箱。contact_id、last_verified_at、verified_batch_id由运行层维护；更新失效须指向同一稳定入口。历史沿用未重读明确标识。失效入口重新激活须有失效之后的新核验来源，不能重复引用旧证据。

平台共用域名和同名仅作关联提示。使用平台卖家ID/规范店铺URL识别店铺；自有域名须有归属依据。强联系重合需解释主体关系，不能把客服电话套给多个商户。

website默认`identity_scope:"unknown"`，不把整个域名当商户。明确自有域名用`identity_scope:"owned_domain"`及ownership_source_ids；平台店铺用`identity_scope:"platform_store"`、platform和seller_id或带店铺路径的URL。平台全站首页不得伪装成店铺。

明确同实体用`canonical_id`指向主记录，并提供`relationship_evidence:{detail,source_ids}`，保留旧ID和历史，不自动凭相似名字合并。强关联但确认不同实体时用`duplicate_review:{decision:"distinct_entities",reason,source_ids,related_lead_ids:[...]}`覆盖相关ID。潜在重复未解决先candidate。

## 评级、资格与问题

capability_evidence每项`{kind,detail,source_ids}`，kind为wholesale/import/distribution/sales_capacity。ongoing_activity为`{detail,source_ids,observed_on:"YYYY-MM-DD"}`；observed_on是实际活动日期，不能用读取时间冒充。

main只有同时具备经营能力、可读当前活动来源、实际日期且在task.activity_max_age_days内才为A；其他合格客户为B。未知日期、陈年活动或仅缓存不能自动A。priority和priority_reason由运行层生成。A/B是研究交接优先级，不是订单概率或销量估算。

主库分别保存entity_status（active/closed/merged/unknown）和qualifications（任务范围键→本次资格及qualified_products等）。一家公司可在眼镜任务合格、耳机任务不符。批次冻结当时结论；后续纠错不改写历史批次。当前资格须结合尚未关闭的问题及联系状态使用。

procurement_access可附{detail,source_ids}，只记录可归属的采购/供应商接洽入口依据，不将销售客服升级为采购权限。business_priority由脚本推导，包含criteria、signals、reason；只表示已证事实与本轮偏好，不是金额、意愿或概率预测。

issues为数组，每项`{issue_id,kind,detail,state,next_action,attempts,next_trigger,source_ids}`，state为open/resolved/blocked。resolved另需resolved_at及解决依据；无需再次执行已解决事项。旧unknowns可迁为未结问题，不因新批传空数组就自动消失。当前表显示未结事项，history保留解决轨迹。

关键问题kind为identity_conflict、location_conflict、white_label_conflict、product_conflict、buyer_conflict、contact_conflict、employee_correction时，open或blocked都会阻止相关正式资格。其他问题可显式blocks_qualification:true；未知采购量等普通未知项不自动阻断。affected_scope_ids/affected_products可界定地域、产品类问题的范围；不填作用所有范围，身份/联系归属冲突始终作用主体。已存关键问题不能通过传空数组、改kind或改为非阻断来绕开，解决须保留新证据。

queue同样采用稳定issue字段，可附lead_id。attempts为非负整数；next_trigger说明新证据/权限/时间等再次执行条件。不得把队列存在说成后台服务已安排，也不在条件未变化时无限重试。

## 覆盖与计数

coverage在**批次顶层**保存，每项`{product,channel,status,attempts,source_ids,limitations,discovered_lead_ids}`。每个任务产品至少一格，按实际计划渠道展开；状态为unsearched、blocked、searched_zero、searched。searched_zero指已研究但无合格结果，与未搜/受阻不同。attempts为实际尝试，limitations为字符串数组。不能为了填格捏造查询或强制六类均分客户数。

lead.discovered_by为`[{channel,source_ids}]`，记录实际发现路径；多渠道同一主体只算一个新增。覆盖中的客户ID须能追溯到本批记录，准备阶段隔离坏行仍保留拒绝明细与修复事项，不假称覆盖已完成。

批次counts保存本轮main/candidate/excluded数量，并分别记录new_entities、new_qualified_entities（本轮前未在主库出现且本轮合格的规范主体）、updated_entities、requalified（同范围重新合格）、first_qualified（主体首次在该范围合格，包含新主体）、new_contacts、new_sources。新增来源、补一个电话或已有候选晋级不等于新增主体。这些指标有交叉，不相加成客户总数；不要用main总数冒充新增客户。

## 保存与交付

同一主库一个写入者。commit持锁核snapshot/hash，备份原始主库后原子替换并读回；冲突时重读、合并、另建基线，不强改旧hash。相同payload重试幂等。详细目录、断点及纠错在[persistence.md](persistence.md)。

export导出本批冻结版本；export-current --batch-id <ID>或--scope-id <SCOPE>导出该范围当前有效客户。当前表从对应资格的证据快照取产品/地区事实，合并最新联系、人工备注、未结问题与主体状态，按当前日期重算评级，不借另一任务最新事实，不修改主库或历史表。旧批缺少可重建的范围证据时如实待核，不能猜。均生成可开发客户、待核线索、来源证据、采集说明；先按本轮有效业务偏好（新项目默认买家类型）排序，再按A/B；无额外偏好时A在B前。联系显示核验时效，未结问题与资格原因可读，覆盖和增量计数不藏在原始JSON里。排除明细留主库，计数留说明。零结果也据实导出。

所有单元格按文本保存，编号/电话前缀不丢，文本不执行公式。交付前独立回读工作表、计数、来源、联系方式；有渲染工具则检查显示。脚本不能保证事实真伪或每国一定有足量客户。
