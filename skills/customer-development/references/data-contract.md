# 本地运行与数据契约

脚本只保存和校验 Codex 已研究的结果，不联网、不搜索、不证明来源真实。研究、字段归属、城市/市场精度、买家角色和任务必需条件仍由 Codex 判断。优先使用 snapshot 生成的结构，不从头猜字段。所有文件使用 UTF-8 JSON。

## 执行与目录

在独立客户项目运行，`<SKILL_DIR>` 是当前 SKILL.md 所在目录。路径含空格时加引号。Windows 可将 python3 换成 py -3。

```text
python3 <SKILL_DIR>/scripts/leadkit.py init --workspace <PROJECT>
python3 <SKILL_DIR>/scripts/leadkit.py snapshot --workspace <PROJECT> --batch-id <BATCH_ID>
python3 <SKILL_DIR>/scripts/leadkit.py validate --workspace <PROJECT> --batch <PROJECT>/research/<BATCH_ID>/batch.json
python3 <SKILL_DIR>/scripts/leadkit.py commit --workspace <PROJECT> --batch <PROJECT>/research/<BATCH_ID>/batch.json
python3 <SKILL_DIR>/scripts/leadkit.py export --workspace <PROJECT> --batch-id <BATCH_ID> --output <PROJECT>/outputs/<BATCH_ID>.xlsx
```

init 仅用于没有 config.json、execution、research、outputs 的新目录，拒绝覆盖。存在旧 Excel 主库时走 [persistence.md](persistence.md)，不能把原目录强行初始化。

- `config.json`：产品及默认范围的提示配置；Codex 读取后填写本批次任务，脚本不会自动继承筛选参数。
- `execution/master.json`：唯一 JSON 主库，保存来源、主体、人工字段、批次及版本历史。
- `execution/backups/`：提交前主库备份。
- `research/<BATCH_ID>/snapshot.json`：创建批次时的基线，不能篡改以跳过并发检查。
- `research/<BATCH_ID>/batch.json`：Codex 填写的本轮研究记录，提交后不改；修正另建批次。
- `outputs/`：员工可读的交付表格，不是第二套主库。

`dedupe --workspace <PROJECT>` 可读取已保存的关联提示，不会自动合并。validate 仅验证字段和引用；`base_matches_current: false` 不能提交。commit 才会校验保存基线与重复主体。已提交相同内容可安全重试；相同批次不同内容会拒绝。发生并发冲突时重读、合并、另建新批次。导出拒绝覆盖旧文件，重导出使用新文件名。

## 批次对象

新批次必填：`schema_version: 2`、`batch_id`、`base_sha256`、`task`、`region_research`、`sources`、`leads`、`queue`、`limitations`。sources/leads/queue/limitations为数组，允许空；limitations为字符串数组。region_research至少记录一个真实地区的预研或受阻情况。主库结构仍为schema_version 1；这与新批次版本不同。

batch_id 为 1–80 位 ASCII 字母、数字、下划线或短横线，以字母或数字开头。客户及来源 ID 还允许点和冒号。ID 稳定且不复用给其他主体。base_sha256 必须保留 snapshot 返回值。

task 必填字段：

| 字段 | 内容 |
| --- | --- |
| request | 员工原始需求，非空字符串 |
| countries | 非空国家名称数组，与客户 country 用同一写法；全球用 `["全球"]` |
| products | 六类中的非空子集；蓝牙音箱先规范成蓝牙音响 |
| requirements | 本轮必需条件字符串数组，逐项由 Codex 核实 |
| allow_channel_candidates | 布尔值，默认 false；允许相邻渠道留candidate，不再允许跳过目标产品白牌条件进入main |
| customer_profile | 必填对象，含非空buyer_types字符串数组、固定brand_policy `white_label_required`、supplier_constraints对象；未知供货条件留空对象或不设置对应项 |

city、market、address、priorities、mode、target_count 可作为 task 附加字段记录。买家类型以customer_profile.buyer_types为准；脚本检查main类型匹配，但城市/市场精度、供货约束等语义仍由Codex核实。附加 `coverage` 对象可保存已读/待读入口、来源覆盖与失败原因；不要仅记录一个无依据的完成百分比。

## region_research：采集前的当地渠道预研

每项必填 `country`、`area`（国家内的适用城市/市场/范围文字）、`status`、`checked_at`、`languages`、`markets`、`communication_channels`、`ecommerce_platforms`、`source_ids`、`limitations`。checked_at为含时区ISO时间；复用历史资料保留真实旧时间和原引用。

