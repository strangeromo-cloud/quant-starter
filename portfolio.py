"""
我的持仓分析：归一化净值 + 相关性矩阵 + 各股风险指标 + 等权组合。
纯读库，支持跨市场（A/US/HK）混合，ts_code 天然区分。
"""
import numpy as np
import pandas as pd

import config
import data


def analyze(tickers: list, start: str = None, end: str = None) -> dict:
    start = start or config.START_DATE
    end = end or config.END_DATE
    start_dt, end_dt = pd.to_datetime(start), pd.to_datetime(end)

    # 每只用「历史+当前」拼接序列（延伸到最新），再组装成面板
    series_map = {}
    for t in tickers:
        s = data.load_combined_series(t)
        if not s.empty:
            series_map[t] = s
    panel = pd.DataFrame(series_map).sort_index() if series_map else pd.DataFrame()
    if not panel.empty:
        panel = panel.loc[(panel.index >= start_dt) & (panel.index <= end_dt)]
    # 区间内真正有数据的才算 present（智谱只有2026数据、区间到2025则算缺失）
    present = [t for t in tickers if (not panel.empty) and t in panel.columns
              and panel[t].notna().any()]
    missing = [t for t in tickers if t not in present]
    if not present:
        return {"series": {}, "metrics": {}, "correlation": None,
                "portfolio": None, "missing": missing, "names": {}}

    panel = panel[present]

    series, metrics = {}, {}
    for t in present:
        s = panel[t].dropna()
        if s.empty:
            continue
        norm = s / s.iloc[0]
        series[t] = {"dates": [d.strftime("%Y-%m-%d") for d in norm.index],
                     "values": [round(float(v), 4) for v in norm.values]}
        rets = s.pct_change().dropna()
        cummax = s.cummax()
        max_dd = float((s / cummax - 1).min()) if len(s) else 0.0
        ann_vol = float(rets.std() * np.sqrt(252)) if len(rets) > 1 else 0.0
        metrics[t] = {
            "total_return": f"{(s.iloc[-1]/s.iloc[0]-1):.1%}",
            "ann_vol": f"{ann_vol:.1%}",
            "max_dd": f"{max_dd:.1%}",
            "days": int(len(s)),
        }

    # 相关性矩阵（日收益，pandas 默认成对剔除缺失）
    rets_df = panel.pct_change()
    corr = rets_df.corr()
    corr_tickers = list(corr.columns)
    corr_matrix = [[(None if pd.isna(corr.iloc[i, j]) else round(float(corr.iloc[i, j]), 2))
                    for j in range(len(corr_tickers))]
                   for i in range(len(corr_tickers))]
    # 平均成对相关性（排除对角线）
    n = len(corr_tickers)
    off = [corr.iloc[i, j] for i in range(n) for j in range(n)
           if i != j and not pd.isna(corr.iloc[i, j])]
    avg_corr = round(float(np.mean(off)), 2) if off else None

    # 等权组合（取所有标的都有数据的共同窗口）
    portfolio = None
    common = panel.dropna()
    if len(common) >= 30:
        norm_df = common / common.iloc[0]
        port_nav = norm_df.mean(axis=1)
        rets = port_nav.pct_change().dropna()
        cummax = port_nav.cummax()
        portfolio = {
            "nav": {"dates": [d.strftime("%Y-%m-%d") for d in port_nav.index],
                    "values": [round(float(v), 4) for v in port_nav.values]},
            "total_return": f"{(port_nav.iloc[-1]/port_nav.iloc[0]-1):.1%}",
            "ann_vol": f"{rets.std()*np.sqrt(252):.1%}",
            "max_dd": f"{(port_nav/cummax-1).min():.1%}",
            "window": f"{port_nav.index[0].strftime('%Y-%m-%d')} ~ {port_nav.index[-1].strftime('%Y-%m-%d')}",
        }

    names = {t: config.TICKER_NAMES.get(t, t) for t in present}
    return {
        "series": series, "metrics": metrics,
        "correlation": {"tickers": corr_tickers, "matrix": corr_matrix, "avg": avg_corr},
        "portfolio": portfolio, "missing": missing, "names": names,
    }
