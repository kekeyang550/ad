# A 股 AI 选股项目交接记录

更新时间：2026-07-13

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

注意：GitHub 首次同步已通过 GitHub API 完成；本机 `git push` 曾出现 GitHub 443 连接失败。后续如需同步，优先尝试普通 `git push`，若仍失败再考虑 GitHub API 或配置网络。

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
- 解析同花顺本地资讯缓存，例如 `实时解盘.xml`。
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
- 可执行 `python -m ths_stock_picker data-health` 查看来源覆盖和冲突数量。
- 当股票日线按工作日估算为滞后时，总览、AI 选股、因子验证和策略回测会显示“日线时效提醒”，避免在常用研究页面忽略数据更新状态。

### Web 页面

当前页面：

- `/`：总览工作台
- `/ths`：同花顺数据源
- `/news`：资讯
- `/factors`：因子验证
- `/factors/{factor_id}`：单因子逻辑、风险边界、当前命中和多周期历史表现
- `/backtest`：策略回测
- `/strategy-backtest-runs`：已保存策略回测记录
- `/strategy-validation`：已保存的样本外验证结论
- `/daily-runs`：每日数据更新流水线记录
- `/data-health`：日线来源、冲突与时效检查
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
```

每日运行记录：

```powershell
python -m ths_stock_picker --db work\ths_stock_picker.db run-daily --limit 200 --history-days 80 --tdx-root D:\new_tdx --tdx-include-indices --out-dir outputs
python -m ths_stock_picker --db work\ths_stock_picker.db daily-runs --limit 20
```

运行测试：

```powershell
python -m unittest discover -s tests
```

当前测试数量：

```text
77 tests
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

## 当前未提交改动

最近一轮开发后，本地有改动：

- `README.md`
- `tests/test_storage_cli.py`
- `ths_stock_picker/cli.py`
- `ths_stock_picker/storage.py`
- `ths_stock_picker/web_panel.py`

这些改动包括：

- 回测风险指标
- 最大回撤
- 权益曲线
- 回撤曲线
- 日线来源规范化与健康检查
- 次日开盘成交与不重叠持仓回测
- AI 榜单一键加入观察池
- 页面工作台导航和使用引导

继续开发前建议先运行：

```powershell
git status --short
python -m unittest discover -s tests
```

## 后续交付标准

建议按以下顺序继续：

1. 扩展历史日线到至少 1-3 年，提高回测可信度。
2. 回测加入交易成本、滑点、停牌、涨跌停不可成交约束。
3. 加入收益曲线的年度/月度统计。
4. 加入指数基准对比，例如上证指数、沪深 300、创业板指、科创 50。
5. 接入财务数据：营收、净利润、ROE、现金流、估值。
6. 接入行业/概念数据，形成行业热度和主题强度。
7. 扩展因子库：MACD、KDJ、RSI、平台突破、涨停回踩、相对强弱。
8. 因子详情页：展示因子解释、公式来源、未来函数风险、样本表现。
9. AI 选股输出“触发条件”和“失效条件”。
10. 每日一键更新流程：数据、新闻、评分、AI 快照、回测、报告；AI 快照异常不阻断数据更新。
11. 自动保存每日策略和回测结果。
12. GitHub Actions 自动测试。
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