- status为 `complete`、`partial`、`blocked`。前两者必须有非空languages字符串数组及可读原文支持的source_ids。
- markets/communication_channels/ecommerce_platforms均为数组。每项有 `name`、`source_ids`，支持对该入口的实际观察；平台可用、当地常用程度等语义不能由数组中出现名字就推定。
- markets项还需 `kind`（中文类型字符串）、`size`（large/medium/small/unknown）。已知规模另需可读原文支持的 `size_source_ids`；缺证用unknown，不猜规模。
- 任一渠道列表为空、status为partial或blocked，limitations必须写非空具体限制。blocked可languages和sources为空；可保存零结果，但不能支持该国main。
- 每名main必须有同国非blocked预研；全球任务也按实际国家保存。城市/范围是否适用由Codex实际核对，不能拿同国别的城市充当指定市场预研。
- 来源可引用本批或主库已有ID。预研会与本批任务一起保存，并在采集说明输出中文摘要、在来源表保留引用。

示例形状（此处省略真正来源对象，实际必须提供可读证据）：

```json
{"country":"测试国","area":"测试城","status":"partial","checked_at":"2026-11-13T10:00:00+08:00","languages":["测试语言"],"markets":[],"communication_channels":[{"name":"公开业务电话","source_ids":["SRC-REGION"]}],"ecommerce_platforms":[],"source_ids":["SRC-REGION"],"limitations":["虚构字段演示；市场及电商尚未调查"]}
```

## sources：字段事实的来源

每个对象必填 `source_id`、`url`、`title`、`read_at`、`level`、`excerpt`。url 为无内嵌凭证的 HTTP(S) 原文网址；read_at 为实际读取时间的 ISO 8601 字符串，含时区。excerpt 只保留支持结论的必要原文或明确标注的简要转述，不复制整页。

| level | 使用方式 |
| --- | --- |
| current_original | 本轮实际读到的原文，包括企业自己发布的商业页面 |
| cached_original | 读到完整原 URL 历史缓存；必填 limitations，说明时间与现货等限制 |
| third_party_original | 实际读到第三方目录/媒体/贸易记录原文；不伪装成商户自述 |
| search_snippet | 只看到搜索摘要，可作发现线索，不能独立支撑可开发资格 |
| failed_read | 登录墙、验证码、空壳或其他读取失败；可记录但不能被事实字段引用 |

可加 `content_date`、`limitations`、`publisher`。旧 source_id 内容不可改写；页面重读或纠正时新建来源 ID。可引用主库已有来源，不必重复上传；同 ID 再传时内容必须完全相同。

本地导入名单不等于网络已核验。保留文件名、工作表和行号等导入出处作为附加字段；没有可核 URL 的记录先作为 candidate，空 sources 可保存，不编造来源网址。已有表格主库按 persistence 更新。

## leads：主体、证据和联系

每个对象必填 `lead_id`、`name`、`country`、`status`、`reason`。status 仅为 `main`（可开发候选）、`candidate`（待核）、`excluded`（后台排除）。reason 写本轮判断原因。

推荐填写 `city`、`address`（附精度/未知说明）、`market_location`（市场名称、场内/周边及铺位精度）、`buyer_type`、`aliases`（字符串数组）、`unknowns`（待核字符串数组）。main必须有buyer_type，且精确属于customer_profile.buyer_types，并用buyer_type_source_ids提供可读依据。新主体有 `manual_notes` 空字符串；已有人工备注/负责人/阶段由运行层保留，不将研究结论写入人工栏。

- `identity_source_ids`：经营主体证据来源 ID 数组。
- `location_source_ids`：地域证据来源 ID 数组；配送范围不能证明经营地址。
- `product_evidence`：数组，每项为 `product`、`detail`、`source_ids`。product 必须为六类标准名称，detail 写具体支持内容与限制。
- `brand_profile`：white_label（独立白牌）、mixed（独立混合经营）、brand_owner（品牌方）、official_store（官方品牌店）、brand_exclusive（品牌专卖店）、unknown（待核）。main只允许前两类；明确的三种品牌排除类只能excluded。
- `white_label_evidence`：数组，每项为 `product`、`detail`、`source_ids`；main至少一个本轮产品在product_evidence和white_label_evidence中同时具有可读原文支持。不能用眼镜销售证据搭配手表白牌证据拼成合格眼镜客户。
- `contacts`：数组，每项为 `kind`、`value`、`role`、`attribution`、`source_ids`。value 始终是字符串；role 未知则写未知，attribution 解释该入口为何属于这个商户。

contact.kind支持phone、whatsapp、zalo、email、website、form、chat、facebook、instagram、telegram、tiktok、youtube、linkedin、other。value保留原字符串，电话不补位，官网仍可保存但仅作辅助。

**实际联系判断：**

