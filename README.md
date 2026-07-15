# A 股选股项目：同花顺本地缓存解析优先版

[![Tests](https://github.com/kekeyang550/ad/actions/workflows/tests.yml/badge.svg)](https://github.com/kekeyang550/ad/actions/workflows/tests.yml)

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
- 可只读导入通达信本地概念/风格板块成员，并汇总主题评分热度与成分股等权价格表现
- 可从公开公司概况补充当前三级行业归属，并按最新评分批次汇总行业热度
- 基于实时行情、流动性、市值、换手率、板块和日线趋势生成规则评分
- 借鉴选股公式思路，转换成可解释因子，并用真实日线做简易回测
- 基于评分、行情、日线趋势、公式型因子、同花顺本地新闻和本地备注生成 AI 辅助选股观点
- 提供命令行状态、导入、评分、数据库概览

## 使用

```powershell
python -m ths_stock_picker status
python -m ths_stock_picker ths-monitor
python -m ths_stock_picker import-ths-news
python -m ths_stock_picker import-public-announcements 000538 --per-symbol 3
python -m ths_stock_picker import-public-announcements --universe auto --limit 30 --per-symbol 3
python -m ths_stock_picker reclassify-news
python -m ths_stock_picker news --limit 20
python -m ths_stock_picker news --tag AI算力
python -m ths_stock_picker factors
python -m ths_stock_picker factor-scan --limit 20
python -m ths_stock_picker refresh-factor-scan-cache --limit 20
python -m ths_stock_picker factor-backtest --horizon 5 --limit-symbols 300
python -m ths_stock_picker factor-matrix --horizons 3,5,10 --limit-symbols 300 --max-bars 260
python -m ths_stock_picker refresh-factor-cache --horizons 3,5,10 --limit-symbols 300 --max-bars 260
python -m ths_stock_picker strategy-backtest --horizon 5 --top-n 10 --min-signal-score 60 --limit-symbols 300 --cost-bps 5 --slippage-bps 5 --benchmark-symbol sh000300 --max-bars 260 --execution next_open --position-mode non_overlapping --save
python -m ths_stock_picker strategy-backtest-runs --limit 20
python -m ths_stock_picker strategy-walkforward --train-days 252 --test-days 63 --max-folds 4 --horizon 5 --top-n 10 --min-signal-score 60 --limit-symbols 100 --cost-bps 5 --slippage-bps 5 --benchmark-symbol sh000300
python -m ths_stock_picker strategy-validate --train-days 252 --test-days 63 --max-folds 4 --horizon 5 --top-n 10 --min-signal-score 60 --limit-symbols 100 --cost-bps 5 --slippage-bps 5 --benchmark-symbol sh000300
python -m ths_stock_picker strategy-validation-runs --limit 20
python -m ths_stock_picker import-tdx-history --tdx-root D:\new_tdx --include-indices --replace-existing --limit-symbols 300
python -m ths_stock_picker tdx-status --tdx-root D:\new_tdx
python -m ths_stock_picker import-tdx-blocks --tdx-root D:\new_tdx
python -m ths_stock_picker themes --min-scored 3 --limit 30
python -m ths_stock_picker import
python -m ths_stock_picker score
python -m ths_stock_picker score --profile configs\scoring.default.json
python -m ths_stock_picker score-runs --limit 10
python -m ths_stock_picker compare-runs --limit 20
python -m ths_stock_picker write-default-profile --out configs\scoring.default.json
python -m ths_stock_picker db-info
python -m ths_stock_picker data-health
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
python -m ths_stock_picker run-daily --limit 200 --history-days 80 --profile configs\scoring.default.json --tdx-root D:\new_tdx --tdx-include-indices --tdx-import-themes --public-announcements --public-announcement-limit 30 --out-dir outputs
python -m ths_stock_picker run-daily --limit 100 --history-days 80 --public-fundamentals --public-fundamental-limit 100 --public-fundamental-reports 8 --public-industries --public-industry-limit 100 --out-dir outputs
python -m ths_stock_picker daily-runs --limit 20
python -m ths_stock_picker import-history path\to\daily.csv
python -m ths_stock_picker import-fundamentals path\to\fundamentals.csv
python -m ths_stock_picker import-public-fundamentals 600000 000001 --reports 8
python -m ths_stock_picker import-public-fundamentals --universe auto --limit 100 --reports 8
python -m ths_stock_picker import-public-industries --universe auto --limit 100
python -m ths_stock_picker industries --min-scored 3 --limit 30
python -m ths_stock_picker score
python -m ths_stock_picker scores --limit 20
python -m ths_stock_picker scores --limit 20 --positive-only
python -m ths_stock_picker score-runs --limit 10
python -m ths_stock_picker compare-runs --limit 20
python -m ths_stock_picker explain 600000 --bars 8
python -m ths_stock_picker diagnose 600000 --bars 8
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

GitHub Actions 会在推送到 `main` 或创建 Pull Request 时，使用 Python 3.11 和 3.12 执行语法检查与单元测试。

## 设计边界

- 本项目仅用于个人投研和选股辅助，不提供投资建议。
- 不接实盘交易，不调用 `xiadan.exe`。
- 不抓包、不绕过登录、不破解加密数据。
- 财务和历史行情若普通客户端没有自然落地缓存，可通过导出文件或已接入的公开数据源补齐；公开财报当前只导入经过字段核验的少数指标。
- `stocknow.dat` 当前只解析代码、记录边界和诊断信息；价格等字段需要用界面数据对照确认后再启用。

## 字段反推工作流

1. 用 `capture-symbols` 保存几只股票的原始 546 字节记录。
2. 用 `observation-template` 生成 CSV 模板。
3. 打开同花顺，填入界面显示的现价、涨跌幅、成交量、成交额等字段。
4. 用 `match-observations` 自动筛选可能的字段偏移和编码方式。

也可以跳过人工填写，使用 `auto-infer-fields` 自动抓取公开行情作为观测值。如果同花顺本地缓存没有可匹配的价格字段，使用 `import-public-quotes` 将公开实时行情作为价格补充源写入 `quotes_realtime`，同花顺本地缓存继续负责证券池、名称、市场和诊断。本地仅代码缓存的重新导入会保留上一批公开价格，避免把可评分行情清空；公开接口临时返回空或部分结果时，也只更新成功返回的代码，保留其余报价等待下次刷新。公开行情补充源当前会写入现价、涨跌幅、成交量、成交额、总市值、流通市值、换手率和板块分类。

## 每日更新

推荐日常使用：

```powershell
python -m ths_stock_picker run-daily --limit 200 --history-days 80 --profile configs\scoring.default.json --tdx-root D:\new_tdx --tdx-include-indices --tdx-import-themes --out-dir outputs
python -m ths_stock_picker run-daily --limit 200 --history-days 80 --tdx-root D:\new_tdx --tdx-include-indices --tdx-import-themes --public-announcements --strategy-snapshot --out-dir outputs
python -m ths_stock_picker daily-runs --limit 20
python -m ths_stock_picker scores --limit 20 --positive-only
python -m ths_stock_picker explain 600000 --bars 8
```

`run-daily` 会依次执行同花顺本地缓存导入、公开实时行情补齐、公开日线补齐、评分、AI 观点快照、轻量审计 CSV、候选池导出和 Markdown 日报生成。传入 `--tdx-root` 时，会在开始处同步本地通达信日线；首次同步导入全历史，后续默认从已入库 TDX 最新日期开始重叠增量同步，可用 `--tdx-start-date` 覆盖。`--tdx-include-indices` 只会额外同步已识别指数；`--tdx-import-themes` 会在日线后同步通达信本地概念和风格成员。传入显式开关 `--public-fundamentals` 后，会按当前股票池抓取公开财报，`--public-fundamental-limit` 默认最多请求 100 只股票，`--public-fundamental-reports` 控制每只股票的报告期数量；公开源会合并已核验的营收、归母净利、同比、加权 ROE 与经营活动现金流，公告日期统一取主财报以保证回测可见性边界。传入 `--public-industries` 后会按同一股票池抓取当前公开三级行业，`--public-industry-limit` 默认最多 100 只；行业只用于当前研究展示，不进入历史因子和回测。`--public-announcements` 会补齐公告资讯；自动 AI 快照要求日线和实时行情时效均为 `current`，`--strategy-snapshot` 要求日线时效为 `current`；任一前提不满足会明确留痕为未保存，手动 `ai-pick --save` 仍可用于历史研究。策略快照使用固定保守参数：持有 5 日、Top 10、因子分至少 60、最多 300 个标的和 260 根日线、单边成本与滑点各 5bps、沪深 300 基准、次日开盘成交且不重叠持仓。策略快照只用于持续研究，参数调整请使用独立的 `strategy-backtest` 命令；快照异常和 AI 快照异常一样不会阻断数据更新。单个代码网络失败会记录在本次运行中，但不会阻断行情、评分和报告。公共日线补齐会自动跳过已有通达信日线的标的，避免混入腾讯前复权价格造成同日双来源冲突。每日流程不再重复导出全部原始 `daily_bars`；需要完整日线 CSV 时可单独执行 `export --table daily_bars`。每次运行都会写入 `daily_runs`：成功记录会保存 TDX 同步量、主题同步量、公开财报与行业的请求股票数、导入量和失败数、日线导入量、通达信已覆盖数量、股票池数量、输出文件和表计数，以及当次日线、行情时效、财务披露覆盖和快照状态；失败记录会保存停止步骤、返回码或异常原因。可用 `daily-runs` 或 Web 的 `/daily-runs` 查看最近运行，不必依赖终端滚动日志。

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

浏览器打开 `http://127.0.0.1:8765/`，可以查看数据表计数、候选池、评分榜、批次变化和同花顺缓存解析诊断。首页支持按代码/名称搜索、按板块筛选、设置最低分、排序和导出当前候选 CSV；`http://127.0.0.1:8765/diagnose` 可输入 6 位代码做一键诊股，汇总评分、AI 观点、触发条件、失效条件、本地备注、近期日线和数据覆盖状态；`http://127.0.0.1:8765/daily-runs` 可复查每日数据更新流水线的成功或失败记录；`http://127.0.0.1:8765/ths` 可查看同花顺进程和 A 股实时缓存活跃度；`http://127.0.0.1:8765/data-health` 可查看日线来源、覆盖范围、同日冲突、按公告日边界统计的财务披露覆盖和公开行业标签；`http://127.0.0.1:8765/themes` 可按当前评分批次查看通达信本地概念/风格的成员覆盖、平均分、正分占比，以及按最近 1/5/20 个本地交易日计算的成分股等权表现与有效价格覆盖；`http://127.0.0.1:8765/industries` 可查看公开三级行业的成员覆盖、平均分和正分占比；`http://127.0.0.1:8765/news` 可查看同花顺本地新闻缓存；`http://127.0.0.1:8765/factors` 可查看公式型因子定义、当前命中和历史回测，`http://127.0.0.1:8765/factors/{factor_id}` 可查看单因子的逻辑、来源、未来函数风险、当前命中与多周期表现；`http://127.0.0.1:8765/backtest` 可用真实日线做组合策略回测并保存本次结果；`http://127.0.0.1:8765/strategy-backtest-runs` 可复查已保存的回测参数、摘要和日线版本；`http://127.0.0.1:8765/strategy-validation` 可复查已保存的滚动样本外结论、参数和数据版本；`http://127.0.0.1:8765/ai` 会生成 AI 辅助选股榜，并可一键保存本次榜单；`http://127.0.0.1:8765/ai/history` 可查看已保存 AI 历史观点；`http://127.0.0.1:8765/ai/changes` 可查看最近两次 AI 观点的变化；`http://127.0.0.1:8765/ai/outcomes` 可复盘已保存 AI 观点的后续表现；个股详情页包含 AI 观点、概念与风格、行业归属、相关新闻、本地观察记录、分项分、触发规则、最近日线表和轻量走势 SVG 图。

`data-health` 同时汇总按公告日边界统计的财务披露覆盖和当前评分覆盖的公开行业标签；行业统计只用于当前研究展示，不进入历史回测。

当最新股票日线按工作日估算存在明显滞后时，总览、AI 选股、因子验证和策略回测会显示“日线时效提醒”，并链接到日线健康页；提示仅作更新状态判断，不包含交易所节假日和盘中状态。

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
- MACD 零轴上金叉：DIF 在零轴上向上穿越 DEA，识别趋势内动量再次增强。
- RSI14 回升确认：RSI14 自弱势区上穿 45，且收盘仍在 MA20 上方。
- KDJ 低位金叉：K 线上穿 D 线且处于低位，同时价格接近 MA20，识别趋势内修复。
- 涨停后缩量回踩：近期出现约 10% 强势上涨后，缩量回到 MA10 附近且仍守住 MA20。
- 平台整理突破：近 20 日价格收敛后突破区间高点，且量能不弱于近 5 日均量。
- 追高过热风险：近 5 日涨幅过大或长上影放量。

命令行：

```powershell
python -m ths_stock_picker factors
python -m ths_stock_picker factor-scan --limit 20
python -m ths_stock_picker refresh-factor-scan-cache --limit 20
python -m ths_stock_picker factor-backtest --horizon 5 --limit-symbols 300
python -m ths_stock_picker factor-matrix --horizons 3,5,10 --limit-symbols 300 --max-bars 260
python -m ths_stock_picker refresh-factor-cache --horizons 3,5,10 --limit-symbols 300 --max-bars 260
python -m ths_stock_picker strategy-backtest --horizon 5 --top-n 10 --min-signal-score 60 --limit-symbols 300 --cost-bps 5 --slippage-bps 5 --benchmark-symbol sh000300 --max-bars 260 --execution next_open --position-mode non_overlapping --save
python -m ths_stock_picker strategy-backtest-runs --limit 20
python -m ths_stock_picker strategy-walkforward --train-days 252 --test-days 63 --max-folds 4 --horizon 5 --top-n 10 --min-signal-score 60 --limit-symbols 100 --cost-bps 5 --slippage-bps 5 --benchmark-symbol sh000300
python -m ths_stock_picker strategy-validate --train-days 252 --test-days 63 --max-folds 4 --horizon 5 --top-n 10 --min-signal-score 60 --limit-symbols 100 --cost-bps 5 --slippage-bps 5 --benchmark-symbol sh000300
python -m ths_stock_picker strategy-validation-runs --limit 20
python -m ths_stock_picker import-tdx-history --tdx-root D:\new_tdx --include-indices --replace-existing --limit-symbols 300
python -m ths_stock_picker tdx-status --tdx-root D:\new_tdx
```

这些因子会作为 AI 选股的证据之一，但不会单独决定买卖。后续可以从 `https://www.gupang.com/` 等公式资料库继续挑选逻辑清晰的公式，转换为因子后再加入回测。

`factor-scan` 和 Web 因子页会复用 `factor_scan_cache`，缓存键包含实际候选股票池和显示条数；`factor-matrix` 和 AI 因子质量评估会复用 `factor_backtest_cache`，缓存键包含回测周期、股票池规模和 K 线窗口。两类缓存均校验轻量的日线版本、财务数据版本和因子引擎版本；导入或删除日线、更新财务数据、或升级因子规则时都会自动失效，因此无需扫描全表也能在通达信或财报数据更新后立即重建。导入大量数据后可先运行 `refresh-factor-scan-cache` 与 `refresh-factor-cache` 预热，减少首次打开页面的等待。

`strategy-backtest` 是组合策略研究回测：每天只根据当日及以前的日线因子信号选股，按信号分选 Top N，持有指定交易日后统计收益。默认 `--execution next_open`，即信号日收盘生成信号、下一交易日开盘买入、持有指定交易日后按收盘卖出；可改为 `signal_close` 做假设对照。默认 `--position-mode non_overlapping`，上一批到期后才允许下一批入场；`daily_batches` 会允许批次重叠，只用于研究信号表现。无成交量或缺失价格的入场、出场日会跳过；对接近标准涨跌幅、且开高低收相同的一字涨跌停，也会保守地跳过。结果会按实际买入日提供年度、月度独立批次表现，帮助识别收益是否集中在少数时段。可用 `--cost-bps` 和 `--slippage-bps` 设置单边交易成本和滑点，结果会同时保留毛收益并统计扣减后的净收益；可用 `--benchmark-symbol` 指定已导入日线的指数或基准代码，例如 `sh000300`；Web 回测页会仅列出本机已导入的上证指数、上证50、沪深300、深证成指、创业板指和科创50，并保留自定义基准代码的兼容项。`--max-bars` 控制每只股票使用最近多少根 K 线，传 `0` 可跑全历史。传入 `--save` 或在 Web 回测页点击“保存本次回测”会保存参数、结果摘要、完整结果和日线版本指纹，可用 `strategy-backtest-runs` 或 `/strategy-backtest-runs` 复核，并可点开单条记录查看保存时的权益曲线、交易样本和期间统计。当前版本仍未计入 ST 历史涨跌幅、复权处理和完整停牌规则等约束，结果用于筛选和迭代策略，不作为交易建议。

`strategy-walkforward` 会把历史切成固定长度的训练窗口和紧随其后的独立测试窗口。每一折的因子有效性只使用训练截止日以前的日线，测试期只生成测试区间的信号和交易；输出每折训练/测试日期、独立批次数、交易数、净均收和最大回撤。样本外结果用于否定或保留策略假设，不应只挑选表现较好的折。

`strategy-validate` 会在滚动样本外验证后应用固定的准入门槛，并自动把参数、日线版本指纹、每折结果和结论保存到 SQLite。结论分为“样本不足、未通过、观察、通过”：至少需要指定折数和交易数，样本外组合收益必须为正、正收益折占比和回撤达到门槛；已提供且完整覆盖的基准还必须有正超额。基准缺失或覆盖不完整时最多为“观察”，不会误标为“通过”。默认门槛可用 `--min-folds`、`--min-trades`、`--min-positive-fold-ratio`、`--max-drawdown` 和 `--min-benchmark-excess-return` 调整。`strategy-validation-runs` 用于复查历史结论；日线更新后版本指纹会变化，旧结论不会被当作新数据上的验证结果。

`import-tdx-history` 会只读解析本机通达信 `vipdoc/sh/lday` 和 `vipdoc/sz/lday` 下的 `.day` 日线文件，导入现有 `daily_bars` 表。`.day` 是通达信未复权日线；读取日线时系统按 `TDX 未复权 > CSV/其他 > 腾讯前复权` 选择唯一规范来源，绝不对不同来源的同日价格求平均。`--include-indices` 只会加入沪市 `000/880/899` 和深市 `399` 开头的已识别指数，不会把债券等其它 `.day` 文件误作指数。`tdx-status` 会只读扫描股票与指数文件的末条日期；先用它确认下载源更新，再运行 `import-tdx-history`。`data-health` 会列出数据源覆盖、同代码同日期冲突和按股票日线计算的工作日时效；时效只是近似提醒，不包含节假日和盘中状态。即使有优先级，仍建议将通达信作为主数据源时使用 `--replace-existing`，保持回测口径单一。

`data-health` 的日线覆盖与冲突统计会按日线数据版本和检查日期持久缓存；重复浏览不会反复扫描全量日线，导入新日线后缓存自动失效并重算。

`import-tdx-blocks` 会只读解析 `T0002/hq_cache/block_gn.dat`（概念）和 `block_fg.dat`（风格）的固定记录结构，只接受 GBK 主题名、有效成员数量和连续 6 位代码都能验证的记录。解析结果写入 `stock_themes`，同一源文件再次导入会替换旧成员，不读取账号或交易数据。`themes --min-scored 3` 和 Web `/themes` 会按最新评分批次汇总成员数、评分覆盖数、覆盖率、平均分和正分占比，并按最近 1/5/20 个本地交易日端点的规范收盘价计算成分股等权收益；没有两个端点收盘价的成分会从对应周期排除并显示有效数。主题价格汇总会按日线、主题成员与规范来源版本缓存，导入日线或重导主题后自动重建。这些指标不是官方板块指数收益，也不构成投资建议。

`import-public-industries` 会按指定代码或当前股票池逐只读取公开公司概况中的东财三级行业，默认最多 100 只；批量任务会优先补齐缺失归属，再按最旧更新时间轮换刷新已有记录，避免反复请求同一批代码。单个网络失败只保留失败记录，不会删除已有行业归属。`industries` 和 Web `/industries` 按最新评分批次汇总当前行业的成员数、评分覆盖、平均分与正分占比。行业标签会随公开数据源变化，只作为当前研究上下文展示，不进入历史因子、策略回测或样本外验证。

`report` 和每日流程生成的 `daily_report.md` 会在行业标签已覆盖至少两只当前评分股票时，附加最多五个行业热度条目。该栏目用于观察当前候选池的行业集中度，不参与候选评分或回测。

`import-fundamentals` 用于导入本地 CSV 财务数据。支持代码、报告期、公告日期、营业收入、净利润/归母净利润、ROE、经营现金流、PE TTM 和 PB 的中英文常见列名；日期支持 `20260331`、`2026/03/31` 与 ISO 日期。系统保留 CSV 原始数值单位和来源文件，不做金额单位换算。

`import-public-fundamentals` 可从公开东财财报接口抓取指定代码或已导入股票池。当前映射已核验的报告期、公告日期、营业收入及其同比、归母净利润及其同比、加权 ROE 与经营活动现金流；现金流表仅取 `NETCASH_OPERATE`，但公告日期统一使用主财报，避免现金流表的异常旧公告日影响回测边界。PE TTM、PB 仍以 CSV 为准。相同报告期存在多条记录时，详情页优先显示字段更完整的一条。带公告日期且满足 ROE、营收和净利润规则的记录可进入“已披露盈利质量”因子；营收同比和净利同比均不为负时可进入“已披露增长质量”因子；经营现金流为正且不低于归母净利 80% 时可进入“已披露现金流质量”因子。系统只从公告日后的下一个交易日开始使用它们。没有公告日期的 CSV 仍只用于展示与人工复核，避免未来数据泄漏。

相对强弱因子采用 RPS 思路：按可比股票池的 60/120 日阶段涨幅做横截面百分位排名。`RPS60 相对强势`、`RPS120 长期强势` 是趋势加分项，`RPS60 相对弱势` 是风险过滤项；策略回测里 RPS 权重低于直接量价形态，避免单纯追高排名。

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
- 触发条件与失效条件：基于 MA5/MA20、评分门槛和观察状态生成，保存到每次 thesis 中供复盘核对
- 下一步动作：等待回踩、复核行业催化、观察 MA5/MA20 支撑等

新闻来源优先使用同花顺本地明文资讯缓存，例如 `text\同花顺\实时解盘.xml`。如果本机同花顺资讯缓存为空，可用东方财富公开公告作为个股相关新闻兜底；公开公告会按股票代码/名称写入同一张 `news_items` 表，供 `/news`、个股详情页和 `/diagnose` 使用。可手动导入和查看：

```powershell
python -m ths_stock_picker import-ths-news
python -m ths_stock_picker import-public-announcements 000538 --per-symbol 3
python -m ths_stock_picker import-public-announcements --universe auto --limit 30 --per-symbol 3
python -m ths_stock_picker news --limit 20
python -m ths_stock_picker news --tag AI算力
```

新闻会被打上确定性事件标签，例如业绩利好、业绩风险、减持质押、回购增持、中标订单、退市风险、并购投资、AI算力、消费、新能源、政策监管和公告；普通业绩预告、定期报告、投资者关系和董事会会议保持中性。AI 只将明确利好计入正向消息、将明确风险计入风险消息，不再把“预亏”或“同比下降”误判为催化。已入库的旧资讯可用 `reclassify-news` 一次重标。AI 选股会优先匹配个股相关新闻；没有直接个股新闻时，会按板块/名称补充主题新闻作为辅助证据。`run-daily` 可加 `--public-announcements --public-announcement-limit 30 --public-announcements-per-symbol 3` 同步当日股票池公告；公开公告网络失败时会写入每日记录，但不会阻断行情、评分和导出。

`report` 和每日流程生成的 `daily_report.md` 会在候选榜中附加“消息面”列，显示每只候选的相关新闻数量与最新标题，便于盘后快速筛掉需要先读公告的标的。

`candidates` 命令和首页“导出 CSV”也会附加 `news_count`、`latest_news_time`、`latest_news_title`、`latest_news_tags` 和 `latest_news_source` 字段，方便在 Excel/WPS 里筛选公告、业绩利好和业绩风险候选。

命令行：

```powershell
python -m ths_stock_picker ai-pick --limit 20 --min-score 20
python -m ths_stock_picker ai-pick --limit 20 --min-score 20 --save
python -m ths_stock_picker ai-explain 600000
python -m ths_stock_picker ai-explain 600000 --save
python -m ths_stock_picker ai-history --limit 30
python -m ths_stock_picker ai-history --symbol 600000
python -m ths_stock_picker ai-changes --limit 50
python -m ths_stock_picker ai-outcomes --limit 30 --horizon 5
python -m ths_stock_picker ai-outcomes --symbol 600000 --horizon 10
```

使用 `--save` 或 Web 上的“保存本次 AI 榜单”会把生成的结构化观点写入 `ai_decisions` 表，供后续复盘和对比。每日流程在同一代码、同一评分日重复执行时会替换旧快照，避免重复存档；原始手动保存仍会留在历史中。`ai-changes` 和 `ai-outcomes` 都按“代码 + 评分日”去重并保留最新版本。`ai-outcomes` 以保存时评分日为信号日，从下一个有效交易日开盘观察到第 N 个后续交易日收盘；日线未更新或观察期未满时会保留“待观察”，不会补造收益结论。复盘页还会按保存结论汇总已完成样本的命中率和平均收益，待观察和无法评估的记录不参与收益统计。Web 对应页面为 `/ai/outcomes`。后续可在这一层接入 OpenAI 或本地大模型，让大模型读取同一份结构化证据后生成更细的行业、财务和风险复盘。

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
