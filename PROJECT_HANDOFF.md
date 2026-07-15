# A 股 AI 选股项目交接记录

更新时间：2026-07-15

## 项目位置

本地目录：

```powershell
C:\Users\Administrator\Desktop\Workspace\A
```

同花顺本地路径：

```powershell
D:\同花顺软件\同花顺
```

本地 Web 面板：

```text
http://127.0.0.1:8765/
```

GitHub 仓库：

```text
https://github.com/kekeyang550/ad
```

GitHub 远端已与本机 `main` 完成同步。本机 Windows 代理使用 `127.0.0.1:7890`，Git 不会自动继承该设置；普通 `git push` 连接失败时，可只对当前命令使用：

```powershell
git -c http.proxy=http://127.0.0.1:7890 -c https.proxy=http://127.0.0.1:7890 push origin main
```

该写法不会修改全局 Git 配置。若代理端口在其它电脑不同，请替换为实际端口或在可直连 GitHub 的网络执行普通 `git push`。

## 项目目标

最终目标是做一个面向中国 A 股的个人投研辅助系统：

1. 从同花顺本地普通客户端读取可安全使用的数据。
2. 借鉴公开选股公式思路，转换成可解释、可复现的选股因子。
3. 使用真实行情和历史日线做因子回测、组合策略回测。
4. 结合新闻、行业主题、走势、因子、备注，由 AI 给出结构化选股建议。
5. 形成“数据更新 -> AI 选股 -> 因子验证 -> 策略回测 -> 观察复盘”的闭环。

项目仅用于个人投研辅助，不提供投资建议，不接实盘交易。

## 当前已实现功能

### 数据接入

- 解析同花顺 `stockname` 股票/指数名称表。
- 解析 `realtime\market.txt` 市场配置。
- 只读监控同花顺进程和实时缓存：
  - `hexin.exe`
  - `hexinhelper.exe`
  - `xiadan.exe`
  - `realtime\shase\stocknow.dat`
  - `realtime\sznse\stocknow.dat`
- 读取本地自选/股票池。
- 使用公开接口补充实时行情和前复权日线；本地仅代码缓存重新导入时保留公开价格补充，公开接口空/部分响应也只更新成功返回的代码，避免评分失去行情字段。
- 只读解析通达信 `.day` 日线，并将通达信作为本机主要历史数据源。
- 只读解析通达信 `block_gn.dat` 概念和 `block_fg.dat` 风格成员缓存，主题记录会先校验固定结构、GBK 名称和连续 6 位代码。
- 可导入本地财务 CSV：营业收入、净利润、ROE、经营现金流、PE TTM、PB；保留报告期、公告日期、原始数值单位和来源文件。
- 可从公开东财财报接口补齐已核验的报告期、公告日期、营业收入及其同比、归母净利润及其同比、加权 ROE 和经营活动现金流；公告日期统一取主财报，避免现金流表中的异常旧公告日进入回测。
- 可从公开东财公司概况逐只补充当前三级行业归属；行业标签仅作当前研究上下文，不参与历史回测。
- 解析同花顺本地资讯缓存，例如 `实时解盘.xml`。
- 消息面采用确定性标签区分业绩利好、业绩风险、减持质押、回购增持、中标订单和中性公告；AI 不会把预亏或同比下降当作正向催化，`reclassify-news` 可重标历史资讯。
- 所有同花顺文件只读访问，不读取账号、cookie、交易敏感文件。

### 数据库

默认数据库：

```text
work\ths_stock_picker.db
```

主要表：

- `securities`
- `market_snapshots`
- `quotes_realtime`
- `watchlists`
- `daily_bars`
- `scores`
- `score_runs`
- `stock_themes`
- `stock_industries`
- `stock_notes`
- `ai_decisions`
- `news_items`
- `factor_backtest_cache`
- `factor_scan_cache`
- `data_versions`
- `strategy_validation_runs`
- `daily_runs`

`work/` 和 `outputs/` 已加入 `.gitignore`，不应提交。

### 评分和 AI 选股

已实现：

- 规则评分
- 日线趋势评分
- AI 辅助选股页
- AI 历史观点保存
- AI 榜单与历史观点保留单只报价时间，便于复核结论使用的行情时效；旧历史缺少该字段时兼容显示 `-`
- 个股详情和一键诊股展示最近 5 条已保存 AI 观点，连同保存时间、评分日、结论、置信度、行情时间和摘要
- AI 观点变化对比
- AI 观点后续表现复盘：保存时评分日后，以首个有效交易日开盘到第 N 个后续交易日收盘；观察期未满保留待观察，并按保存结论汇总已完成样本的命中率和平均收益
- 每日 AI 快照重跑时按“代码 + 评分日”替换旧版本；观点变化和后续表现同样按该键去重，避免重复执行夸大样本数
- 个股详情页
- 个股本地备注/观察状态
- AI 榜单一键加入观察池
- AI thesis 的触发条件和失效条件（基于均线、评分门槛与观察状态，保存后可复盘）

