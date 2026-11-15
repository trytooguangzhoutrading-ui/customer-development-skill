# 保存、断点与纠错 · 1.5

## 唯一主库

先按[project-context.md](project-context.md)定位项目绑定及有效规则。新工作区使用本地JSON主库，Excel是交付视图。已有Excel主表继续作为权威入口，不为迁就脚本初始化第二个主库。安装器只更新技能，不迁移真实客户，也不覆盖已有公司配置。主管确认公司产品重点变化后，Codex读取原配置、更新相应默认产品字段并保留其他设置；新任务采用新配置，旧冻结批次不重写。产品范围已变化的采集应建立新任务，继续原任务仍保留其原范围。当前市场内研究可并行，一个主库只允许一个写入者。所有市场Excel都是同一主库的交付视图，不能按市场初始化多个主库。

新工作区目录：

- config.json：公司默认主线products、附带supplementary_products、买家类型、地区、供货条件和默认政策。新任务按显式需求覆盖默认；未知价格/MOQ留未知。
- execution/master.json：来源、主体、独立任务资格、问题、人工字段及冻结批次。
- execution/backups/：写入前以内容hash保存的准确主库备份。
- research/<ID>/snapshot.json：研究开始前保存的主库基线。
- research/<ID>/batch.json及断点：当前研究，不等于已正式入库。
- execution/task-index.json：活动任务、交付索引；execution/channel-registry.json：实际工具及渠道可用性观察。
- research/<ID>/research-log.json：本轮计划入口、尝试、分页/游标、固定起始时间、停止原因及接续历史；不是后台调度。
- outputs/：统一单工作表客户Excel及用户明确需要的纠错模板；不作为第二套JSON主库。

init仅用于全新目录，拒绝覆盖。换电脑需另行传递已授权工作区，安装Skill不会共享客户或账号。

## 新任务与恢复

以下任务CLI用于JSON主库；已有Excel走下文及project-context.md中的Excel分支。先snapshot再联网。workflow snapshot自动继承config；`--overrides <TASK.json>`提供本轮任务明确变化，不能把上轮地区静默当所有新任务的地区。“继续”先status/continue读取持久活动任务；resume仍可用于明确批次草稿恢复。不能只凭最新文件名混入另一地区。城市、市场或地址的显式改变会同步规范geo_scope，禁止显示新地址却按旧范围准入。

```text
python3 <SKILL_DIR>/scripts/leadkit_workflow.py snapshot --workspace <PROJECT> --batch-id <ID> --overrides <TASK.json>
python3 <SKILL_DIR>/scripts/leadkit_workflow.py checkpoint --workspace <PROJECT> --batch <BATCH.json>
python3 <SKILL_DIR>/scripts/leadkit_workflow.py resume --workspace <PROJECT> --batch-id <ID>
python3 <SKILL_DIR>/scripts/leadkit_workflow.py status --workspace <PROJECT>
python3 <SKILL_DIR>/scripts/leadkit_workflow.py continue --workspace <PROJECT>
```

checkpoint允许任务尚未完成，保存实际候选、来源、预研、队列和覆盖；不谎称已通过commit。阶段有成果就保存，不等整批结束。恢复检查断点完整性、基线和当前主库；有冲突则在最新主库重做合并，另建批次，不能只改旧hash。未变化的失败路径不无限重试。

未提交任务的continue恢复同一草稿及固定预算；已提交的任务建立新接续批，沿用任务范围、未读/受阻入口、已读历史与未结问题。数量目标未满时继续剩余目标；已达标后员工再说继续，按原目标启动下一轮。coverage模式中的数量仅是阶段观察值，继续未完路线，不能以达到该值关闭城市任务。换地区或产品是新任务，并仍对历史查重。多任务且没有明确活动指针时返回候选任务，不能猜。导出后用workflow record-delivery登记实际交付文件。

专用客户工作区可运行`leadkit_workflow.py setup-project --workspace <PROJECT> --dedicated-workspace`。仅增量写入该目录AGENTS.md中的受管客户采集规则；先备份原文，保留其他规则，重复执行幂等。不修改全局配置，不在不明用途项目擅自加规则。员工安装后的项目设置需求即可授权此可逆设置。

## 逐条预检与原子提交

先用prepare校验共享任务、来源和预研，再分离可提交客户与有错误的记录。错误原件、原因与修复事项保留；输出结果明确接受/拒绝数量及文件路径。重复关系与合并风险也应在提交前处理。共享头部无效时整批阻断，不把零客户包冒充成功。prepare按相关记录组合处理，不由行顺序决定合并成败；指向既有主库客户的合法queue关闭/更新，即使本批无该客户行也须保留。

```text
python3 <SKILL_DIR>/scripts/leadkit_workflow.py prepare --workspace <PROJECT> --batch <BATCH.json> --output-dir <PREPARED_DIR>
python3 <SKILL_DIR>/scripts/leadkit.py commit --workspace <PROJECT> --batch <prepare返回的可提交文件>
```

使用prepare返回的真实路径。单条错误隔离不等于被排除客户已核实；留修复队列，最终说明尚未纳入的数量。修复后新建批次复用客户ID，不能重复增加主体。

