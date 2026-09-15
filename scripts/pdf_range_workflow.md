# PDF 结构整理和线性化自动化

`pdf-range-assets.yml` 每小时第 37 分钟运行，并在 `Build Reader Assets` 成功结束后运行。
默认每个 checkpoint 检查 30 个输入，每次最多 4 个 checkpoint，最多 3 个文件并行。
工作流还会把每个 checkpoint 分成 4 个独立 Runner；每个 Runner 构建互不重复的
结果 bundle，最后由单个 publish Runner 合并并提交。这只并行计算，不并行修改
Reader Assets manifest，因此不会因为多个实例同时提交而互相覆盖。
规划按来源仓库和原始/生成类型分组，让已处理较少的组优先轮流获得额度，避免
按路径排序让其他格式生成的 PDF 长期排在原始 PDF 之后。元数据跳过不计入该进度。
包含大于 256 MiB 输入的批次使用一个 worker。成功发布后清理该批次上传的临时对象，
仅保留结果报告；其他输入和候选临时文件随任务释放。

## 范围与增量身份

- 覆盖当前搜索清单中的原始 PDF，以及 `manifest.json` 中 ready、reader_mode=pdf 的
  当前可读产物。原格式不参与白名单判断：TIFF、DJVU、PPT、表格、PS、CAJ 等输出
  PDF 时都会纳入。其他格式继续由原转换工作流决定输出类型。
- 已有字体修复、解密、格式转换的 PDF 以该产物为输入，不重新选用坏原件。
- 已通过逐页图片阅读的记录使用原图片路线，不覆盖其映射。
- 存量使用稳定队列分批回填，后续只处理新增/变化的文件和规则升级后的输入。
  批次数量限制只延后剩余任务，不会将未处理的文件标记为完成。
- 原始文件通过固定源 revision 下的 LFS SHA-256 或 Git blob OID 识别；仓库 revision
  只使元数据缓存失效，不等同于每个文件都变化。下载后验证实际 SHA-256/Git blob。
- 生成 PDF 通过当前产物 SHA-256 和上游 profile 识别。身份还包含 qpdf 版本、
  PDF.js/评测场景/决策规则版本；同内容输入可复用已完成的评测和产物。

## 评测和选择

`pdf_range.py` 使用固定 PDF.js 6.3.289、1 MiB Range、disableAutoFetch/disableStream，
在隔离的本地 HTTP 服务和冷浏览器页面中测量初始化、首页、附近两页、目录、空闲、
第 10 页/中间页/末页及最终空闲的读取字节和请求数。初始 200 探测另行统计。
一个文件的原件和候选共用同一个新启动的 Chromium 进程，但每个候选仍使用独立页面、
独立 PDF.js 任务和独立 HTTP 服务，避免重复启动浏览器，同时保留冷任务隔离。
采样渲染限制最长边为 2048，比较候选前后像素与提取文字；不会改写 PDF 内的图像。

低于 4 MiB 输入记录 unchanged，不下载/转换。大于 2 GiB 输入记录 unsupported。
这些元数据即可决定的状态在规划阶段批量记录，不占用每个 checkpoint 的浏览器评测额度。
其他输入先进行全页内容与结构签名校验；当前支持普通页面、资源、裁剪/旋转、元数据、
目录、显式本地页面打开目标及页码标签。打开目标中的页面引用按页序比较，防止对象
重新编号影响判断，并能检测目标页被改变。脚本、外部跳转等打开动作继续拒绝处理。
含加密、注释、表单、命名目标、复杂标签结构等尚未支持比较的文档记录 unsupported，
继续使用现有可读资源，不悄悄丢弃这些结构。

启动读取量大于 8 MiB，或至少 4 MiB 且超过整本 25%，会依次生成并评测：

1. `qpdf --object-streams=generate --stream-data=preserve`
2. `qpdf --linearize --stream-data=preserve`
3. `qpdf --object-streams=generate --linearize --stream-data=preserve`