AI 目前是本地结构化分析引擎，不依赖外部大模型。它综合：

- 综合评分
- 实时行情
- 日线趋势
- 公式型因子
- 因子历史有效性
- 同花顺本地新闻
- 本地观察备注

输出：

- 重点观察
- 观察
- 等待回踩
- 谨慎复盘
- 回避

### 公式型因子

已内置因子：

- `ma_multi_breakout`：均线共振突破
- `volume_breakout`：温和放量突破
- `ma20_pullback`：MA20 缩量回踩
- `macd_zero_axis_cross`：MACD 零轴上金叉
- `rsi14_recovery`：RSI14 回升确认
- `kdj_low_cross`：KDJ 低位金叉
- `limit_up_pullback`：涨停后缩量回踩
- `platform_breakout`：平台整理突破
- `overheat_chase_risk`：追高过热风险
- `rps_60_strength`：RPS60 相对强势
- `rps_120_strength`：RPS120 长期强势
- `rps_60_weakness`：RPS60 相对弱势风险

已实现：

- 当前因子信号扫描
- 单因子回测
- 多周期因子矩阵
- 因子有效性评分
- 因子历史结论：有效、观察、中性、反向、样本不足

公式思路参考库：

```text
https://www.gupang.com/
```

原则：只借鉴逻辑清晰、可解释、可复现、无未来函数风险的公式思路，不直接迷信公式名称。

### 策略回测

已实现组合策略回测：

- 每个交易日只使用当日及以前的数据选股。
- 按公式型因子信号打分。
- 每日选 Top N。
- 持有指定交易日数。
- 统计未来收益。

当前支持指标：

- 交易数
- 交易日数
- 胜率
- 平均收益
- 组合日均收益
- 最大回撤
- 收益波动
- 盈亏比
- 风险收益指标
- 最好单笔
- 最差单笔
- 组合权益曲线
- 回撤曲线
- 每日组合平均收益
- 交易样本明细
- 次日开盘成交或信号日收盘成交两种假设
- Web 回测页会仅列出本机已导入日线的上证指数、上证50、沪深300、深证成指、创业板指和科创50作为基准，仍兼容手动代码
- 默认不重叠持仓，并保留每日重叠批次作为研究对照
- 无成交量或缺失价格的入场、出场日自动跳过
- 按实际买入日统计年度和月度独立批次表现
- 滚动样本外验证：训练期因子质量与测试期交易日期隔离
- 策略准入评估：按样本外折数、交易数、收益、回撤和基准超额给出“样本不足、未通过、观察、通过”，并保存每次验证的参数、数据版本和结果
- 普通策略回测可保存参数、结果摘要、完整结果与日线版本指纹，并在 Web/命令行查看历史记录

最近一次保守样本外验证（2026-07-13，已保存为 `strategy_validation_runs` 第 1 条）：

```text
train_days=200
test_days=50
folds=3
limit_symbols=30
execution=next_open
position_mode=non_overlapping
cost_bps=5
slippage_bps=5
benchmark=sh000300
trades=190
portfolio_avg=-0.26%
positive_folds=0/3
max_dd=-14.11%
benchmark_avg=0.18%
benchmark_excess=-0.44%
verdict=未通过
```

说明：当前策略在这组独立样本外窗口中未达到准入门槛，不能作为有效策略使用；后续优化必须重新运行独立验证。当前已计入可配置手续费和滑点，并默认使用“信号日收盘生成信号、下一交易日开盘买入、持有期后收盘卖出”的成交方式。接近标准涨跌幅且开高低收相同的一字涨跌停会跳过；尚未计入 ST 历史涨跌幅、复权处理、持仓规模和完整停牌规则等约束。

### 日线数据质量

本机已导入约 430 万根通达信日线；可选股票 5,246 个，已识别指数 761 个，股票/指数源文件目前到 2026-07-08。

