# 工作流盘点与恢复边界

盘点基准：重建后的 `main`（2026-11-03）。本表覆盖整理前的 69 个工作流：首批移除 9 个、保留 60 个，另新增 2 个手动只读检查。**保留不等于批准运行**：仓库 Actions 暂停时已配置部分 secret，但不是所有入口的凭据都已准备；重新启用前必须逐项审查触发器、仓库身份、权限、调用链和最小必要 secret。表中不包含 secret 的值，也不是恢复操作说明。

计划触发器仍写在部分文件中，包括会写入或删除数据的任务。开启仓库 Actions 可能同时放行所有未单独禁用的定时任务；不要仅凭本表启用 Actions。普通 GC 仍被旧生产者范围、新旧仓库 ID 和历史维护恢复问题阻塞；仓库重建、404 或本地测试都不是隔离证明。

## 分批恢复前的保护快照

2026-11-03，以中央仓 ID `1443726000`、`main` 的 `23f141f` 为基准，在仓库 Actions 总开关仍关闭时，逐个将当时全部 60 个工作流设为 `disabled_manually`，包括 `workflow_call` 内层，逐项读回确认；当时没有 Actions run。后来单独放行 Space 健康检查完成一次成功运行，再次关闭总开关并逐项确认 60 个工作流禁用。此后新增的 BHA 手动检查需要单独处理，不在旧快照内。**关闭单个中央仓工作流不证明外部写者已隔离，也不授权 GC。**

恢复之前，须再次核验仓库 ID、`main`、完整工作流清单和每个状态、全局开关及各类未完成 run；任何新增、变更或读回不一致都应停止，而不是假定快照仍有效。随后只选择经过审查的入口逐个放行；外层及可复用内层的启停行为和调用链必须先核实，不能只启用外层。禁用期间的事件不能假定会自动重放，尤其需要单独检查校订提交及 issue 评论。

建议先从不写书籍或索引的验证任务入手，再按需要恢复搜索、校订和普通 Reader；写入及部署任务应分别核对权限、secret 和实际调用。仓库默认 `GITHUB_TOKEN` 权限曾只读；`search-daily-sync.yml` 已显式请求 `contents: write` 用于提交 `state/commits.json`，但尚未在 Actions 实跑验证。不能假定已配置的 `GH_PAT` 会自动用于该步骤。`bha-reindex-auth-check.yml` 只调用固定的状态 GET 端点，用于单独确认 `BHA_REINDEX_TOKEN`，不启动重建；应以实际 run 结果为准。搜索同步脚本只提交各自的生成数据路径，Git 操作或 rebase 失败会停止；本地 Git 测试不等于实际 Space／Pages 写入或端到端部署成功。

`reader-maintenance.yml`、`reader-gc-collect.yml`、`reader-gc-ccrd.yml` 和其他定点退役/撤回入口在独立安全审查前不得放行；`reader-v3-publish.yml` 还需评估日程及 `withdraw/reset-documents`，不能因普通 `unify` 调用就视为完全无风险。尤其是该 workflow 当前即使 `unify` 报告 unchanged 也会设置 `changed=true`：若 apply=true，后续仍可能发布 Pages、确认两端并压缩 base catalog。单本 CHM 的旧 document-reset 收据还绑定既有 v3 代与快照；不要把其新代视为已兼容，不能放宽 GC 检查。分批方案不修改旧维护 journal，也不扩大章节扫描或 Reader 限额。

## 保留：BHA、搜索与校订

| 文件 | 触发 | 用途与决策 |
|---|---|---|
| `bha-fork-archives.yml` | 定时、手动 | 维护 BHA 源仓 fork；保留。 |
| `bha-reindex-auth-check.yml` | 手动 | 固定 BHA 管理状态端点的只读鉴权检查，不启动索引重建。 |
| `bha-source-files.yml` | 手动 | 镜像源文件；可选 optimize 会改写镜像，不能当作只读任务。 |
| `bha-update-index.yml` | 定时、仓库事件、手动 | BHA 索引及校订状态更新；保留。 |
| `proofread-ocr-conflict-review.yml` | issue 评论 | 处理已有 OCR 补丁冲突的校订命令；不是远端 OCR 生产者。 |
| `proofread-publish-upstream.yml` | 定时、手动 | 向上游发布校订。 |
| `proofread-revert.yml` | 手动、issue 评论 | 校订撤销。 |
| `proofread-review-commands.yml` | issue 评论 | 校订审核命令。 |
| `proofread-submit-parse.yml` | 仓库事件 | 校订提交的解析入口。 |
| `proofread-submit.yml` | 手动、仓库事件 | 校订提交入口。 |
| `search-daily-sync.yml` | 定时、手动 | 搜索数据同步至 Space/Pages。 |
| `search-directory-tests.yml` | 指定路径 push、手动 | 本地搜索目录同步回归测试。 |
| `search-spaces-ci.yml` | 定时、仓库事件、手动 | Search Spaces 前端契约测试。 |
| `search-update-directories.yml` | 手动 | 更新 HF 仓库目录。 |
| `ops-keep-spaces-alive.yml` | 定时、手动 | 保活指定 Spaces。 |

