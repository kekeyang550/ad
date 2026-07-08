# A 股选股项目：同花顺本地缓存解析优先版

这是一个只读读取本机同花顺普通客户端缓存的选股项目雏形。第一版重点是把本地可安全利用的数据接入进来，并把未知二进制行情格式作为诊断信息保存，不猜字段、不接交易、不读取 cookie 或账号敏感文件。

默认同花顺路径：

```powershell
D:\同花顺软件\同花顺
```

## 功能

- 检测同花顺安装目录、`stockname`、`realtime`、`hexin.exe`
- 用 GB18030 解析 `stockname\stockname_*.txt`
- 解析 `realtime\market.txt`
- 通过共享读读取正在被同花顺占用的 `stocknow.dat`
- 保存 SQLite 表：`securities`、`market_snapshots`、`quotes_realtime`、`watchlists`、`scores`
- 对未知行情二进制格式保留诊断，不输出伪造价格
- 可用腾讯公开接口补充实时行情和前复权日线
- 基于实时行情、流动性、市值、换手率、板块和日线趋势生成规则评分
- 借鉴选股公式思路，转换成可解释因子，并用真实日线做简易回测
- 基于评分、行情、日线趋势、公式型因子、同花顺本地新闻和本地备注生成 AI 辅助选股观点
- 提供命令行状态、导入、评分、数据库概览

## 使用

```powershell
python -m ths_stock_picker status
python -m ths_stock_picker ths-monitor
python -m ths_stock_picker import-ths-news
python -m ths_stock_picker news --limit 20
python -m ths_stock_picker news --tag AI算力
python -m ths_stock_picker factors
python -m ths_stock_picker factor-scan --limit 20
python -m ths_stock_picker factor-backtest --horizon 5 --limit-symbols 300
python -m ths_stock_picker import
python -m ths_stock_picker score
python -m ths_stock_picker score --profile configs\scoring.default.json
python -m ths_stock_picker score-runs --limit 10
python -m ths_stock_picker compare-runs --limit 20
python -m ths_stock_picker write-default-profile --out configs\scoring.default.json
python -m ths_stock_picker db-info
python -m ths_stock_picker snapshots --limit 5
python -m ths_stock_picker inspect-symbol 600000 --json-out outputs\600000.inspect.json
python -m ths_stock_picker capture-symbols 600000 000001 600519 --out outputs\capture_baseline.json
python -m ths_stock_picker observation-template 600000 000001 600519 --out outputs\observations_template.csv
python -m ths_stock_picker match-observations --capture outputs\capture_baseline.json --observations outputs\observations_template.csv
python -m ths_stock_picker auto-infer-fields 600000 000001 600519 300750
python -m ths_stock_picker import-public-quotes 600000 000001 600519 --observations-out outputs\observations_public.csv
python -m ths_stock_picker import-public-quotes --from-cache --limit 200
python -m ths_stock_picker import-public-history --universe auto --limit 200 --days 80
python -m ths_stock_picker universe --source auto --limit 50
python -m ths_stock_picker run-daily --limit 200 --history-days 80 --profile configs\scoring.default.json --out-dir outputs
python -m ths_stock_picker import-history path\to\daily.csv
python -m ths_stock_picker score
python -m ths_stock_picker scores --limit 20
python -m ths_stock_picker scores --limit 20 --positive-only
python -m ths_stock_picker score-runs --limit 10
python -m ths_stock_picker compare-runs --limit 20
python -m ths_stock_picker explain 600000 --bars 8
python -m ths_stock_picker note 600000 --status watch --tags "银行,低波" --text "观察回踩"
python -m ths_stock_picker notes --limit 20
python -m ths_stock_picker notes --limit 20 --status review
python -m ths_stock_picker notes --q "放量" --sort score
python -m ths_stock_picker delete-note 600000
python -m ths_stock_picker ai-pick --limit 20 --min-score 20 --save
python -m ths_stock_picker ai-explain 600000 --save
python -m ths_stock_picker ai-history --limit 30
python -m ths_stock_picker ai-history --symbol 600000
python -m ths_stock_picker ai-changes --limit 50
python -m ths_stock_picker candidates --limit 50 --out outputs\candidates.csv
python -m ths_stock_picker report --limit 20 --out outputs\daily_report.md
python -m ths_stock_picker export --out-dir outputs
python -m ths_stock_picker serve --host 127.0.0.1 --port 8765
```

自定义同花顺路径或数据库路径：

```powershell
python -m ths_stock_picker --ths-root "D:\同花顺软件\同花顺" --db "work\ths_stock_picker.db" import
```

## 设计边界

