# 项目入口、统一配置与历史库

每个项目只绑定一个权威主库。Skill安装只提供方法和脚本；客户、账号及公司配置不随安装自动同步。已有目录先检查项目及祖先目录绑定，然后实际阅读主库与补充资料，不能从“新聊天”推断“新客户库”。

## 先检查

```text
python3 <SKILL_DIR>/scripts/leadkit_project.py inspect --project <当前项目目录>
```

有绑定时返回project_root、master、company_config、active_rules、additional_sources及缺失情况。JSON使用返回的workspace；Excel沿用返回的工作簿，不调用JSON的init/snapshot。缺失源先恢复，或报告缺少的历史范围；不能宣称企业净新增。配置或规则hash变化时读取真实变更，新任务记录当前版本，旧批次不改写。日常主库新增内容不要求重新绑定。

没有绑定：定位真实历史文件、公司默认配置及当前有效规则。明确历史权威文件后bind；只有确实全新且没有历史的工作区才用leadkit.py init（自动建立绑定）。历史范围不清时先查项目文件，必要时集中确认，不能随意挑最近的文件为主库。

## 首次绑定已有项目

```text
python3 <SKILL_DIR>/scripts/leadkit_project.py bind --project <项目根> --master <主库相对路径> --kind xlsx --config <公司配置相对路径.json> --rules <有效规则相对路径.md> --rules-version <规则版本> --additional-sources <补充来源清单.json>
```

JSON主库使用kind=json且路径为某工作区的execution/master.json。公司配置是JSON对象；字段参照assets/task-defaults.json，不用覆盖真实公司设定。安装器不会替换已有配置；主管明确变更产品重点后，由Codex保留其他设置，只更新products与supplementary_products等已授权字段，新任务记录新配置hash，旧批次保持冻结。所有路径相对项目根；只读补充源可使用../其他项目/…，主库及有效规则必须在本项目中。补充JSON可为普通历史对象、批次或记录数组，不要求主库结构；只登记为阅读/查重来源，不自动导入。无补充源时省略--additional-sources。清单例：

```json
[{"kind":"xlsx","path":"history/其他已采客户.xlsx","label":"历史补充查重"}]
```

绑定写入.customer-development-project.json，不复制或合并客户。补充来源必须实际读取、按主体匹配；不相加不同表的行数，不以路径已登记代替完成查重。JSON导出新增数字只表示相对当前JSON主库，不自动证明企业净新增。

更换主库/规则绑定用相同命令加--replace --reason <真实变更理由>，保留绑定历史；先确认迁移结果，不能先改绑定再丢掉原库。共享项目时一并传递已授权配置、有效规则、主库及补充源；换电脑后inspect验证路径。

## 自然语言调用

专用客户项目可运行：

```text
python3 <SKILL_DIR>/scripts/leadkit_workflow.py setup-project --workspace <项目根> --dedicated-workspace
```

此命令对JSON和已绑定Excel项目均有效，仅增量写入项目AGENTS.md的受管块并保留备份，不改全局设置。之后员工在同项目说“采集胡志明市的蓝牙耳机批发商”或“继续上一批”即可。需要宿主已加载安装的技能；安装完按宿主提示开启新对话。

## Excel主库的执行分支

已有Excel按persistence.md读取实际字段、查重、保留人工内容并增量处理。JSON提交/任务CLI不直接处理任意Excel；不能为使CLI可运行而在项目下另建空JSON库。

在项目research/<任务ID>/保存独立task.json及research-log.json，记录本次请求、规则/配置/主表和补充源hash、原稳定ID映射、六类渠道地图与联系生态、国家／城市／市场目录、当前市场及其已读边界、规模及顺序、每次真实查询/读取、商户来源、查重关系、未读入口、准入缺口、停止原因与交付位置。它们是研究记录而非第二份客户主库。沿用research-progress.md相同事实标准，Excel分支由Codex逐项检查，不声称已通过仅适用于JSON的程序闸门。

实际更新先备份、核原件hash及人工栏，另写临时结果并回读；只能在合并成功后说主表已更新。不能安全合并则交付有稳定ID的增量表及待合并说明。Excel任务另外维护execution/excel-task-index.json：schema_version=1、backend="xlsx"、active_task_id以及tasks字典；每个任务索引含task_path、research_log_path、state、deliveries，路径相对项目根。任务文件记录task_id及上述请求、范围、hash、预算和时间，日志保存实际入口与尝试。它是Codex维护的Excel接续记录，不是JSON主库，也不宣称可使用JSON工作流CLI。

新窗口先按active_task_id读取同一task.json和research-log.json，保留已开始时间及已用预算。另对比当前公司products/supplementary_products与任务已保存范围；说明差异而不静默改任务。用户已要求采用新主营定位时另建任务，原任务保留。旧Excel项目还没有索引时扫描真实未完成研究；只有唯一任务才自动关联，多任务且无明确活动指针时集中确认，不能按修改时间挑最近任务。更新索引保留旧任务；继续真实未读入口，不清空研究。

## 当前配置与已保存任务的差异

JSON的workflow status/continue会返回configuration_review，列出当前默认与任务主线、附带范围差异及配置/绑定/运行规则版本。差异不是自动迁移指令：仅继续原任务仍用原范围；明确采用新公司定位时用当前默认snapshot建立新任务并明确地区，沿用历史去重及适用的原始证据。不要把升级安装当作其他项目配置已经同步，也不要让同一次继续偷偷新增产品。

## 当前市场与交付位置

Excel分支也维护国家→城市／地区→批发市场／批发城→商户目录，并在任务和日志中保存当前市场、顺序、完整性限制、未读入口和受阻条件。市场归属用实际证据核实；不能把同城或送货范围自动填成场内。未完成前不自动推进第二市场；预算结束保留当前市场，完成后下一批才推进。同城预研可按真实日期沿用，跨城重新核当地渠道。

每市场独立导出Excel及地区／市场进度总览，仍共用当前唯一主库和全部补充查重源。品牌店放独立工作表，白牌为已核／待核标记和实际结果，不改变客户ID。当前规则采用independent_product；旧white_label_required任务保留原政策，用户要求采用新规则时建立新任务，不自动重写历史客户资格。
