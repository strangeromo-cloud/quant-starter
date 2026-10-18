"""全局配置。敏感信息用环境变量传（token / DB 密码），不写进代码。"""
import os

# Tushare token：用环境变量 TUSHARE_TOKEN 传（运行脚本会自动注入）
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")

# 数据库连接见 db.py，从 DB_HOST/DB_USER/DB_PASSWORD/DB_NAME 环境变量读
# 服务器容器内运行时 DB_HOST=mysql, DB_NAME=quant

# 回测区间
START_DATE = "20180101"
END_DATE = "20251231"

# 股票池：用沪深300成分（动态，按月取，自动避开幸存者偏差）
INDEX_CODE = "000300.SH"      # 沪深300
BENCHMARK_CODE = "000300.SH"  # 基准也用沪深300

# ---- 交易成本（A股真实费率，回测必须扣）----
COMMISSION = 0.00025   # 佣金 万2.5（双边）
STAMP_TAX = 0.0005     # 印花税 千0.5（仅卖出）
SLIPPAGE = 0.001       # 滑点 千1（双边，保守估计）

# ---- 策略参数 ----
MOMENTUM_LOOKBACK = 6   # 动量回看月数
HOLD_TOP_N = 20         # 每月持有动量最强的前 N 只
REBALANCE = "M"         # 调仓频率：月度

# ---- 风控择时参数 ----
TIMING_MA_DAYS = 200    # 大盘长期均线天数（跌破则空仓，~年线用 250，200 更灵敏）

# ==== 港美股（当前成分静态池，有幸存者偏差；只支持动量/动量+风控）====
# 美股池：纳斯达克大型股（代码为 Tushare 格式：纯 ticker）
US_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "COST", "NFLX",
    "AMD", "ADBE", "PEP", "CSCO", "TMUS", "INTC", "QCOM", "INTU", "AMAT", "TXN",
    "ISRG", "BKNG", "HON", "VRTX", "REGN", "LRCX", "MU", "ADI", "PANW", "KLAC",
    "SNPS", "CDNS", "MRVL", "ORLY", "CRWD", "ABNB", "FTNT", "ADP", "MELI", "CTAS",
]
# 港股池：恒生科技 + 蓝筹（akshare 下载，后复权）
# 8 只核心股：腾讯/阿里/美团/京东/小米/网易/中国移动/港交所
HK_UNIVERSE = [
    "00700.HK", "09988.HK", "03690.HK", "09618.HK",
    "01810.HK", "09999.HK", "00941.HK", "00388.HK",
]
# 美股拆股表（后向复权用）：ticker -> [(拆股生效日, 拆股比例)]
US_SPLITS = {
    "AAPL":  [("20200831", 4)],
    "AMZN":  [("20220606", 20)],
    "GOOGL": [("20220718", 20)],
    "NVDA":  [("20210720", 4), ("20240610", 10)],
    "TSLA":  [("20200831", 5), ("20220825", 3)],
    "PANW":  [("20220914", 3), ("20241216", 2)],
}
# 各市场基准/择时指数（index_global 代码）
MARKET_BENCHMARK = {"US": "IXIC", "HK": "HSI"}
MARKET_UNIVERSE = {"US": US_UNIVERSE, "HK": HK_UNIVERSE}

# ==== 我的持仓分析 ====
# 股票中文名映射（展示用）
TICKER_NAMES = {
    "00700.HK": "腾讯控股", "09988.HK": "阿里巴巴", "03690.HK": "美团", "09618.HK": "京东",
    "01810.HK": "小米", "09999.HK": "网易", "00941.HK": "中国移动", "00388.HK": "港交所",
    "02513.HK": "智谱AI",
    "AAPL": "苹果", "MSFT": "微软", "NVDA": "英伟达", "GOOGL": "谷歌", "AMZN": "亚马逊",
    "META": "Meta", "TSLA": "特斯拉", "MU": "美光", "AVGO": "博通", "AMD": "AMD",
}
# 默认持仓（SpaceX SPCX 尚未上市无数据，暂不含）
PORTFOLIO_DEFAULT = ["00700.HK", "GOOGL", "NVDA", "AAPL", "MU", "02513.HK"]

