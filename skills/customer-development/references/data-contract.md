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

必填：`schema_version: 1`、`batch_id`、`base_sha256`、`task`、`sources`、`leads`、`queue`、`limitations`。后四项为数组，允许空；limitations 为字符串数组。

batch_id 为 1–80 位 ASCII 字母、数字、下划线或短横线，以字母或数字开头。客户及来源 ID 还允许点和冒号。ID 稳定且不复用给其他主体。base_sha256 必须保留 snapshot 返回值。

task 必填字段：

| 字段 | 内容 |
| --- | --- |
| request | 员工原始需求，非空字符串 |
| countries | 非空国家名称数组，与客户 country 用同一写法；全球用 `["全球"]` |
| products | 六类中的非空子集；蓝牙音箱先规范成蓝牙音响 |
| requirements | 本轮必需条件字符串数组，逐项由 Codex 核实 |
| allow_channel_candidates | 布尔值，默认 false；只在本轮或适用地区规则明确允许渠道例外时设 true |

city、market、address、buyer_types、priorities、mode、target_count 可作为 task 附加字段记录。脚本不自动执行这些语义筛选。附加 `coverage` 对象可保存已读/待读入口、来源覆盖与失败原因；不要仅记录一个无依据的完成百分比。

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

推荐填写 `city`、`address`（附精度/未知说明）、`buyer_type`、`aliases`（字符串数组）、`unknowns`（待核字符串数组）。新主体有 `manual_notes` 空字符串；已有人工备注/负责人/阶段由运行层保留，不将研究结论写入人工栏。

- `identity_source_ids`：经营主体证据来源 ID 数组。
- `location_source_ids`：地域证据来源 ID 数组；配送范围不能证明经营地址。
- `product_evidence`：数组，每项为 `product`、`detail`、`source_ids`。product 必须为六类标准名称，detail 写具体支持内容与限制。
- `contacts`：数组，每项为 `kind`、`value`、`role`、`attribution`、`source_ids`。value 始终是字符串；role 未知则写未知，attribution 解释该入口为何属于这个商户。

contact.kind 支持 phone、whatsapp、email、website、form、facebook、instagram、telegram、tiktok、youtube、linkedin、other。Zalo 等使用 other，并在 value/role 明确平台，仍需显式平台证据。公开商户表单或官方社交入口可用，不要求必须找到电话号码。

WhatsApp 另需 `whatsapp_source_ids` 和 `whatsapp_evidence`（明确平台文字或公开链接）。普通电话不能凭号码推定 WhatsApp。`status` 可选 active/historical/invalid，默认 active 只表示本次采集使用的入口；`reachability` 仅 untested/unavailable，默认 untested，不能因看到号码就写已可达。历史、无效、已知不可用联系不能单独支撑 main。

main 需要主体、地域、至少一项本轮产品及至少一个可归属入口有可读原文支持，并满足任务语义条件。允许的渠道例外需 `task.allow_channel_candidates: true`，以及 lead 的 `channel_fit`、`channel_source_ids`，明确未核具体产品。candidate 可以保存缺字段线索，不降低 main 门槛。

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

导出本批次冻结的主体版本，后续更新不改旧批次视图。表格包含可开发客户、待核线索、来源证据、采集说明；排除记录仅计数并留主库。单元格均按文本写入，保留号码前缀，避免文本被当公式执行。脚本校验 ZIP/XML；交付前还应独立回读表头、数量与关键字段。有宿主渲染能力时检查显示。

[batch-example.json](../assets/batch-example.json) 是明确标注的虚构结构示例，不能作为真实采集结果或直接提交。零结果也能提交及导出，必须据实填写限制。
