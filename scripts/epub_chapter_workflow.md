# 电子书章节按需加载范围

电子书 Reader 对章节包先读取 `chapter-manifest.json`，打开第 1 章并预取附近章节；
后续章节由滚动位置按需读取。原始 EPUB/MOBI/AZW3/FB2/CHM 仍保留为 fallback，
章节包只是阅读入口的派生资源。

## 当前范围

`EPUB_CHAPTER_SPLIT_BYTES` 为 8 MiB，适用于 EPUB、MOBI、AZW3、FB2 的 `foliate` Reader。
CHM 转 EPUB 的 `epub` Reader 单独使用 `CHM_CHAPTER_SPLIT_BYTES`（16 MiB）。

低于 8 MiB 的普通电子书仍使用原始包，避免为小书增加大量独立对象和发布开销。
这个阈值不是格式限制，而是按生产清单统计选择的成本/收益边界。

2026-09-16 生产 API 核对的来源记录统计（不是去重后的资产数量）：

| 原始文件大小 | 文件数 | 处理方式 |
| ---: | ---: | --- |
| 小于 4 MiB | 813 | 原始包 |
| 4-8 MiB | 98 | 原始包 |
| 8-16 MiB | 54 | 新增章节包范围 |
| 16-32 MiB | 37 | 已有章节包范围 |
| 至少 32 MiB | 28 | 已有章节包范围 |

统计来源：生产 `https://voiceofml-search.hf.space/api/search`，查询 EPUB、MOBI、
AZW3、FB2，返回总数 1,030。大小是搜索元数据快照，可能随源清单更新变化。
新增档位包括 37 条 EPUB、8 条 MOBI、7 条 AZW3、2 条 FB2。

## 增量行为

现有 Reader Assets 条目若已经是 ready 但缺少 `chapter_manifest`，且当前源大小达到
8 MiB（CHM 为 16 MiB），会由 `scan_reader_assets.py` 自动重新入队。已有章节包不会因
仅降低阈值而全部重建；当前章节包 profile 保持不变。新增或源内容变化的电子书按
现有 source revision、SHA 和 profile 复用规则处理。

章节包生成前仍会校验 EPUB spine、HTML、资源数量和资源总大小；生成失败保留原始
Reader 资源，不会发布不完整章节包。发布同时写入 manifest、章节对象和 sidecar，
失败重试沿用现有 Reader Assets 工作流。

## 后续判断

章节包必须携带完整 `epub-search-index.json.gz`，用于 Reader 首次搜索时按需加载；
验收应比较全书命中总数，并确认未加载章节的结果能跳转高亮、原始来源信息正确。
8 MiB 以下暂不全量拆分；后续按章节数、首章/资源大小和实测加载开销建立候选名单。
章节包的收益来自独立章节读取；只有章节很少或首章包含大量共享资源的书，拆包可能
收益有限。