- 本项目仅用于个人投研和选股辅助，不提供投资建议。
- 不接实盘交易，不调用 `xiadan.exe`。
- 不抓包、不绕过登录、不破解加密数据。
- 财务和历史行情若普通客户端没有自然落地缓存，后续通过导出文件或开源数据源补齐。
- `stocknow.dat` 当前只解析代码、记录边界和诊断信息；价格等字段需要用界面数据对照确认后再启用。

## 字段反推工作流

1. 用 `capture-symbols` 保存几只股票的原始 546 字节记录。
2. 用 `observation-template` 生成 CSV 模板。
3. 打开同花顺，填入界面显示的现价、涨跌幅、成交量、成交额等字段。
4. 用 `match-observations` 自动筛选可能的字段偏移和编码方式。

也可以跳过人工填写，使用 `auto-infer-fields` 自动抓取公开行情作为观测值。如果同花顺本地缓存没有可匹配的价格字段，使用 `import-public-quotes` 将公开实时行情作为价格补充源写入 `quotes_realtime`，同花顺本地缓存继续负责证券池、名称、市场和诊断。公开行情补充源当前会写入现价、涨跌幅、成交量、成交额、总市值、流通市值、换手率和板块分类。

## 每日更新

推荐日常使用：

```powershell
python -m ths_stock_picker run-daily --limit 200 --history-days 80 --profile configs\scoring.default.json --out-dir outputs
python -m ths_stock_picker scores --limit 20 --positive-only
python -m ths_stock_picker explain 600000 --bars 8
```

`run-daily` 会依次执行同花顺本地缓存导入、公开实时行情补齐、公开日线补齐、评分、CSV 导出、候选池导出和 Markdown 日报生成。

评分器当前包含：流动性、盘中动量、价格区间、盘中位置、市值分层、换手率分层、板块风险、ST/PT/退市名称风险，以及基于日线的 MA5/MA20 趋势、5 日/20 日动量和 20 日波动率。

评分分项权重可以通过 JSON 配置调整。默认模板位于 `configs\scoring.default.json`，也可以重新生成：

```powershell
python -m ths_stock_picker write-default-profile --out configs\scoring.default.json
```

配置中的 `component_weights` 用于放大或缩小分项分，`disabled_components` 可禁用某个分项。未出现在配置里的分项默认权重为 `1.0`。

每次评分都会写入 `score_runs` 批次记录，并在 `scores` 中保存 `profile_name`，可用 `score-runs` 查看最近评分批次。

连续运行两次评分后，可以用 `compare-runs` 比较最近两个批次的分数、排名和新增/下降变化；Web 面板在存在至少两个批次时也会显示“批次变化”。

启动本地只读 Web 面板：

```powershell
python -m ths_stock_picker serve --host 127.0.0.1 --port 8765
```

浏览器打开 `http://127.0.0.1:8765/`，可以查看数据表计数、候选池、评分榜、批次变化和同花顺缓存解析诊断。首页支持按代码/名称搜索、按板块筛选、设置最低分、排序和导出当前候选 CSV；`http://127.0.0.1:8765/ths` 可查看同花顺进程和 A 股实时缓存活跃度；`http://127.0.0.1:8765/news` 可查看同花顺本地新闻缓存；`http://127.0.0.1:8765/factors` 可查看公式型因子定义、当前命中和历史回测；`http://127.0.0.1:8765/ai` 会生成 AI 辅助选股榜，并可一键保存本次榜单；`http://127.0.0.1:8765/ai/history` 可查看已保存 AI 历史观点；`http://127.0.0.1:8765/ai/changes` 可查看最近两次 AI 观点的变化；个股详情页包含 AI 观点、相关新闻、本地观察记录、分项分、触发规则、最近日线表和轻量走势 SVG 图。

## 公式型因子和回测

项目会把选股公式网站或软件公式中的思路拆成可解释、可复现的 Python 因子。公式名称不直接进入决策，必须先经过：

1. 逻辑拆解：确认使用的是价格、成交量、均线、指标等可获得字段。
2. 未来函数检查：规避重绘、未来引用、不可复现字段。
3. 当前扫描：用最新日线判断哪些股票触发信号。
4. 历史回测：统计触发后未来 N 个交易日的胜率、平均收益、最好和最差表现。

当前内置因子包括：

- 均线共振突破：类似一阳穿多线，要求收盘站上 MA5/MA10/MA20。
- 温和放量突破：接近或突破 20 日高点，成交量温和放大。
- MA20 缩量回踩：趋势仍在 MA20 上方，价格贴近 MA20 且缩量。
- 追高过热风险：近 5 日涨幅过大或长上影放量。

