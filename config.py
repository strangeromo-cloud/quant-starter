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
