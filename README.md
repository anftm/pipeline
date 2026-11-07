# 数据管道

本仓库负责数据源目录、搜索索引、Reader 资源转换与发布。`scripts/` 包含实际处理代码，`.github/workflows/` 包含运行入口，`tests/` 为离线回归，`config/` 和 `state/` 存储配置及上次状态。

## 当前状态

- Pipeline 已移除本项目的文档 OCR 生产、校订、OCR 搜索发布，以及其前置 PDF/DjVu 转页面流和页面流写入入口；旧 page manifest 只作为历史引用读取。BHA 上游仍提供名为 `ocr_cache` / `ocr_patch` 的历史文本分支，解析现有 BHA 数据仍使用它们；该上游解析依赖尚未替换。浏览器直接阅读 PDF/DjVu 原件、已有派生 PDF，以及不渲染图片的文件健康检查不属于 OCR 页面流。
- 旧 OCR 数据、桶对象、部分元数据及两个搜索站的历史阅读代码仍存在，本次没有撤下线上 OCR 数据或完成全站下线。Reader/GC 对旧引用的读取用于避免误把其他资源判为可删；已移除的转换代码可从 Git 历史查阅。
- 原先用于 BHA OCR 补丁的冲突审核和阻塞检查已删除；后续解析以所选来源版本及补丁为准，不再阻塞等待旧审核流程，结果可能与过去的冲突处理不同。
- `pdf-account-worker.yml` 仅是现有桶维护检查识别历史工作流库存的无执行哨兵；不得把文件存在当作外部写者已隔离。
- 截至 2026-11-06，仓库 Actions 已恢复：搜索日常同步、目录测试、Space 保活、Space 契约测试和 Reader v3 手动发布器；其他入口仍需逐项审查。Reader v3 自动退役计划已取消；历史清理工作流仍可能写入或删除数据，不得因测试通过就启用定时 GC。

## 资源查询与下线预览

`scripts/bucket_file.py` **仅查询**一个精确桶文件；之前的直删入口已经撤销，不再单独设置查询工作流。通常使用 `HF_TOKEN`，其他凭据可通过 `--token-env HF_INPUT_TOKEN` 指定。

`scripts/reader_route_plan.py` 可按源仓库和精确路径，预览移除单条 Reader 路由的影响：对比 canonical、v3 pinned base、合并投影，列出相同直接对象的其他直接引用；可选择读取当前格式源索引以寻找可能重新生成该路由的记录。它不列全桶、不更新线上索引、不删除对象。读取几十个源索引可能需要时间。

```sh
python3 scripts/bucket_file.py 'vomebook/reader-assets-v2' 'some/exact/file'
python3 scripts/reader_exact_references.py 'vomebook/pdf-pages-v2' 'derived/精确路径/document.pdf'
python3 scripts/reader_route_plan.py --repo 'VoiceOfML/SomeRepo' --path '目录/一本书.pdf' --scan-sources
```

**预览结果不是可删除证明。** `reader_exact_references.py` 只读取精确对象与六个主要索引层，解释 canonical、当前投影、redirect、PDF 索引及 legacy catalog 各代的直接引用；2026-11-07 实测单路径约 16 秒。它不扫描嵌套 manifest、未知根、缓存或外部链接，不授权删除。`reader_route_plan.py` 的 `expected_projection` 是本地计算值，并未访问或确认部署中的两个站点；其源索引检查也只是某个时间点、不是原子快照。

## 撤回或恢复单条派生 Reader 路由

`reader-v3-publish.yml` 现有手动入口新增 `remove-route`、`restore-route`、`verify-route`：先用 `inspect` 获取当前 `pointer.generation`，在 `resource_json` 输入 `{"repo":"VoiceOfML/SomeRepo","path":"目录/一本书.pdf"}`，把该代填入 `expected_parent`。先 `remove-route` 且 `apply=false` 查看结果，再以相同代和 `apply=true` 应用。精确源路径在工作流输出/artifact 中可见。仓库和路径改变时需要重新检查代；不要用通配符。