对象流候选如果已经把启动读取量降到原件的 50% 以下、产物体积不超过原件 2%，
并通过完整内容签名，会直接结束该文件的候选搜索。否则继续评测线性化候选。
这不是跳过校验，而是避免在已经明显达标的文件上重复运行通常更慢的两个实验。

全部保留原始图像、字体和内容流。线性化还需通过 `--check-linearization`，但格式
有效不等同于加载更快。与原件对比，通过候选必须同时满足：

- 启动字节量至少下降 30%，至少节省 1 MiB，产物体积不增长超过 5%。
- 启动请求数不增加，固定跳页序列的累计字节量和请求数不高于原件。
- 空闲时不继续读完整文件，没有新增渲染异常。
- 全部页面内容/资源、几何、裁剪、旋转、元数据与目录目标保持一致，采样渲染和文字一致。

多个候选通过时，依次选择启动字节最少、启动请求最少、跳页累计字节最少的版本。
没有收益则记录 no-gain；候选测量不完整且没有通过结果时记录 failed，供显式重试。
每次浏览器测量限 90 秒，qpdf 转换限 120 秒；超时不是成功，也不把截断数据当成精确总量。
本地读取量不能直接换算成用户网络中的提速倍数；上线后还需原入口和代理验收。

## 状态、映射和发布

`pdf_range_manifest.json` 独立保存评测状态、输入身份、候选指标和选中产物，避免被
普通转换工作流的 active_keys 清理。原始转换 manifest 保持独立；常规发布器、清理器
和索引生成器共同合并结构优化记录，再应用有效的逐页图片映射。

上游生成产物的 SHA/profile 变化后，旧优化映射立即失效。原始源的变化在下一次指纹
扫描后停用旧映射并重新排队。工具版本升级而输入内容未变时，保留已有可读优化版
直至重评完成。失败不覆盖新上游修复产物；源文件删除会从范围状态移除。

候选文件、评测状态和共享 sidecar 在同一父 revision 保护的提交中发布。并发常规
转换的提交冲突会重读其最新 manifest 后重建 sidecar；范围状态并发变化则停止并重跑。
发布响应丢失但远端状态已一致时按成功处理。同一状态不会因仓库无关提交不断重写。
旧产物不会在发布时立即删除。原阅读 ID 和原始格式的下载 URL 保留。

其他格式新生成的 PDF 会在原转换工作流完成后被此工作流发现；首次回填也处理已存在
的 PDF 产物。采用独立任务以避免浏览器评测阻塞原格式转换，当前会下载固定资产版本
进行评测，而非在原转换进程内执行。

## 运行与验证

工作流支持 repo/path 精确定位、每批 limit、checkpoint 数量、retry_failed 和 dry_run。
失败的相同输入不会每小时无限重试；修复工具后使用 retry_failed，或变更评测版本。
计划只读取源清单与文件指纹，不会因 dry-run 下载 PDF 或发布状态。

```bash
python3 -m pip install -r scripts/requirements-pdf-range.txt
python3 -m playwright install chromium
npm install --ignore-scripts --no-audit --no-fund pdfjs-dist@6.3.289
python3 -B scripts/pdf_range_assets.py --limit 10 --workers 2 --build-only
python3 -B -m unittest tests.test_range_pdf tests.test_reader_assets tests.test_pdf_assets tests.test_repair_gbk_pdf -q
```

前两个输入文件默认为现有源解析器生成的 `output/search_data.json` 和 `state/commits.json`；
使用 `--vendor` 可指定固定 pdfjs-dist 或现有 Reader vendor 路径。`--build-only` 完成
生成与校验但不发布，`--dry-run` 仅输出计划。浏览器实测属于显式重型验收，默认测试
命令离线且无需浏览器；第 17 册的真实测量及完整 Reader 验收见 `range_pdf.md`。