命令行：

```powershell
python -m ths_stock_picker factors
python -m ths_stock_picker factor-scan --limit 20
python -m ths_stock_picker factor-backtest --horizon 5 --limit-symbols 300
```

这些因子会作为 AI 选股的证据之一，但不会单独决定买卖。后续可以从 `https://www.gupang.com/` 等公式资料库继续挑选逻辑清晰的公式，转换为因子后再加入回测。

## 同花顺实时缓存监控

项目会只读检查同花顺主进程和 A 股本地实时缓存：

```powershell
python -m ths_stock_picker ths-monitor
```

监控对象包括 `hexin.exe`、`hexinhelper.exe`、`xiadan.exe` 是否存在，以及 `realtime\shase\stocknow.dat`、`realtime\sznse\stocknow.dat` 的文件大小、更新时间和距今时间。状态含义：

- `active`：缓存 3 分钟内更新
- `stale`：缓存 1 小时内更新，但不是近期活跃
- `old`：缓存超过 1 小时未更新
- `missing`：关键缓存不存在

该监控只判断同花顺是否把数据写入本地文件；同花顺界面内存中的逐笔刷新不一定会同步落盘。

## AI 辅助选股

当前 AI 层先采用本地结构化分析引擎，不依赖外部大模型即可运行。它会把评分分项、触发规则、实时行情、近 30 日线、公式型因子、同花顺本地新闻、观察池备注合成为：

- 结论：重点观察、观察、等待回踩、谨慎复盘、回避
- 置信度：基于价格字段、成交额/换手率、评分分项、日线数量和本地备注完整度计算
- 正向证据：趋势、流动性、换手质量、盘中位置、本地标签等
- 风险点：追高、换手过热、趋势破位、波动过高、ST/退市风险等
- 下一步动作：等待回踩、复核行业催化、观察 MA5/MA20 支撑等

新闻来源优先使用同花顺本地明文资讯缓存，例如 `text\同花顺\实时解盘.xml`。可手动导入和查看：

```powershell
python -m ths_stock_picker import-ths-news
python -m ths_stock_picker news --limit 20
python -m ths_stock_picker news --tag AI算力
```

新闻会被打上初步事件标签，例如业绩预告、退市风险、并购投资、AI算力、消费、新能源、政策监管。AI 选股会优先匹配个股相关新闻；没有直接个股新闻时，会按板块/名称补充主题新闻作为辅助证据。

命令行：

```powershell
python -m ths_stock_picker ai-pick --limit 20 --min-score 20
python -m ths_stock_picker ai-pick --limit 20 --min-score 20 --save
python -m ths_stock_picker ai-explain 600000
python -m ths_stock_picker ai-explain 600000 --save
python -m ths_stock_picker ai-history --limit 30
python -m ths_stock_picker ai-history --symbol 600000
python -m ths_stock_picker ai-changes --limit 50
```

使用 `--save` 或 Web 上的“保存本次 AI 榜单”会把生成的结构化观点写入 `ai_decisions` 表，供后续复盘和对比。`ai-changes` 会对每只股票最近两次保存的 AI 观点做比较，标记新增、结论变化、置信度变化和稳定项。后续可在这一层接入 OpenAI 或本地大模型，让大模型读取同一份结构化证据后生成更细的行业、财务和风险复盘。

本地观察记录保存在项目 SQLite 的 `stock_notes` 表里，不会写回同花顺文件。可以在个股详情页编辑，也可以进入 `http://127.0.0.1:8765/notes` 查看“观察、持有、回避、复盘”分组后的观察池。观察池支持搜索代码/名称/标签/备注、按更新时间/评分/涨跌幅/现价/代码排序、导出 CSV，并可在列表页删除本地记录。命令行同样支持写入、筛选、搜索、排序和删除：

```powershell
python -m ths_stock_picker note 600000 --status watch --tags "银行,低波" --text "观察回踩"
python -m ths_stock_picker notes --limit 20
python -m ths_stock_picker notes --limit 20 --status watch
python -m ths_stock_picker notes --q "回踩" --sort score
python -m ths_stock_picker delete-note 600000
```

## 历史日线导入

`import-history` 支持 CSV，列名可使用英文或常见中文表头：`代码`、`日期`、`开盘`、`最高`、`最低`、`收盘`、`成交量`、`成交额`。如果 CSV 没有代码列，可以用 `--symbol 600000` 指定默认代码。

也可以用公开日线接口直接补齐最近一段前复权日线：

```powershell
python -m ths_stock_picker import-public-history 600000 000001 --days 80
python -m ths_stock_picker import-public-history --universe auto --limit 200 --days 80
```