有执行账本时先prepare，再对有效子集research finish，最后commit；结束依据及修订见[research-progress.md](research-progress.md)。commit仍是完整事务：持锁→核基线→校验引用、资格和合并→保存精确备份→原子替换→回读。相同批次同payload重试返回原结果，相同ID不同内容拒绝。导出失败仅重试导出，不重新研究/提交。导出使用新文件名，不覆盖已交付版本。

## Excel纠错回流

员工输入“这些客户的联系失效了”“请合并我的备注”时，Codex负责导入。新JSON主库先生成专用模板；不让员工改内部JSON或资格计算字段。

```text
python3 <SKILL_DIR>/scripts/leadkit_workflow.py correction-template --workspace <PROJECT> --output <CORRECTIONS.xlsx>
python3 <SKILL_DIR>/scripts/leadkit_workflow.py corrections-preview --workspace <PROJECT> --input <FILLED.xlsx>
python3 <SKILL_DIR>/scripts/leadkit_workflow.py corrections-apply --workspace <PROJECT> --input <FILLED.xlsx> --preview-id <PREVIEW_ID>
```

模板自带稳定客户/联系编号和版本信息。模板列为客户编号、商户名称（只读）、联系入口编号（只读）、人工备注、需复核事项、失效联系入口编号、重新采集。员工只改后四列；失效项填入口编号，多项用换行或逗号；重新采集填“是”或留空。保留所有行、编号及模板元信息。Codex先preview核差异和版本，再将返回的preview_id用于apply；修改过文件后须重新preview。用户已要求合并时无需逐行重复批准。文件校验、版本冲突或不支持内容不能被“强制覆盖”绕开。

- 备注只更新人工栏，不制造网络证据。
- 联系失效按contact_id更新，不因空格格式不同创建另一入口。涉及当前资格时重新评估；不改写已交付冻结批次。
- 分类纠正先建立待核问题，必要时将相关当前资格降为待核，补证后再判断；不能用一个“合格”文本无证据晋级。
- 只处理白名单字段；公式、错误编号和被篡改模板应拒绝。原始记录、差异、主库备份及导入来源可追溯；相同纠错可幂等重试。
- 普通结果表不会自动监控。员工已经改了普通表时，Codex先读取、按稳定ID识别其真实修改，再转换到受支持纠错流程，不能按列位置覆盖整行。

该功能只用于采集纠错。负责人、报价、订单、销售阶段等不自动修改。

纠错后应交付当前范围表。可在corrections-apply附`--output <CURRENT.xlsx> --batch-id <范围对应批次>`，或另行执行`leadkit.py export-current --workspace <PROJECT> --batch-id <ID> --output <CURRENT.xlsx>`；也可使用明确scope-id。旧批次export仍是原交付快照，不能把它当作纠错后的最新表。

## 已有Excel主库

此分支由Codex使用表格工具和独立Excel任务索引执行；JSON的snapshot/status/continue/commit不会直接接收Excel。按[项目入口](project-context.md)登记活动任务和日志，多任务无指针不能按最近文件猜。

先读真实表头、稳定ID、来源和人工字段，建立映射与只读查重索引。历史JSON、摘要和旧报告不替代当前原件。

通过当前可用表格工具按ID准备字段级旧值/新值/证据/理由。保存前重读hash和涉及人工字段；变化则以最新版合并。保留公式、格式、注释、来源、原值解释及人工跟进，不硬编码某地区的列号。另存备份，写临时版、校验后替换并回读。

另一写入者占用时交付独立增量表及待合并说明，不无限等锁或覆盖。原件不可用时明确未更新主库，不猜行号或重建旧客户。专用JSON工作区纠错命令不直接套在任意历史Excel主表上。

## 问题、资格和覆盖

实体、门店、平台账号和品牌分开。同名/共号/同域只提示关系，确认同主体才建canonical关系；合并保留旧ID可追溯。去重包含所有历史状态，不因本次不匹配就从历史索引消失。

任务资格单独存；主库最新任务结论不能当全局客户状态。旧批次保持原评估时点，当前开发使用新任务资格及未结问题。问题按issue_id关闭、受阻或接续，解决证据保留在历史，不继续展示为未解决。

每批存产品×渠道覆盖、真实尝试、发现归因与限制。分别统计新增主体、更新、重新符合资格；新增来源或电话不等于新增客户。任务完成不代表市场穷尽，队列存在不表示定时执行。只有用户另行要求周期任务时使用宿主自动化。

## 逐市场接续与新旧政策

新默认按market_sequence保存国家、城市、市场目录和本批固定的batch_market_id。未完成、暂缓或受阻时继续同一市场；只有真实完成后，新接续批才进入下一市场，同一批不混入第二市场的正式客户。同城可沿用仍有效的预研，跨城市先更新对应渠道和联系生态。目录未核完整时保留限制，不能把已列城市／市场说成全部。

只交付一份Excel中的一个客户总表，以字段筛选独立商户、品牌店和待核线索；证据与说明同排保留，当前市场进度置于表头。当前市场未完成仍可交阶段表，但状态保持未完成。市场归属须有证据，未知位置进入待定位／待核，不从任务市场自动填入客户。

当前政策independent_product不以白牌为准入：独立客户经营主营并通过其他门槛即可；官方品牌店和专卖店同表分类，纯品牌方排除。旧white_label_required任务继续原门槛，冻结历史不改写；用户明确采用新政策时新建任务并沿用同一主库查重。历史品牌排除对象重入新任务须记录旧决定和新政策／身份核验依据，不凭改字段消除旧记录。
