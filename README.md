# 数据管道

本仓库负责数据源目录、搜索索引、Reader 资源转换与发布。`scripts/` 包含实际处理代码，`.github/workflows/` 包含运行入口，`tests/` 为离线回归，`config/` 和 `state/` 存储配置及上次状态。

## 当前状态

- 本地已移除本项目的文档 OCR 生产、校订、OCR 搜索发布，以及其前置 PDF/DjVu 转页面流和页面流写入入口；旧 page manifest 只作为历史引用读取。BHA 上游仍提供名为 `ocr_cache` / `ocr_patch` 的历史文本分支，解析现有 BHA 数据仍使用它们；该上游解析依赖尚未替换。浏览器直接阅读 PDF/DjVu 原件、已有派生 PDF，以及不渲染图片的文件健康检查不属于 OCR 页面流。
- 旧 OCR 数据、桶对象、部分元数据及两个搜索站的历史阅读代码仍存在，本次没有撤下线上 OCR 数据或完成全站下线。Reader/GC 对旧引用的读取用于避免误把其他资源判为可删；已移除的转换代码可从 Git 历史查阅。
- 原先用于 BHA OCR 补丁的冲突审核和阻塞检查已删除；后续解析以所选来源版本及补丁为准，不再阻塞等待旧审核流程，结果可能与过去的冲突处理不同。
- `pdf-account-worker.yml` 仅是现有桶维护检查识别历史工作流库存的无执行哨兵；不得把文件存在当作外部写者已隔离。
- 截至 2026-11-06，仓库 Actions 已恢复：搜索日常同步、目录测试、Space 保活、Space 契约测试和 Reader v3 手动发布器；其他入口仍需逐项审查。Reader v3 自动退役计划已取消；历史清理工作流仍可能写入或删除数据，不得因测试通过就启用定时 GC。

## 资源查询与下线预览

`scripts/bucket_file.py` **仅查询**一个精确桶文件；之前的直删入口已经撤销，不再单独设置查询工作流。通常使用 `HF_TOKEN`，其他凭据可通过 `--token-env HF_INPUT_TOKEN` 指定。

`scripts/reader_route_plan.py` 可按源仓库和精确路径，预览移除单条 Reader 路由的影响：对比 canonical、v3 pinned base、合并投影，列出相同直接对象的其他直接引用；可选择读取当前格式源索引以寻找可能重新生成该路由的记录。它不列全桶、不更新线上索引、不删除对象。读取几十个源索引可能需要时间。

```sh
python3 scripts/bucket_file.py 'vomebook/reader-assets-v2' 'some/exact/file'
python3 scripts/reader_route_plan.py --repo 'VoiceOfML/SomeRepo' --path '目录/一本书.pdf' --scan-sources
```

**预览结果不是可删除证明。** 报告里的 `expected_projection` 是本地计算值，并未访问或确认部署中的两个站点。它不检查嵌套 manifest、历史代、搜索索引、站点缓存或外部引用；源索引检查也只是某个时间点、不是原子快照。

## 撤回或恢复单条派生 Reader 路由

`reader-v3-publish.yml` 现有手动入口新增 `remove-route`、`restore-route`、`verify-route`：先用 `inspect` 获取当前 `pointer.generation`，在 `resource_json` 输入 `{"repo":"VoiceOfML/SomeRepo","path":"目录/一本书.pdf"}`，把该代填入 `expected_parent`。先 `remove-route` 且 `apply=false` 查看结果，再以相同代和 `apply=true` 应用。精确源路径在工作流输出/artifact 中可见。仓库和路径改变时需要重新检查代；不要用通配符。

撤回会建立过滤后的 v3 基础快照、删除该书的 v3 覆盖，并在 v3 catalog 保留此源的抑制状态。后续现行 `unify` 会继续过滤它；不会改动其他书的路由。工作流通过现有中央锁发布固定版本到 Pages，再最多等待 20 分钟并只读核对两站是否提供同一代的完整投影及目标条目状态；此操作**不写 GC 回执，也不授权清理对象**。`verify-route` 可用当前代重试只读核验；如果更新已成功但后续发布或网络失败，可用当前代重跑同一个 `remove-route` 或 `restore-route` 以继续发布，不要误认为已完成。`restore-route` 移除抑制并使用**当前 canonical 路由**，不会自动恢复被移除的旧 v3 覆盖。

**范围限制：**这只撤回派生 Reader sidecar 路由，不删除搜索记录、源文件、桶对象、canonical 格式索引，也不关闭源文件直接阅读或下载；PDF/DjVu 等仍可能从原数据集回退打开。已打开的页面和缓存也可能继续使用旧路由。对象可能被其他书、历史代或嵌套 manifest 共用，因此本流程不执行物理删除，也不能证明可删。旧版本或外部写者不一定遵守抑制状态；上线前须部署全部相关代码并核对实际写者。旧 `withdraw` 仍只撤 v3 覆盖，并非 `remove-route`。

这实现了索引路由撤回与两站核验，但**快速且可验证的对象清理仍未实现**；旧 GC 未因这些功能被放行。

## PDF 页面桶：derived → objects

