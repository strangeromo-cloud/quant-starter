"""
美股板块动量轮动扫描：每日计算板块 ETF 动量集成排名，供 Lark 卡片展示。

信号 = mean(3月, 6月, 12月收益)（多回看期集成，回测验证：2006-2026 扣成本
前3月调 年化+10.7%/回撤-40% vs SPY +8.8%/-52%；叠加SPY年线门后回撤仅-17%，Sharpe 0.78）。
数据 Twelve Data，增量刷新缓存进 recent_price 表（首次全量由 bt_sector.py 拉好）。
仅技术信号，非投资建议。
"""
import time

import numpy as np
import pandas as pd

import config
import data
import db

SECTORS = ["XLK", "XLC", "XLV", "XLF", "XLE", "XLI", "XLP", "XLY", "XLU", "XLB", "XLRE", "SMH"]
# 主题 ETF：短线热度雷达专用（纯展示。回测：20d 快速轮动年化仅 +5.4%/Sharpe 0.37，
# 远逊月度基线 +9.6%/0.79——热度只做观察雷达，不产生买卖信号）
THEMES = ["IGV", "XBI", "URA", "ITA", "TAN", "GDX", "KWEB", "ARKK"]
NAMES = {"XLK": "科技", "XLC": "通信", "XLV": "医疗", "XLF": "金融", "XLE": "能源",
         "XLI": "工业", "XLP": "必需", "XLY": "可选", "XLU": "公用", "XLB": "材料",
         "XLRE": "地产", "SMH": "半导体", "IGV": "软件", "XBI": "生物科技",
         "URA": "铀核能", "ITA": "军工", "TAN": "太阳能", "GDX": "金矿",
         "KWEB": "中概互联", "ARKK": "创新"}
LOOKBACKS = (63, 126, 252)
TOP_N = 3          # 轮动策略持仓数（回测口径）
# 持仓 → 所在板块（卡片高亮你的板块排名）
HOLDING_SECTORS = {"SMH": "英伟达/美光", "XLK": "苹果", "XLC": "谷歌"}


def _refresh(symbols: list, outputsize: int = 30):
    """增量刷新最近行情到 recent_price（免费版 8 次/分钟，间隔 9s；429 等 65s 重试）。"""
    for sym in symbols:
        for attempt in range(3):
            try:
                df = data._fetch_twelvedata(sym, outputsize=outputsize)
                import pandas as pd
                df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
                db.upsert_df(df[["ts_code", "trade_date", "close"]], "recent_price",
                             key_cols=["ts_code", "trade_date"])
                break
            except Exception as e:
                if "429" in str(e) and attempt < 2:
                    print(f"[sector] {sym} 429，等 65s")
                    time.sleep(65)
                else:
                    print(f"[sector] {sym} 刷新失败(用缓存): {repr(e)[:80]}")
                    break
        time.sleep(9)


def _ensemble_mom(s, upto=None):
    """动量集成 = mean(3/6/12月收益)。upto 给定时只用截至该日的数据（复现月末信号）。"""
    if upto is not None:
        s = s[s.index <= upto]
    if len(s) < max(LOOKBACKS) + 5:
        return None
    return float(np.mean([s.iloc[-1] / s.iloc[-lb] - 1 for lb in LOOKBACKS]))


def compute(refresh: bool = True) -> dict:
    """返回 {ranking, holding_ranks, total, asof, heat, rotation}；失败返回 None。
    heat     : 短线热度雷达 [(sym, 20日, 5日)]（板块+主题合并池，纯展示）
    rotation : 月度轮动策略执行视图 {holdings, gate_on, preview, month_end_soon}
               holdings=上一月末信号的应持前3；preview=按最新数据的排名（月末生效）"""
    try:
        if refresh:
            _refresh(SECTORS + THEMES)
        series = {}
        for sym in SECTORS + THEMES + ["SPY"]:
            s = data.load_recent_price(sym)
            if len(s) >= 30:
                series[sym] = s
        moms, asof = {}, None
        for sym in SECTORS:
            m = _ensemble_mom(series.get(sym, data.load_recent_price(sym)))
            if m is None:
                continue
            moms[sym] = m
            asof = max(asof, series[sym].index[-1]) if asof is not None else series[sym].index[-1]
        if len(moms) < 6:
            return None
        ranking = sorted(moms.items(), key=lambda x: -x[1])
        holding_ranks = {}
        for i, (sym, m) in enumerate(ranking, 1):
            if sym in HOLDING_SECTORS:
                holding_ranks[sym] = (i, m)

        # --- 短线热度雷达（20日+5日，板块+主题合并池，纯展示）---
        heat = []
        for sym, s in series.items():
            if sym == "SPY" or len(s) < 25:
                continue
            heat.append((sym, float(s.iloc[-1] / s.iloc[-20] - 1),
                         float(s.iloc[-1] / s.iloc[-5] - 1)))
        heat.sort(key=lambda x: -x[1])

        # --- 月度轮动策略执行视图（回测口径：月末信号 + SPY 年线门）---
        rotation = None
        spy = series.get("SPY")
        if spy is not None and len(spy) > 210:
            last_dt = spy.index[-1]
            prev_me = (last_dt.replace(day=1) - pd.Timedelta(days=1))  # 上月末（日历）
            me_moms = {sym: _ensemble_mom(series[sym], upto=prev_me)
                       for sym in SECTORS if sym in series}
            me_moms = {k: v for k, v in me_moms.items() if v is not None}
            holdings = sorted(me_moms, key=me_moms.get, reverse=True)[:TOP_N]
            spy_me = spy[spy.index <= prev_me]
            gate_on = bool(spy_me.iloc[-1] >= spy_me.rolling(200).mean().iloc[-1]) if len(spy_me) > 210 else True
            preview = [sym for sym, _ in ranking[:TOP_N]]
            next_month = (last_dt + pd.offsets.MonthEnd(0))
            month_end_soon = bool((next_month - last_dt).days <= 3)
            rotation = {"holdings": holdings, "gate_on": gate_on,
                        "preview": preview, "month_end_soon": month_end_soon}

        return {"ranking": ranking, "holding_ranks": holding_ranks,
                "total": len(ranking), "heat": heat[:4], "rotation": rotation,
                "asof": asof.strftime("%Y-%m-%d") if asof is not None else ""}
    except Exception as e:
        print("[sector] 计算失败:", repr(e)[:120])
        return None