撤回会建立过滤后的 v3 基础快照、删除该书的 v3 覆盖，并在 v3 catalog 保留此源的抑制状态。后续现行 `unify` 会继续过滤它；不会改动其他书的路由。工作流通过现有中央锁发布固定版本到 Pages，再最多等待 20 分钟并只读核对两站是否提供同一代的完整投影及目标条目状态；此操作**不写 GC 回执，也不授权清理对象**。`verify-route` 可用当前代重试只读核验；如果更新已成功但后续发布或网络失败，可用当前代重跑同一个 `remove-route` 或 `restore-route` 以继续发布，不要误认为已完成。`restore-route` 移除抑制并使用**当前 canonical 路由**，不会自动恢复被移除的旧 v3 覆盖。

**范围限制：**这只撤回派生 Reader sidecar 路由，不删除搜索记录、源文件、桶对象、canonical 格式索引，也不关闭源文件直接阅读或下载；PDF/DjVu 等仍可能从原数据集回退打开。已打开的页面和缓存也可能继续使用旧路由。对象可能被其他书、历史代或嵌套 manifest 共用，因此本流程不执行物理删除，也不能证明可删。旧版本或外部写者不一定遵守抑制状态；上线前须部署全部相关代码并核对实际写者。旧 `withdraw` 仍只撤 v3 覆盖，并非 `remove-route`。

这实现了索引路由撤回与两站核验，但**快速且可验证的对象清理仍未实现**；旧 GC 未因这些功能被放行。

## PDF 页面桶：derived → objects

`python3 scripts/reader_page_audit.py --respect-active-redirects --output output/page-audit.json` 只读扫描 `vomebook/pdf-pages-v2` 的 `derived/` 和 `objects/`，按源 SHA、Xet 内容 hash、长度及目标 manifest 的精确书籍身份匹配；同内容不同书名必须分别匹配，无法唯一匹配的条目不会迁移。该选项会把当前 v3 redirect 中的旧文件单独计入 `legacy_pdfs_in_active_redirects`；不加此选项时，“未被当前 PDF 索引使用”并不代表没有 redirect 或其他引用。报告含 `index_sha256`、`canonical_sidecar_sha256`、`v3_generation`、逐条映射、未解决项，以及未被当前索引分类的文件；报告**不授权删除**。快照不是原子的，不能作为外部写者已停的证据。

优先使用 `reader-v3-publish.yml` 的 `redirect-derived`：在 `resource_json` 填写只读审计取得的 `index_sha256`、`sidecar_sha256`、`v3_generation`，先 `apply=false` 检查，再以同一身份 `apply=true`。它重新扫描并核对目标文件及 manifest，把全部已匹配路由组成新的内容寻址 v3 快照，仅通过 S3 条件写推进**一个 v3 指针**；不改 canonical 派生 PDF 索引、canonical sidecar、旧 catalog，也不删对象或旧代。随后发布 Pages 投影并核对两站；报告不是删除清单。

已知中央 Reader 发布器使用 `reader-sidecar` 并发组；搜索同步写搜索数据，不写 Reader 桶索引。单指针路径避免了依赖旧 OCR 仓库 fence 的多对象索引事务，但不证明其他 HF/S3 凭据已失效，也不保证未来无外部写者。发布前后复核副本和指针，保留全部旧数据；并发者或目标文件异常仍会造成冲突或发布后失败，需要停止并检查。redirect 活跃时，工作流的 `unify`、`remove-route`、`restore-route` 会重新审计原索引、副本、manifest 和 legacy catalog，再以单个条件指针发布；未经审计的内部 `unify` 仍拒绝。canonical sidecar handoff 在任何写入前先做相同的 redirect 审计，再交由中央发布器重新审计并刷新。`reset-documents` 与 GBK 修复仍在写入前阻止，直到分别实现兼容流程。