`python3 scripts/reader_page_audit.py --output output/page-audit.json` 只读扫描 `vomebook/pdf-pages-v2` 的 `derived/` 和 `objects/`，按源 SHA、Xet 内容 hash、长度及目标 manifest 的精确书籍身份匹配；同内容不同书名必须分别匹配，无法唯一匹配的条目不会迁移。报告含 `index_sha256`、`canonical_sidecar_sha256`、`v3_generation`、逐条映射、未解决项，以及未被当前索引分类的文件；报告**不授权删除**。快照不是原子的，不能作为外部写者已停的证据。

已部署的 `reader-v3-publish.yml` 提供 `migrate-derived`：需要在 `resource_json` 中填写上述三个身份，字段名分别为 `index_sha256`、`sidecar_sha256`、`v3_generation`。先 `apply=false`；复核后 `apply=true` 还必须提供 `"external_writers_stopped":true`。每次重新扫描并检查对象、manifest、旧索引及 v3 代。该操作只把当前 canonical 派生 PDF 索引和 sidecar 路由改到已存在的精确 `objects/` 副本，保留旧对象和旧代；之后在发布前再次核实隔离，再发布新投影并等待两站确认。不能混同旧 `withdraw` 或将报告当成删除清单。

迁移首次改索引前会将内容寻址的续跑计划保存在 `reader-index/gc/page-migration/`，日志打印 `transaction_id`；完成后也写入工作流报告。若中断，使用 `resume-derived`、`apply=true` 和 `{"transaction_id":"日志中的值","external_writers_stopped":true}`：每个索引只能处于该计划的精确旧值或新值，偏离则停止。若事务计划写入尚未返回，先在桶中只读核对其 SHA，勿猜测。完成后仍须核对两站。

实际写入及续跑在各写入边界重新检查原有全部配置仓库、以及历史盘点中额外记录仓库的 GitHub 工作流：除当前发布工作流的当前 run 外均须停用并排空，外部仓库还须停用 Actions；检查失败、不可访问、404 或缺少凭据均停止。这是当前部署版本的旧维护 fence，**不代表已退场的 OCR 生产者在新架构中仍应运行，也不应要求用户重新创建旧账户**。后续应在确认实际活跃写者、撤销或收口旧桶写凭据之后，用面向真实写者的隔离替换它；在此之前旧 fence 仍会阻止 apply。仅 Actions 状态或 `external_writers_stopped=true` **不能证明**其他桶凭据已失效；Hub 无原子 CAS，必须在整个迁移和恢复期间协调其他写者停写。站点及对象的旧 URL 缓存、历史代、其他域引用不由迁移解决。

`python3 scripts/reader_page_cleanup.py --graph-report output/gc/reader-gc-report.json --output output/page-cleanup.json` 可以从现有跨桶图的完整报告中分出 page 桶的 `derived/` 和 `objects/` 候选；缺桶或图不完整不输出候选。输出仍只是待审核报告，不能直接删除。

原手动 `reader-gc-collect.yml` 提供 `scope=page-plan` / `scope=page-apply` 两步，在现有锁内直接执行：在已隔离并排空生产者的维护窗口，以 `resume_run={"paths":["derived/精确路径"]}` 生成计划，审阅 `page-exact-plan` artifact 和日志 SHA；随后设置 `apply=true`，并通过 `resume_run={"plan_run":"运行ID","plan_sha256":"计划SHA","external_writers_stopped":true}` 应用。应用会重新构建四桶图、核验两站及对象版本、重新验证所遍历的嵌套清单版本、要求额外确认外部写者已停，再记录 intent 并发送一次精确批次；部分成功或响应丢失时只可重新盘点，不会自动重试。本地计划有效期改为 24 小时、artifact 保留两天，一次最多 1000 个键；这只延长审核期限，不延长引用、消费者或对象版本证明，应用时仍须重新扫描和复核。长时间扫描超过期限仍会失败，尚未实测真实桶上的耗时。它仍复用旧 CHM 事务检查及生产者 fence；若任何证明不足便停止，不能仅靠勾选外部确认绕过。旧 GC 尚未整体简化。

page 迁移和精确删除代码已部署；Reader 发布器已启用并完成只读 `inspect`，迁移 apply 仍被未核实的外部写者与历史仓库权限阻塞，page 精确删除所在 collector 仍禁用。单条路由下线、PDF/DjVu 页面流生产代码移除和其他大规模 OCR 清理变更仍只在本地。历史 CHM 固定批次的 19 个工作流入口在本地已移除，但尚未部署，旧全桶 GC 及其阻塞也未整体重写；详见 `.github/WORKFLOWS.md`。

## 本地检查

```sh
python3 -m pip install -r scripts/requirements.txt PyYAML==6.0.3
python3 -B -m unittest discover -s tests -p 'test_*.py' -q
```

部分 Reader 转换测试需要系统工具，例如 `7z`、DjVuLibre 和 LibreOffice；缺少时部分测试会跳过。工作流说明见 `.github/WORKFLOWS.md`。搜索目前仍由 `search-daily-sync.yml` 生成数据并写入两个站点的 Git 仓库，数据与代码解耦是后续任务；本次 OCR 删除并未解决 Git 数据历史增长。
