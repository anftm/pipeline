# BHA 校订、拆分及索引恢复交接（2026-11-06）

> **此分支是未启用的恢复候选，不是已恢复的生产服务。** 用户希望尽快恢复 BHA 校订/拆分投稿、随后恢复索引自动更新；这份交接供后续 agent 接手。不要把模拟结果或 GitHub 接受 dispatch 当作真实完成回执。

**并行更新：**本分支首个候选提交推送期间，其他会话已将中央 main 从 `5abc978...` 推进到 `0a3b479ff6720c28c97f8e6127c1598bae1fdd30`，该提交只新增 `CCRD-AUTOMATION-HANDOFF-20261106.txt`，未改本候选文件。不要把本分支旧父提交当成当前 main，也不要以本分支强推覆盖它；下一次操作应再次核对 main 和其他会话。

## 当前线上与权限

- 中央仓库为 `anftm/pipeline`（仓库 ID `1443726000`）；本分支从旧 main `5abc978f45f3bc4717e0ef460f800a81e069564a` 开始，只包含 BHA 候选和本说明；新的并行 main 见上。推送分支不改变 main、workflow 状态或旧任务。
- BHA Space `vomebook/bha-Search` 当前部署 `decd6c30a1fe768ccbce2710b03c4160f442fbb4`，runtime RUNNING；健康索引 ready、377513 文档，搜索与预览通过。其前端保护草稿，不把通用202视为可靠回执；快照恢复失败时不会在启动时自动重建；旧快照 generation 和临时清单被保留。
- 校订/拆分接口在当前配置默认暂停，随机不存在的合成文档测试均返回503及保留草稿。Space 没有设置解除暂停的 variable/secret。不要用真实用户稿或重放旧请求验收。
- 中央53个 workflow 全部 `disabled_manually`，8个 run 全终态。当前可用 GitHub 账号仓库 `push=true`、`admin=false`，查看 Actions 仓库策略返回403，**不能据此判断当前总开关开启或关闭**。Secret 名称存在不证明值、权限或有效性。此前管理员凭据不在当前环境，不在本文件记录或复述。
- Reader 维护会话的既有部分保护检查曾依赖中央 main 固定 `5abc978...` 且所有 workflow 禁用；并行 CCRD 文档提交已改变 main，必须由维护方核对自己的新基线。PDF 245 项复制/只读预检已完成，但路由交接仍未完成。任何合并主分支或启用 workflow 前须与该会话协调；不能覆盖其改动、旧路由、收据或 processing pin。不得启动旧远程 OCR 或 GC，不扩大章节扫描/Reader 限额。

## 本分支内容与明确暂停

- `scripts/submit_proofread.py` 及两个投稿 workflow：校订/拆分校验预览身份与基线、检查 archive 分支变化，对新旧 auto-merge 标记一律保留 PR 并创建人工审核 issue；PR/issue 不完整响应及部分效果停止；workflow 使用 `queue: max`。**`main()` 入口仍由 `require_paused_delivery()` 无条件拒绝，未恢复后台接收。**
- `.github/workflows/bha-update-index.yml` 与 `scripts/bha_reindex_result.py`：增加 job、目标版本和 snapshot 状态确认；workflow 目前禁用，且其15分钟定时任务启用后可能真正写 parsed、tracker、索引和 snapshot，不是只读。生产 reindex secret 的值及鉴权有效性未知。
- `tests/test_bha_minimal_recovery.py`、`tests/test_bha_reindex_result.py` 等是本地模拟验证；2026-11-06 相关189项测试通过。不能推断真实 Actions、PR、回执或生产 reindex 已验收。
- Space 配套离线候选及测试已保存到本分支 `bha-recovery-candidate/`；它**不是**线上 Space 的当前实现，也不含此前本地独立实验/桥文件。完整本地候选另在 `/root/opencode-chm-scratch-20261102/bha-minimal-recovery-20261104/`。**不可直接覆盖线上当前 `app/main.py`、`scripts/start.sh` 或 snapshot 清理保护**；候选基于更旧 Space 版本，合并须按当前 `decd6c...` 基线逐文件审查。先读候选目录 README。
- 新版 `content_hash` 会改变所有文档预览基线；即使先前已发布草稿保护，**已经打开的旧页面**仍可能在 hash 不匹配时删除其拆分草稿，或收到普通202时清稿。新页面只能保留/复制部分旧稿；旧页面退出时机无法证明。当前候选 hash 不应无提示部署。
- 同一 Space 进程的不确定状态只在内存中，重启/多副本后消失；通用202不是持久接收或真实结果，前端同表单防重也不跨刷新或设备。真实持久接收、独立可查回执、跨运行去重、外部写者隔离及 PR/issue/crop 部分成功的恢复均未完成。BHA 索引桶 `vomebook/bha-data` 为公开桶，**不要存投稿正文、capability 或凭据**。

## 接手建议

1. 重新只读确认中央 main、所有 workflow 状态和活动 run、Space head/runtime、secret 权限以及 Reader 维护停点；不要盲目合并旧基线或启用旧任务。
2. 获得管理员对 Actions 总策略的确认及操作权限，避免只放开 Space 而接收器仍停。先在隔离目标验证 GitHub token 对 archive PR、tracker、裁剪文件的最小权限与可观察结果。
3. 按当前 Space 基线合并校订、拆分和身份候选，明确处理旧页面/旧草稿，完成测试和受控启动；生产接收与回执能力未确认之前不要把请求标成已处理，部分效果不得整次重放。
4. 协调 Reader 维护条件后，仅逐项部署/启用必要的 BHA 投稿路径；先验收新的校订和拆分处理及人工审核，再受控处理索引。不要启用其他工作流或旧 OCR/GC。
5. 索引部分还需核对 snapshot 容量、secret 鉴权、失败/超时后的跨运行重复 POST 和状态身份。当前双观察不是外部写者锁，已知本地状态非原子 CAS；任何不确定结果只读核查，不重发旧任务。

更多本地记录：`/root/opencode-chm-scratch-20261102/bha-frontend-first-deploy-20261104/README-deployment-status.md`、`bha-minimal-recovery-20261104/README-minimal-recovery.md`、`bha-submission-recovery-20261103/README-local-candidate.md`、`reader-store-deployment-handoff-20261103.md`。这些绝对路径只在当前机器可用；不包含任何凭据。