# ==== 买入点 / 卖出点（回撤 + 止盈止损）====
# 全局默认参数（每只可在 WATCH_LEVELS 里单独覆盖）：
#   buy_pullback : 买点 = 近 high_lookback 日高点 ×(1 - buy_pullback)，价格跌到这里=低吸买点
#   take_profit  : 止盈卖点 = 成本价 ×(1 + take_profit)（需填 cost 才启用）
#   stop_loss    : 止损卖点 = 成本价 ×(1 - stop_loss)；未填 cost 时降级为
#                  移动止损 = 近 high_lookback 日高点 ×(1 - stop_loss)
#   high_lookback: 计算近期高点/低点的回看交易日数
LEVEL_DEFAULTS = {
    "buy_pullback": 0.10,
    "take_profit": 0.25,
    "stop_loss": 0.12,
    "high_lookback": 60,
    # ---- 真 ATR / Chandelier 止损 & 趋势门（回测验证：趋势门是风险调整后最优）----
    "atr_period": 22,        # ATR 回看（Wilder 平滑），Chandelier 标准用 22
    "chandelier_mult": 4.0,  # Chandelier 止损 = 近22日最高 - 4×ATR（回测: 4×比3×少被扫; 5-6×更宽更优）
    "trend_ma": 200,         # 趋势门均线（年线）
    "trend_gate": True,      # True=价跌破上升年线时不发买入信号（避免接飞刀）
    "use_chandelier": True,  # True=持仓止损用 Chandelier(ATR) 替代固定成本%止损
}
# 每只持仓的成本价与自定义参数。cost=None 表示未填（买点+移动止损照常，止盈不算）。
# 填入自己的持仓成本价即可启用精确止盈/止损，例如 {"cost": 120.0, "take_profit": 0.30}。
WATCH_LEVELS = {
    "00700.HK": {"cost": 455.12},
    "GOOGL":    {"cost": None, "held": False},  # 空仓，仅提示回补买点
    "NVDA":     {"cost": 198.36},
    "AAPL":     {"cost": 292.78},
    "MU":       {"cost": 1030.73},
    "02513.HK": {"cost": None, "held": False},  # 空仓，仅提示回补买点
}

