# Pipeline 工作流

`.github/workflows/` 是当前 Actions 入口；Git 历史可查询先前的一次性任务。工作流文件存在不等于已启用或已部署。重新启用任何定时任务前，应先核对其触发器、写入权限、目标和线上生产者。

## 主要入口

- `search-daily-sync.yml`：生成搜索数据并同步两站；目前仍将生成数据提交到站点 Git，尚未解耦。
- `bha-*`、`proofread-*`：BHA 数据和校订处理。OCR 生产和冲突审核入口已移除，但 BHA 历史文本输入仍在使用。
- `reader-*-stream.yml`、`reader-chm-repair.yml`、`reader-office-chapters.yml`、`reader-v3-publish.yml`：Reader 资源转换和发布。`reader-static-pdf-stream.yml` 把 PPT/WPS/PS 等源文件转成供直接阅读的完整 PDF，不是已退场的 PDF/DjVu 逐页渲染链。`redirect-derived` 将匹配的 PDF 阅读路由通过一个 S3 条件指针切到现有 objects；`reconcile-derived-index` 在两站已确认后以单对象条件写更新 PDF canonical 索引，均保留旧对象和历史代；`publish-generation` 可恢复两站发布。redirect 活跃时 `unify` 和单路由修改先重新审计并条件发布；document reset 仍阻止。旧 `migrate-derived` 修改多个 canonical 索引，不删除对象，仍受旧 fence 和写者隔离阻塞。`remove-route` 撤回单条派生 Reader 路由的功能已部署，但尚未执行实际撤回。故障、中断和外部写者风险见 `README.md`。
- `scripts/reader_sidecar_reconcile.py` 为后续 canonical sidecar/catalog 对齐提供受旧 fence 约束的可恢复事务；当前无公开 apply 入口，pending 标记会阻止 GC 与普通 handoff。
- 精确桶文件查询留作本地 `scripts/bucket_file.py` 命令，不再单设工作流。`scripts/reader_exact_references.py` 可快速解释已知直接索引和历史代的精确引用；`scripts/reader_route_plan.py` 可预览单书路由。两者均不扫描完整图，也不授权删除。
- `reader-gc.yml`、`reader-maintenance.yml`、`reader-gc-collect.yml`、`reader-gc-ccrd.yml`：原有全桶分析和 GC 路径。精确 page 文件计划/应用已部署到现有 collector 菜单，但 collector 仍禁用；仍须维护隔离、四桶图、两站、版本、CHM 检查与外部停写。部分定时任务可写入或删除。

## 移除的一次性入口

固定 CHM 退役、预检、特定批次、终态和 GC 放行工作流均从 Actions 入口删除，包括它们的外层与 `-inner`。普通 `reader-chm-repair.yml` 不在其中。旧 CHM 脚本、测试、维护 journal 和桶对象暂未删除：某些旧 GC 路径仍依赖它们；**删除入口不表示旧事务完成、隔离已恢复或资源可被自动 GC**。如确需恢复历史事务，须先核对线上状态并从 Git 历史恢复相应入口。

PDF/DjVu 的 OCR 前置页面流生产器及其旧 manifest 写入迁移命令已移除并部署，历史 page manifest 的只读引用解析保留。`pdf-account-worker.yml` 暂为旧 GC 护栏所依赖的无执行库存哨兵，不代表外部写者已隔离。此次同时移除固定电子书交接、两个一次性 producer 审计及重复 media backfill 的 Actions 入口；脚本、计划和历史记录仍在。日常 media 工作流手动 `apply=true, limit=0` 覆盖 backfill 目的，但其 codec 检查与空矩阵行为不同；旧交接不能再从 Actions 续跑。

截至 2026-11-06，中央仓 Actions 已启用，`search-daily-sync.yml`、`search-directory-tests.yml`、`ops-keep-spaces-alive.yml`、`search-spaces-ci.yml` 和 `reader-v3-publish.yml` 已启用并完成基本运行；其余工作流仍禁用。Reader 发布器现为手动入口，不再定时执行退役 journal。此状态不是其他仓库或桶凭据已隔离的证明；真实迁移和清理仍需单独核验。