- 当前 `data-health` 检查结果为 `clean`，没有同代码同日期的来源冲突。
- `data-health` 的时效以可选股票最新日线为准；截至 2026-07-13，最新股票日线为 2026-07-08，显示约 3 个工作日滞后。`tdx-status` 也确认下载源股票/指数均只到 2026-07-08，因此不是数据库导入漏数。
- `run-daily` 对已有通达信日线的标的只更新实时行情、评分和报告，不再追加腾讯前复权日线；通达信更新仍通过 `import-tdx-history` 执行，避免混合复权口径。
- `run-daily --tdx-root D:\new_tdx` 可在每日流程开始前同步通达信日线；首次导入全历史，后续默认从已入库 TDX 最新日期做重叠增量同步，`--tdx-include-indices` 可额外同步已识别指数。
- 每次成功的 `run-daily` 都会把当时的日线健康、最新股票日线、实时行情时效快照和轻量审计导出表保存到 `daily_runs`；Web 的“每日记录”会直接显示，旧记录没有快照时显示 `-`。日常流程不会重复导出数百万行 `daily_bars`，完整原始日线仅在手动导出时生成。
- `import-tdx-history --include-indices` 仅导入沪市 `000/880/899` 和深市 `399` 开头的已识别指数，不将债券等其它 `.day` 文件误作指数；可先执行 `tdx-status --tdx-root D:\new_tdx` 只读查看源文件时效。
- 若未来同日存在多个来源，读取层固定按 `TDX 未复权 > CSV/其他 > 腾讯前复权` 选择唯一规范来源，绝不平均不同复权口径。
- CLI `data-health` 与 Web `/data-health` 已统一展示日线来源/时效、实时行情时效（总覆盖、近一工作日覆盖、过期数量和最近价格日期）、按公告日边界统计的财务披露覆盖，以及当前评分覆盖的行业标签。2026-07-14 批量公开财报导入后为 80 只已披露股票、80 只现金流覆盖、623 条现金流记录。
- `data-health` 的日线覆盖与冲突摘要按日线数据版本和检查日期持久缓存；实际 430 万根日线首次统计约 7.84 秒，缓存命中约 0.28 秒，新日线导入后会自动重算。
- 当股票日线按工作日估算为滞后时，总览、AI 选股、因子验证和策略回测会显示“日线时效提醒”，避免在常用研究页面忽略数据更新状态。

### 本地主题数据

- 已导入本机通达信概念/风格成员关系 29,257 条，其中概念 165 个、风格 103 个。
- `themes` 和 Web `/themes` 按最新评分批次汇总成员数、评分覆盖数、覆盖率、平均分和正分占比，并按最近 1/5/20 个本地交易日的成分股规范收盘价计算等权收益与有效价格覆盖；默认至少需要 3 只成分股进入当前评分批次，避免单一股票偶然高分主导排序。
- 主题价格表现使用本地成分股等权汇总，不等同于官方主题指数；结果按日线、主题成员和规范来源版本缓存，日线同步或主题重导后会自动重算。
- `import-public-industries --universe auto --limit 100` 会逐只补充东财三级行业，按成功记录增量写入，不会因单个网络失败删除已有归属。批量更新会优先覆盖缺失标签，再按最旧更新时间刷新已有标签，避免反复请求同一批代码。`industries` 和 Web `/industries` 会按最新评分聚合行业覆盖、平均分和正分占比；行业不参与历史因子、策略回测或样本外验证。
- `run-daily --tdx-root D:\new_tdx --tdx-import-themes` 会在日线同步后更新主题缓存，并把导入数量保存到每日运行记录。
- `run-daily --public-fundamentals --public-fundamental-limit 100 --public-fundamental-reports 8` 可在公开日线后按当前股票池抓取财报；这是显式开关，默认最多请求 100 只股票，单个代码网络失败只记录在每日运行中，不阻断行情、评分和报告。`--public-industries --public-industry-limit 100` 可按同一股票池更新当前公开三级行业。Web `/daily-runs` 会显示公开财报和公开行业的请求数、导入量与失败数。
- `run-daily` 的自动 AI 快照要求日线与实时行情时效都为 `current`，显式 `--strategy-snapshot` 要求日线时效为 `current`；任一前提不满足都会明确记为未保存，避免历史复盘和策略记录混入伪当日信号。手动 `ai-pick --save` 仍可用于历史研究。策略快照按固定保守口径保存：持有 5 日、Top 10、因子分至少 60、最多 300 个标的和 260 根日线、单边成本与滑点各 5bps、沪深 300 基准、次日开盘成交且不重叠持仓。快照异常也只写入每日记录而不阻断数据更新。Web `/daily-runs` 和 CLI `daily-runs` 会显示快照状态、原因、保存回测编号和交易样本数。
- 2026-07-14 已实机运行 `run-daily --limit 5 --public-industries --public-industry-limit 5`：5 个行业标签全部导入，`stock_industries.csv`、候选池、日报和 AI 快照均已生成，运行记录为成功。
- `report` 与每日生成的 `daily_report.md` 会在至少两只当前评分股票拥有相同行业归属时，列出最多五个行业热度条目；该栏目仅用于观察候选池行业集中度，不参与候选评分或回测。