`redirect-derived` 先创建内容寻址快照、catalog 和 promotion，再打印 staged generation，最后只发一次指针 `If-Match`；条件冲突或响应丢失不可盲目重试。查看 `inspect` 确认指针：若仍是旧代，可重新只读审计和预演；若已是 staged generation，用 `publish-generation`、`apply=true`、`expected_parent=该代` 仅重新投影并核对两站；若是其他代则停止调查。此恢复入口不会修改桶索引或写 GC 回执。

`reconcile-derived-index` 在 active redirect 已经发布后，用 `resource_json={"index_sha256":"当前PDF索引SHA-256","v3_generation":"当前代"}` 先预演再执行。它重新核对全部 redirect 的旧/新对象、manifest、两站投影和当前索引，仅以单个 `If-Match` 更新 `derived_pdf_manifest.json` 中已验证的路径和 manifest，不改 sidecar、历史 catalog、v3 指针或文件。响应丢失时先读取该索引并比较预演报告里的前后 SHA，不盲目重试；若其他身份变化则重新审计。后续 redirect 审计同时识别 canonical 索引的旧路径和已迁移新路径。成功不授权删除；历史 sidecar、catalog、redirect 及其他引用依然保护旧对象。

`reader_sidecar_reconcile.py` 是后续对齐 canonical sidecar 与 legacy catalog 的受保护事务实现：先保存内容寻址计划和快照，先写带 pending 标记的新 catalog，再写 sidecar，最后条件清除标记。中途失败保留旧代，可按事务 ID 在同一隔离条件下恢复；pending 期间 GC、ack、压缩及普通 sidecar handoff 停止。它仍要求旧 writer fence 和独立写者隔离，当前没有公开 apply 入口，不能用 404 或调用参数代替凭据收口。

旧 `migrate-derived` / `resume-derived` 保留用于之后真正改 canonical 索引的多对象事务。该事务首次改索引前会保存内容寻址续跑计划并打印 `transaction_id`；续跑必须检查每个索引的精确旧值或新值。它仍受旧 fence 和外部写者隔离阻塞，不能把 redirect 成功误当 canonical 索引已迁移。

实际写入及续跑在各写入边界重新检查原有全部配置仓库、以及历史盘点中额外记录仓库的 GitHub 工作流：除当前发布工作流的当前 run 外均须停用并排空，外部仓库还须停用 Actions；检查失败、不可访问、404 或缺少凭据均停止。这是当前部署版本的旧维护 fence，**不代表已退场的 OCR 生产者在新架构中仍应运行，也不应要求用户重新创建旧账户**。后续应在确认实际活跃写者、撤销或收口旧桶写凭据之后，用面向真实写者的隔离替换它；在此之前旧 fence 仍会阻止 apply。仅 Actions 状态或 `external_writers_stopped=true` **不能证明**其他桶凭据已失效；必须在整个迁移和恢复期间协调其他写者停写。站点及对象的旧 URL 缓存、历史代、其他域引用不由迁移解决。

旧多对象迁移的三个可变索引和 v3 指针以及新单指针发布都使用 S3 `PutObject If-Match`，要求运行环境提供可访问 `vomebook` 桶的 `HF_S3_ACCESS_KEY_ID` / `HF_S3_SECRET_ACCESS_KEY`。`migrate-derived` 只读预演可在三个身份之外额外传入 `"check_s3":true`，用 Actions 同名 Secret 对四个现有索引执行 S3/Hub 字节一致性检查；这不尝试写入，也不能证明写权限或外部隔离。2026-11-07 在 `pdf-pages-v2` 和 `reader-assets-v2` 的随机临时键上验证了条件创建、条件更新、过期 ETag 拒绝、Hub/S3 跨接口更新可见性，以及条件删除；临时对象均已确认移除。实际四个索引经 S3 和 Hub 读取的字节相同，实测没有改生产索引。这只是该时间点和该凭据的验证，不代表 Actions 中的同名 Secret 有效。条件写只保护单个对象，**不代替上述 fence、真实写者隔离或跨对象事务**。无版本机制时 ETag 不能防止 ABA；条件写冲突及响应丢失时不要盲目重试，应检查事务再续跑。计划、不可变快照和发布投影仍有独立写入，旧对象仍保留。