# ==== 美股短线动量（Martin Luk 式：回调 / 突破 / EP + 紧止损 + R 仓位）====
# 规范日线规则 v1：回测（bt_swing.py）与生产纸面跟踪（swing_scan.py）必须用同一套参数。
# 为什么集中在这里：一处改、两边同步；web 端读取时用 SWING.get(key, 默认值) 容错。
SWING = {
    # ---- 账户与风险（盘前作业卡片按这里算建议股数）----
    "account_size": 10000,      # 账户规模（美元）
    "risk_pct": 0.005,          # 每笔打到止损亏账户 0.5%（= 1R）
    "max_position_pct": 0.25,   # 单仓市值 ≤ 25% 账户
    "max_exposure_pct": 1.0,    # 总敞口 ≤ 100%（现金账户，无杠杆）
    "max_stop_pct": 0.03,       # 止损距离上限 3%（变体 5%）；实际还受 ADR20/2 约束
    "hard_stop_cap": 0.05,      # 绝对上限：任何情况下止损距离都不超过 5%
    "min_stop_pct": 0.0,        # 止损距离下限（0 = 只要求 high>low，与回测一致；防 1R 为 0）
    "open_skip_mult": 1.5,      # t+1 高开后 (open-stop)/open > max_stop×1.5 → 放弃不追
    "cost_pct": 0.001,          # 单边成本/滑点 0.1%
    # ---- 交易资格（t 日收盘时点，无未来函数）----
    "price_min": 5,             # 价格 ≥ 5 美元
    "dollar_vol_min": 5e7,      # 20 日均成交额 ≥ 5000 万美元
    "mcap_min": 1e9,            # 市值 ≥ 10 亿美元（东财行情榜初筛用）
    "adr_min": 0.035,           # ADR20 = 近 20 日 mean(high/low - 1) ≥ 3.5%
    "adr_days": 20,
    "dollar_vol_days": 20,
    # ---- 均线 ----
    "ema_fast": 9, "ema_mid": 21, "ema_slow": 50, "ema_long": 150,
    "ema21_rise_days": 5,       # 趋势叠层：EMA21 > 5 个交易日前的 EMA21
    # ---- 观察名单 ----
    "watch_topk": 20,           # 资格池按日涨幅前 20
    "watch_min_gain": 0.03,     # 且日涨幅 ≥ 3%
    "watch_days": 20,           # 保留 20 个交易日（收盘跌破 EMA50 提前移出）
    "watch_cap": 40,            # 生产观察名单上限（控制每日 Twelve Data 调用量）
    "rs_topk": 10,              # 冷启动/补充：东财 60 日涨幅前列最多补几只
    "rs_fill_to": 25,           # 名单少于这么多只时才用 60 日涨幅补
    # ---- 形态 ----
    "pb_touch_tol": 0.005,      # 回调：low ≤ EMA9×(1+0.5%)
    "pb_max_ext50": 0.25,       # 回调：close/EMA50-1 ≤ 25%（不追远离均线的）
    "bo_range_adr_mult": 0.6,   # 突破：inside day 或 振幅 ≤ 0.6×ADR20×close
    "bo_ema9_tol": 0.03,        # 突破：|close/EMA9-1| ≤ 3%
    "bo_base_days": 10,         # 突破：近 10 日 (最高-最低)/close ≤ 3×ADR20
    "bo_base_adr_mult": 3.0,
    "ep_gap": 0.10,             # EP：open ≥ 昨收×1.10
    "ep_vol_mult": 3.0,         # EP：量 ≥ 3×近 50 日均量
    "ep_vol_days": 50,
    "ep_cand_max": 10,          # 生产：名单外"当日跳空≥10%"候选最多取几只 K 线（财报季一天可能几十只，
                                # 不设上限会挤掉名单股的 Twelve Data 额度、或让扫描拖过开盘）
    # ---- 持仓管理 ----
    "be_days": 3,               # 持满 3 个交易日且 EMA9>成交价 → 止损上移到成交价（保本）
    "partial_ext": 0.20,        # close ≥ EMA9×1.20 → 收盘卖 15%（每笔最多一次）
    "partial_frac": 0.15,
    # ---- 市场过滤 / 连亏熔断（回测变体；生产里体现在交易环境结论上）----
    "f1_index": "QQQ",
    "f2_strong_min": 0.40,      # F2：资格池强档占比 ≥ 40% 才开新仓
    "streak_half": 3,           # 连亏 3 笔 → 风险减半直到下一笔盈利
    "streak_pause": 5,          # 连亏 5 笔 → 暂停开新仓
    "pause_days": 2,
    # ---- 交易环境（market_env.assess）----
    "env_indices": ["QQQ", "IWM"],
    "env_ext_days": 15,         # 延伸：连续 ≥15 天在 21EMA 上 且 距 21EMA 超过近一年 80 分位
    "env_ext_pctl": 0.80,
    "env_ext_weeks": 4,         # 或 连续 4 周上涨且没回到过 21EMA
    "env_strong_stop": 0.20,    # 强档占比 < 20% → 休息观望
    "env_breadth_drop": 0.10,   # 强档占比 5 日内下降 ≥10 个百分点 → 精选少做
    "env_breadth_lag": 5,
    "env_min_watch": 10,        # 观察名单不足 10 只 → 宽度退用 20 只板块/主题 ETF
    "env_loss_streak": 3,       # 自己连亏 ≥3 笔 → 精选少做
    # ---- 取数（Twelve Data 免费版 8 次/分钟、800 次/天，与生产其他推送共用）----
    "td_pause_s": 9,            # 两次 Twelve Data 调用间隔 ≥ 9 秒
    "td_429_wait_s": 65,        # 撞分钟限流等 65 秒重试
    "td_budget": 80,            # 单次扫描最多调用次数（保护共享日额度）
    "hist_full": 300,           # 首次取 300 根：Twelve Data 按调用次数计费、与根数无关；220 根时
                                # EMA150 的首根种子权重还残留 5%，300 根降到 2%（强/弱档更准）
    "hist_incr": 15,            # 之后增量取 15 根
    "hist_keep": 320,           # 计算只用最近 320 根
    "em_pages": 16,             # 东财行情榜按成交额最多翻 16 页（每页 100；成交额跌破 5000万$ 即提前停，
                                # 实测合格池约 1400 只）
    "em_page_pause_s": 1.5,     # 东财行情榜翻页间隔
    "em_kline_pause_s": 3,      # 东财日K兜底：间隔 ≥3 秒 + 指数退避
    # ---- 卡片 ----
    "card_max_setups": 8,
    "backtest_note": "日线回测2012-26：期望-0.31R/笔、年化-5.5%，远不及同池等权+16.7%，无优势",
}