- phone/email：可归属且有可读原文的真实业务电话/邮箱。格式及语义由Codex核实，脚本不是拨号/邮箱验证器。
- form：需 `action: "form"`、`action_evidence`（观察到的联系功能）、`action_source_ids`（该功能的可读原文依据）。
- WhatsApp、Zalo、社交、chat及other：需 `action: "message"`、action_evidence、action_source_ids，表明实际可发起消息的入口。普通展示主页不够；平台身份必须有明示证据。
- website永不单独满足联系门槛，也不能填action来绕过；实际网页表单另记form。
- WhatsApp仍需 `whatsapp_source_ids`、`whatsapp_evidence`；Zalo平台身份明确写进action_evidence及对应来源，不能从电话号码推定。
- `status`可选active/historical/invalid，默认active只表示本次采集使用的入口；`reachability`仅untested/unavailable，默认untested。历史、无效、已知不可用入口不支撑main。未外联不能标已可达。
- candidate可以保留还缺动作证据的社交页面。填写了action时须符合kind、说明功能；进入main仍至少有一项实际联系完整过关。

main还需主体、地域、买家类型、目标产品、目标白牌、同国有效预研及任务语义全部满足。`allow_channel_candidates: true`只允许保留拓品candidate，不能绕门槛。联系人采购权限未知可保留，但不能说已核买家意向。

## 开发优先级：本批证据推导

`capability_evidence`为可选数组，每项含kind、detail、source_ids；kind为wholesale/import/distribution/sales_capacity之一。`ongoing_activity`为可选对象，含detail、source_ids，可另有observed_on（YYYY-MM-DD，实际活动/资料日期；未公开时省略，不能用读取日期冒充）。不填时省略该对象，不用null。

main全部通过准入后：经营能力有可读原文依据，且ongoing_activity有current_original或third_party_original支持，运行层推导A；其余main为B。仅旧缓存或摘要不能支撑当前持续经营A。模型仍须核来源语义与时效，不能把陈年页面标成持续经营。

`priority`、`priority_reason`由commit生成并覆盖输入值，不手填A提高资格；非main清空优先级。本次未提供可支持A的证据，不继承旧A。近期经营依据、评级理由和来源会写入Excel，A在前B在后，并列保持稳定顺序。

所有 source_ids 字段为不重复的字符串数组，引用必须存在。strong 原文检查只验证来源的声明层级，Codex 仍必须实际读取、核对内容。

## 更新、查重和队列

同主体复用 lead_id。更新时传本轮完整资格事实、状态、理由和来源；未提供的其他原有字段继承。旧证据/别名/联系/待核问题追加保留，纠正说明写入 reason 或附加 corrections，注明旧项已解决；避免旧待核项被误读为当前结论。相同 kind+value 联系可明确标为 historical/invalid，原版本保存在 history。人工字段不能通过采集批次覆盖。

相似名称、独立域名或业务联系产生提示。潜在重复 main 必须复用正确主体 ID、留 candidate，或在确证独立时给出：

```json
{"duplicate_review":{"decision":"distinct_entities","reason":"依据说明","source_ids":["SRC-ID"],"related_lead_ids":["另一稳定ID"]}}
```

related_lead_ids 必须覆盖所有匹配对象。不要为通过校验编造独立主体依据。确认重复的旧记录后台保留并写清归并关系。

queue 每项必须有非空 `next_action`，可加 url、reason、product、region。需记录能实际继续的入口。limitations 记录读取障碍、时间限制和尚未覆盖范围。

## 输出检查

导出本批次冻结的主体版本，后续更新不改旧批次视图。新导出增加品牌经营类型、白牌经营证据、所在市场/位置精度、近期经营依据、开发优先级及理由。画像/预研以中文摘要展示，不要求员工读内部JSON。表格包含可开发客户、待核线索、来源证据、采集说明；排除记录仅计数并留主库。单元格均按文本写入，保留号码前缀，避免文本被当公式执行。脚本校验 ZIP/XML；交付前还应独立回读表头、数量与关键字段。有宿主渲染能力时检查显示。

[batch-example.json](../assets/batch-example.json) 是明确标注的虚构结构示例，不能作为真实采集结果或直接提交。零结果也能提交及导出，必须据实填写限制。


## 1.0主库与旧批次接续

旧主库仍可读取，无需批量迁移。已提交的旧批次可原样重试commit（相同payload），可导出但标明历史规则、未按1.1重新核验、不自动赋A/B；这不会修改旧主库事实或人工备注。

新的提交必须schema_version 2。旧未提交包不能仅改版本号：读取最新主库，snapshot另建批次，按新画像与预研补齐目标白牌、实际联系和角色依据。原客户复用稳定ID，保留历史来源和人工字段；品牌不符或缺证时按新事实降为excluded/candidate。资格下降不删除历史记录。已有品牌排除类不能通过省略brand_profile改回candidate；发现旧分类错误时，明确提交新分类及对应真实证据，旧值保留history。
