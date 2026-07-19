"""
美股板块动量轮动回测：验证「每月持有动量最强的前3个板块ETF」相对 SPY 有无真实 edge。

策略（研究验证的稳健形态）：
  - 池子：11 个 SPDR 板块 ETF（XLK/XLC/XLV/XLF/XLE/XLI/XLP/XLY/XLU/XLB/XLRE）+ SMH 半导体
  - 信号：动量集成 = mean(3月, 6月, 12月收益)（多回看期集成，避免单参数脆弱）
  - 换仓：月末调仓，持前3等权；扣单边 0.05% 成本（ETF点差+滑点，美股无佣金）
  - 变体：RISK 版 = SPY 跌破200日线时全仓现金（复用已验证的趋势门）
对照：SPY 买入持有。数据 Twelve Data（免费版 outputsize=5000，约20年），
拉取后缓存进 recent_price 表，后续扫描器复用。
"""
import time

import numpy as np
import pandas as pd

import data
import db

SECTORS = ["XLK", "XLC", "XLV", "XLF", "XLE", "XLI", "XLP", "XLY", "XLU", "XLB", "XLRE", "SMH"]
BENCH = "SPY"
TOP_N = 3
COST = 0.0005          # 单边 0.05%
LOOKBACKS = (63, 126, 252)   # 3/6/12月（交易日）


def fetch_cached(symbol: str) -> pd.Series:
    """优先读 recent_price 缓存；不足 3 年时从 Twelve Data 拉全量并缓存。"""
    s = data.load_recent_price(symbol)
    if len(s) < 750:
        for attempt in range(3):
            try:
                df = data._fetch_twelvedata(symbol, outputsize=5000)
                break
            except Exception as e:
                if "429" in str(e) and attempt < 2:
                    print(f"[bt] {symbol} 429，等 65s")
                    time.sleep(65)
                else:
                    raise
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        db.upsert_df(df[["ts_code", "trade_date", "close"]], "recent_price",
                     key_cols=["ts_code", "trade_date"])
        s = data.load_recent_price(symbol)
        time.sleep(9)  # 免费版 8 次/分钟
    return s


def month_ends(idx: pd.DatetimeIndex) -> list:
    s = pd.Series(idx, index=idx)
    return list(s.groupby(s.index.to_period("M")).max())


def run():
    print("[bt] 拉取/缓存行情 ...")
    px = {}
    for sym in SECTORS + [BENCH]:
        s = fetch_cached(sym)
        if len(s) < 300:
            print(f"  跳过 {sym}: 仅 {len(s)} 天")
            continue
        px[sym] = s
        print(f"  {sym}: {len(s)} 天  {s.index[0].date()} ~ {s.index[-1].date()}")
    panel = pd.DataFrame(px).sort_index().ffill(limit=3)
    spy = panel[BENCH].dropna()
    sectors = [c for c in panel.columns if c != BENCH]
    ma200 = spy.rolling(200).mean()

    mes = [d for d in month_ends(panel.index) if d >= panel.index[0] + pd.Timedelta(days=400)]
    rets = panel.pct_change()

    for use_gate in (False, True):
        tag = "ROT+GATE" if use_gate else "ROT"
        nav, nav_spy = [1.0], [1.0]
        dates = [mes[0]]
        hold, turnover_cost = [], 0.0
        for i in range(len(mes) - 1):
            d0, d1 = mes[i], mes[i + 1]
            # 信号：截至 d0 的动量集成（无未来函数）
            moms = {}
            for c in sectors:
                s = panel[c].dropna()
                s = s[s.index <= d0]
                if len(s) < max(LOOKBACKS) + 5:
                    continue
                moms[c] = np.mean([s.iloc[-1] / s.iloc[-lb] - 1 for lb in LOOKBACKS])
            top = sorted(moms, key=moms.get, reverse=True)[:TOP_N]
            gate_off = use_gate and (spy[spy.index <= d0].iloc[-1] < ma200[ma200.index <= d0].iloc[-1])
            new_hold = [] if gate_off else top
            # 换手成本：进出各扣一次
            changed = len(set(new_hold) ^ set(hold))
            cost = changed / max(TOP_N, 1) * COST
            hold = new_hold
            # 持有期收益（d0 次日 ~ d1）
            period = rets.loc[(rets.index > d0) & (rets.index <= d1)]
            if hold:
                pr = period[hold].mean(axis=1).fillna(0)
            else:
                pr = pd.Series(0.0, index=period.index)  # 现金
            nav.append(nav[-1] * float((1 + pr).prod()) * (1 - cost))
            nav_spy.append(nav_spy[-1] * float((1 + period[BENCH].fillna(0)).prod()))
            dates.append(d1)
        nav = pd.Series(nav, index=dates)
        nav_spy_s = pd.Series(nav_spy, index=dates)
        yrs = (dates[-1] - dates[0]).days / 365.25
        for name, n in ((tag, nav), ("SPY", nav_spy_s)):
            cagr = n.iloc[-1] ** (1 / yrs) - 1
            dd = (n / n.cummax() - 1).min()
            mrets = n.pct_change().dropna()
            sharpe = mrets.mean() / mrets.std() * np.sqrt(12) if mrets.std() > 0 else float("nan")
            print(f"  {name:9} 总{n.iloc[-1]-1:+8.0%}  年化{cagr:+7.1%}  回撤{dd:+7.0%}  Sharpe{sharpe:5.2f}")
        print("-" * 60)
    # 当前排名（供参考）
    print("[bt] 当前动量排名（截至最新收盘）:")
    moms = {}
    for c in sectors:
        s = panel[c].dropna()
        if len(s) < max(LOOKBACKS) + 5:
            continue
        moms[c] = np.mean([s.iloc[-1] / s.iloc[-lb] - 1 for lb in LOOKBACKS])
    for i, (c, m) in enumerate(sorted(moms.items(), key=lambda x: -x[1]), 1):
        print(f"  {i:2}. {c:5} {m:+.1%}")


if __name__ == "__main__":
    run()