## 保留：普通 Reader 和 CCRD 发布

| 文件 | 触发 | 用途与决策 |
|---|---|---|
| `publish-ccrd-index.yml` | 定时、手动 | 发布 CCRD/CW 基础索引，与已移除的 OCR 发布不同。 |
| `reader-chapter-search-repair.yml` | 手动 | 修复指定章节的搜索投影。 |
| `reader-chm-repair.yml` | 定时、手动 | CHM 本地临时转换并生成章节；不扩大扫描或限额。 |
| `reader-ebook-stream.yml` | 定时、手动 | 电子书章节流。 |
| `reader-image-stream.yml` | 定时、手动 | 独立图片资源。 |
| `reader-media-stream.yml` | 定时、手动 | 媒体转换流；保留音频等现用功能。 |
| `reader-office-chapters.yml` | 定时、手动 | Office 章节流。 |
| `reader-spreadsheet-stream.yml` | 定时、手动 | 表格阅读流。 |
| `reader-static-pdf-stream.yml` | 定时、手动 | PPT/PS/TIFF 等非 OCR 静态 PDF 转换，不等同 PDF/OCR 页面流。 |
| `reader-web-stream.yml` | 定时、手动 | 网页阅读流。 |
| `reader-v3-publish.yml` | 定时、手动 | 多个普通 Reader 流调用的中央投影；手动菜单仅保留 `inspect`、`unify`、`ack`、`retire`、`withdraw`、`reset-documents`，其中后两项会撤回路由，不应作为普通整理执行；旧文本/OCR 操作仍明确拒绝，退休日志定时器和暂停条件不变。 |
| `reader-media-backfill.yml` | 手动 | 现有媒体格式回填，可能写入资源。 |
| `reader-resource-repair.yml` | 手动 | 根据报告修复缺失的独立图片；不是恢复文档 OCR。 |
| `reader-ebook-processing-handoff.yml` | 手动 | 固定电子书处理记录的校验/交接；保留到其恢复状态单独确认。 |

## 保留：GC、隔离和兼容哨兵

| 文件 | 触发 | 用途与决策 |
|---|---|---|
| `reader-gc.yml` | 定时、手动 | 引用图报告；手动选项可记录观察，不应笼统称为永远只读。 |
| `reader-maintenance.yml` | 定时、手动 | 普通 GC 隔离与恢复编排；默认排程可能删除，保持关闭。 |
| `reader-gc-collect.yml` | 手动 | 精确收集器，被维护流程调用；保持关闭。 |
| `reader-gc-ccrd.yml` | 定时、手动 | 独立 CCRD GC；定时任务会应用 legacy 代际清理但不含 staging，手动可选 OCR 和 staging，保持关闭。 |
| `producer-migration-audit.yml` | 手动 | 生产者迁移证据审计；可写审计对象，并非隔离证明。 |
| `producer-v2-credential-preflight.yml` | 手动 | 已准备但未激活的 v2 身份/凭据检查；当前仍绑定旧仓 ID。 |
| `pdf-account-worker.yml` | 手动 | OCR 计划 job 硬禁用；当前 GC、维护预检、document fence 和 v2 shadow 将此路径作为库存完整性哨兵。不得直接删除或移除对空库存的失败关闭检查。 |
| `pdf-account-publish.yml` | 手动 | job 硬禁用；上述保留的 worker 文件仍有静态通知引用，本批一并保留。 |

## 保留：定点维护、预览和历史恢复

以下入口多数绑定固定清单、旧运行身份或维护日志；有的虽叫 preview 仍会隔离和恢复生产者。保留只是避免破坏历史恢复，不是授权再次执行、撤销映射或删除对象。外层工作流持锁后调用对应 `-inner` 工作流；不要独立删除其中一半。

