"""
美股板块动量轮动扫描：每日计算板块 ETF 动量集成排名，供 Lark 卡片展示。

信号 = mean(3月, 6月, 12月收益)（多回看期集成，回测验证：2006-2026 扣成本
前3月调 年化+10.7%/回撤-40% vs SPY +8.8%/-52%；叠加SPY年线门后回撤仅-17%，Sharpe 0.78）。
数据 Twelve Data，增量刷新缓存进 recent_price 表（首次全量由 bt_sector.py 拉好）。
仅技术信号，非投资建议。
"""
import time

import numpy as np

import config
import data
import db

SECTORS = ["XLK", "XLC", "XLV", "XLF", "XLE", "XLI", "XLP", "XLY", "XLU", "XLB", "XLRE", "SMH"]
LOOKBACKS = (63, 126, 252)
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


def compute(refresh: bool = True) -> dict:
    """返回 {ranking: [(sym, mom)], holding_ranks: {sym: (rank, mom)}, total, asof}；失败返回 None。"""
    try:
        if refresh:
            _refresh(SECTORS)
        moms, asof = {}, None
        for sym in SECTORS:
            s = data.load_recent_price(sym)
            if len(s) < max(LOOKBACKS) + 5:
                continue
            moms[sym] = float(np.mean([s.iloc[-1] / s.iloc[-lb] - 1 for lb in LOOKBACKS]))
            asof = max(asof, s.index[-1]) if asof is not None else s.index[-1]
        if len(moms) < 6:
            return None
        ranking = sorted(moms.items(), key=lambda x: -x[1])
        holding_ranks = {}
        for i, (sym, m) in enumerate(ranking, 1):
            if sym in HOLDING_SECTORS:
                holding_ranks[sym] = (i, m)
        return {"ranking": ranking, "holding_ranks": holding_ranks,
                "total": len(ranking),
                "asof": asof.strftime("%Y-%m-%d") if asof is not None else ""}
    except Exception as e:
        print("[sector] 计算失败:", repr(e)[:120])
        return None