`python3 scripts/reader_page_cleanup.py --graph-report output/gc/reader-gc-report.json --output output/page-cleanup.json` 可以从现有跨桶图的完整报告中分出 page 桶的 `derived/` 和 `objects/` 候选；缺桶或图不完整不输出候选。输出仍只是待审核报告，不能直接删除。2026-11-07 一次只读 S3 全图实测：约 107.7 万个 Reader assets 对象列举耗时约 497 秒，随后还需读取两万余根/manifest，运行在 1300 秒执行上限内未完成，**没有生成完整报告或删除授权**；这不等于已经确认用户遇到的 14 天问题原因。扫描在并行根读取阶段超时后会立即返回空桶、不完整且不授权删除的报告，不再等待线程池结束；已发出的网络请求仍可能短暂运行，调用方不得复用该次扫描状态。单次 inventory API 调用本身仍受其网络超时和重试限制。

原手动 `reader-gc-collect.yml` 提供 `scope=page-plan` / `scope=page-apply` 两步，在现有锁内直接执行：在已隔离并排空生产者的维护窗口，以 `resume_run={"paths":["derived/精确路径"]}` 生成计划，审阅 `page-exact-plan` artifact 和日志 SHA；随后设置 `apply=true`，并通过 `resume_run={"plan_run":"运行ID","plan_sha256":"计划SHA","external_writers_stopped":true}` 应用。应用会重新构建四桶图、核验两站及对象版本、重新验证所遍历的嵌套清单版本、要求额外确认外部写者已停，再记录 intent 并发送一次精确批次；部分成功或响应丢失时只可重新盘点，不会自动重试。计划有效期为 24 小时、artifact 保留两天，一次最多 1000 个键；这只延长审核期限，不延长引用、消费者或对象版本证明，应用时仍须重新扫描和复核。长时间扫描超过期限仍会失败，尚未实测真实桶上的耗时。它仍复用旧 CHM 事务检查及生产者 fence；若任何证明不足便停止，不能仅靠勾选外部确认绕过。旧 GC 尚未整体简化。

2026-11-07 已通过单指针 redirect 将 238 条当前路由切到 `objects/`，两站确认同一代，PDF/CAJ/KDH 的 Range 冒烟和 CAJ 两站浏览器翻页通过；`reconcile-derived-index` 随后对齐了 238 条 PDF canonical 索引，当前 245 条均不再以 `derived/` 为 `new_path`。canonical sidecar 和旧 catalog 尚未对齐，旧对象与历史代均未删除。旧多对象迁移仍受旧 fence 阻塞，page 精确删除所在 collector 仍禁用；当前结果不改变删除限制。单条路由下线入口、Pipeline 内 PDF/DjVu 页面流生产和 OCR 生产代码移除也已部署；尚未执行路由撤回、索引迁移或物理删除。历史 CHM 固定批次的 19 个工作流入口已移除，但旧全桶 GC 及其阻塞尚未整体重写；详见 `.github/WORKFLOWS.md`。

## 本地检查

```sh
python3 -m pip install -r scripts/requirements.txt PyYAML==6.0.3
python3 -B -m unittest discover -s tests -p 'test_*.py' -q
```

部分 Reader 转换测试需要系统工具，例如 `7z`、DjVuLibre 和 LibreOffice；缺少时部分测试会跳过。工作流说明见 `.github/WORKFLOWS.md`。搜索目前仍由 `search-daily-sync.yml` 生成数据并写入两个站点的 Git 仓库，数据与代码解耦是后续任务；本次 OCR 删除并未解决 Git 数据历史增长。