| 文件 | 触发 | 用途与决策 |
|---|---|---|
| `ccrd-ocr-exact-remove.yml` | 手动 | 固定 OCR 退役清单及续跑；可能删除，暂留待历史核对。 |
| `ccrd-ocr-withdraw-consumer.yml` | 手动 | 固定身份的 OCR 消费端撤回；可能写 Space。 |
| `chm-epub-graph-preview.yml` | 手动 | 固定 CHM 引用图预览和隔离恢复。 |
| `chm-epub-retirement.yml` | 手动 | 固定 CHM 清单的退役及中断恢复。 |
| `chm-single-interrupted-preflight.yml` | 手动 | 对本次单本中断交接做只读检查，含纯内存 shadow 回归；无写入或恢复入口，不释放处理保护。 |
| `chm-final20-execute.yml` | 手动 | 固定最后 20 项的外层入口。 |
| `chm-final20-execute-inner.yml` | 被调用 | 对应固定最后 20 项的内层执行/恢复。 |
| `chm-remaining352-after-final20.yml` | 手动 | 固定 352 项续跑外层入口。 |
| `chm-remaining352-after-final20-inner.yml` | 被调用 | 对应 352 项内层续跑。 |
| `chm-subset-execute.yml` | 手动 | 指定 CHM 子集外层执行入口。 |
| `chm-subset-execute-inner.yml` | 被调用 | 对应子集内层执行。 |
| `chm-subset-locked-graph.yml` | 手动 | 指定子集锁内图校验外层。 |
| `chm-subset-locked-graph-inner.yml` | 被调用 | 对应锁内图校验。 |
| `chm-subset-locked-preflight.yml` | 手动 | 指定子集锁内预检外层。 |
| `chm-subset-locked-preflight-inner.yml` | 被调用 | 对应锁内预检。 |
| `chm-subset-protect.yml` | 手动 | 原 20 项保护外层；保留历史语义，不推断当前仍应执行。 |
| `chm-subset-protect-inner.yml` | 被调用 | 对应保护内层。 |
| `chm-terminal-release.yml` | 手动 | 历史终态及维护交接外层。 |
| `chm-terminal-release-inner.yml` | 被调用 | 对应终态及维护交接内层。 |
| `publish-chm-gc-release.yml` | 手动 | 历史 CHM GC 放行证据外层；本表不构成放行。 |
| `publish-chm-gc-release-inner.yml` | 被调用 | 对应放行证据内层。 |
| `clear-withdrawn-ocr-ids.yml` | 手动 | OCR ID 映射清理外层；映射已清空，不重新执行。 |
| `clear-withdrawn-ocr-ids-inner.yml` | 被调用 | 对应映射清理内层。 |
| `expanded-ocr-graph-preview.yml` | 手动 | 固定 OCR/PS 图预览及隔离恢复。 |
| `expanded-ocr-retirement.yml` | 手动 | 固定 OCR/PS 对象清单，可能删除；本次不执行。 |

## 本批已移除：不再提供 Actions 入口

旧脚本、历史路径分类和维护证据保留；这些路径仍可能出现在旧 journal、安全护栏、测试夹具或源代码中，不能把名称从历史记录里抹去。旧仓 run 已随删库消失，不表示外部写者已隔离。

| 文件 | 原因 |
|---|---|
| `exact-stale-run-delete.yml` | 一次性入口固定旧仓 ID 和两条旧 run；旧仓已重建。 |
| `force-ocr-book.yml` | 单书远端 OCR 的两个 job 均已硬禁用。 |
| `pdf-lane-watchdog.yml` | 旧 OCR worker 自动调度 job 已硬禁用；历史恢复不再重启它。 |
| `publish-ccrd-ocr.yml` | OCR/报刊旧发布入口的所有 job 均已硬禁用；基础 CCRD 索引单独保留。 |
| `reader-ocr-audit.yml` | 旧远端 OCR 审计 job 已硬禁用。 |
| `reader-ocr-repin.yml` | 旧 OCR 页面重绑 job 已硬禁用。 |
| `reader-publish-text.yml` | 远端标准化文本发布 job 已硬禁用，不涉及现有原生文字。 |
| `reader-v3-build.yml` | 旧候选构建/回调均已硬禁用；中央发布器中不可达的调度步骤同步删除。 |
| `stage-ccrd-ocr-text.yml` | 旧 OCR staging job 已硬禁用；其调用方也已移除。 |

在用源文件、原生内嵌文字、PDF/DjVu/PS 原件、章节、媒体和用户阅读数据均不在本批清理范围。未来如迁移 `pdf-account-worker.yml` 哨兵或清理历史恢复入口，应先设计独立的失败关闭协议、旧 journal 兼容和相关回归测试。