### 财务数据

- `import-fundamentals` 支持中英文常见列名和 `YYYYMMDD`/`YYYY-MM-DD` 报告期、公告日期，最新报告期会显示在个股详情。
- `import-public-fundamentals 000001 --reports 8` 已接入公开东财财报接口；也可用 `--universe auto --limit 100` 批量抓取当前股票池。
- 公开源映射已核验的报告期、公告日期、营业收入及其同比、归母净利润及其同比、加权 ROE 和经营活动现金流；现金流表只取 `NETCASH_OPERATE` 金额，公告日期仍以主财报为准。PE TTM、PB 仍可由 CSV 导入。
- 原始 CSV 金额与比率单位、以及公开接口原始值不会被自动转换；相同报告期优先显示字段更完整的记录。
- 已加入“已披露盈利质量”“已披露增长质量”和“已披露现金流质量”因子：前者要求公告日期存在、加权 ROE 不低于 8%、营收和归母净利润为正；增长因子要求源数据中的营收同比和归母净利同比均不为负；现金流因子要求经营活动现金流为正且不低于归母净利的 80%。三者都从公告日后的下一个交易日才进入扫描、因子回测和策略回测；无公告日期的 CSV 仍仅用于展示与人工复核。
- 因子扫描与回测缓存的指纹已纳入财务数据版本，更新财务记录后会自动失效并重建。

### Web 页面

当前页面：

- `/`：总览工作台
- `/ths`：同花顺数据源
- `/news`：资讯
- `/factors`：因子验证
- `/factors/{factor_id}`：单因子逻辑、风险边界、当前命中和多周期历史表现
- `/themes`：通达信本地概念/风格的评分热度、有效价格覆盖和等权价格表现
- `/industries`：公开三级行业的当前评分汇总
- `/backtest`：策略回测
- `/strategy-backtest-runs`：已保存策略回测记录
- `/strategy-validation`：已保存的样本外验证结论
- `/daily-runs`：每日数据更新流水线记录
- `/data-health`：日线来源、冲突、时效、财务披露和当前行业标签覆盖
- `/ai`：AI 选股
- `/ai/history`：AI 历史
- `/ai/changes`：AI 观点变化
- `/ai/outcomes`：AI 观点后续表现复盘
- `/notes`：观察池
- `/symbol/{code}`：个股详情

界面已经优化为“工作台”结构：

1. 数据更新
2. AI 选股
3. 因子验证
4. 策略回测
5. 观察复盘

各核心页面都有用途说明和下一步入口。

若日线时效滞后，总览、AI 选股、因子验证和策略回测会直接显示更新提醒，并可跳转至日线健康页复查。

## 常用命令

启动 Web：

```powershell
python -m ths_stock_picker --db work\ths_stock_picker.db --ths-root D:\同花顺软件\同花顺 serve --host 127.0.0.1 --port 8765
```

检查同花顺状态：

```powershell
python -m ths_stock_picker --db work\ths_stock_picker.db --ths-root D:\同花顺软件\同花顺 ths-monitor
```

导入同花顺资讯：

```powershell
python -m ths_stock_picker --db work\ths_stock_picker.db import-ths-news --limit-per-file 300
```

AI 选股：

```powershell
python -m ths_stock_picker --db work\ths_stock_picker.db ai-pick --limit 10 --min-score 20 --save
```

因子矩阵：

```powershell
python -m ths_stock_picker --db work\ths_stock_picker.db factor-matrix --horizons 3,5,10 --limit-symbols 300
```

策略回测：

```powershell
python -m ths_stock_picker --db work\ths_stock_picker.db strategy-backtest --horizon 5 --top-n 10 --min-signal-score 60 --limit-symbols 300
```

策略样本外验证：

```powershell
python -m ths_stock_picker --db work\ths_stock_picker.db strategy-validate --train-days 200 --test-days 50 --max-folds 3 --horizon 5 --top-n 10 --min-signal-score 60 --limit-symbols 30 --cost-bps 5 --slippage-bps 5 --benchmark-symbol sh000300
python -m ths_stock_picker --db work\ths_stock_picker.db strategy-validation-runs --limit 20
```

检查通达信源文件时效：

```powershell
python -m ths_stock_picker --db work\ths_stock_picker.db tdx-status --tdx-root D:\new_tdx
python -m ths_stock_picker --db work\ths_stock_picker.db import-tdx-blocks --tdx-root D:\new_tdx
python -m ths_stock_picker --db work\ths_stock_picker.db themes --min-scored 3 --limit 30
python -m ths_stock_picker --db work\ths_stock_picker.db import-fundamentals path\to\fundamentals.csv
python -m ths_stock_picker --db work\ths_stock_picker.db import-public-fundamentals 000001 --reports 8
python -m ths_stock_picker --db work\ths_stock_picker.db import-public-fundamentals --universe auto --limit 100 --reports 8
python -m ths_stock_picker --db work\ths_stock_picker.db import-public-industries --universe auto --limit 100
python -m ths_stock_picker --db work\ths_stock_picker.db industries --min-scored 3 --limit 30
python -m ths_stock_picker --db work\ths_stock_picker.db run-daily --limit 100 --public-fundamentals --public-fundamental-limit 100 --public-fundamental-reports 8 --public-industries --public-industry-limit 100 --out-dir outputs
```

每日运行记录：

```powershell
python -m ths_stock_picker --db work\ths_stock_picker.db run-daily --limit 200 --history-days 80 --tdx-root D:\new_tdx --tdx-include-indices --tdx-import-themes --out-dir outputs
python -m ths_stock_picker --db work\ths_stock_picker.db run-daily --limit 200 --history-days 80 --tdx-root D:\new_tdx --tdx-include-indices --tdx-import-themes --public-announcements --strategy-snapshot --out-dir outputs
python -m ths_stock_picker --db work\ths_stock_picker.db daily-runs --limit 20
```

运行测试：

```powershell
python -m unittest discover -s tests
```

当前测试数量：

```text
113 tests
```

最近一次测试结果：通过。

## 当前运行状态

最近一次本地服务已启动：

```text
http://127.0.0.1:8765/
```

最近 PID 可能会变化，必要时用端口检查：

```powershell
Get-NetTCPConnection -LocalPort 8765 -State Listen
```

重启服务常用命令：

```powershell
$connections = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
foreach ($c in $connections) { Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 500
Start-Process -FilePath python -ArgumentList @('-m','ths_stock_picker','--db','work\ths_stock_picker.db','--ths-root','D:\同花顺软件\同花顺','serve','--host','127.0.0.1','--port','8765') -WorkingDirectory 'C:\Users\Administrator\Desktop\Workspace\A' -WindowStyle Hidden
```

## 继续开发前

继续开发前建议先同步并运行：

```powershell
git pull --rebase
git status --short
python -m unittest discover -s tests
```

## 后续交付标准

建议按以下顺序继续：

1. 扩展历史日线到至少 1-3 年，提高回测可信度。
2. 回测加入交易成本、滑点、停牌、涨跌停不可成交约束。
3. 加入收益曲线的年度/月度统计。
4. 加入指数基准对比，例如上证指数、沪深 300、创业板指、科创 50。
5. 继续扩展财务质量因子，例如估值；已披露现金流质量已加入，继续以公告日期为边界做样本外验证。
6. 完善行业覆盖与稳定性；当前已接入公开三级行业、行业评分汇总，以及通达信概念/风格成员、评分热度和本地成分股等权价格表现。
7. 扩展因子库：MACD、KDJ、RSI、平台突破、涨停回踩、相对强弱。
8. 因子详情页：展示因子解释、公式来源、未来函数风险、样本表现。
9. AI 选股输出“触发条件”和“失效条件”。
10. 每日一键更新流程：数据、新闻、评分、AI 快照、报告已实现；显式传入 `--strategy-snapshot` 时会额外保存可复核回测记录，AI 或回测快照异常均不阻断数据更新。
11. 根据已保存的策略快照继续积累多市场状态样本，并仅在新的独立样本外验证通过后讨论策略参数调整。
12. GitHub Actions 自动测试：已补充 Python 3.11/3.12 的语法检查和单元测试工作流。
13. README 增加截图和快速启动。

## 安全边界

必须继续遵守：

- 不接实盘交易。
- 不调用下单。
- 不读取 cookie、账号、交易敏感文件。
- 不破解加密数据。
- 不绕过登录。
- 不输出确定性投资承诺。
- 所有 AI 和回测结论都标记为投研辅助，不构成投资建议。

## Token 安全提醒

之前为了同步 GitHub，用户在对话里提供过 GitHub token。后续不要把 token 写入文件、日志或 git remote URL。建议用户在 GitHub 设置中删除或轮换已暴露 token。
