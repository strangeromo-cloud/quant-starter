"""
持仓择时信号：每只持仓相对自己「年线」(默认200日均线)的位置 → 趋势健康/转弱 + 操作建议。
优先用 recent_price（当前原始价）；没有则退回 daily_price（回测复权价，可能不够新）。
"""
import pandas as pd

import config
import data


def compute(tickers: list, ma_days: int = 200) -> list:
    out = []
    for t in tickers:
        name = config.TICKER_NAMES.get(t, t)
        s = data.load_recent_price(t)
        real_price = True   # recent_price 是真实原始价
        src = "当前价"
        if s.empty:
            panel = data.load_price_panel([t])
            s = panel[t].dropna() if (not panel.empty and t in panel.columns) else pd.Series(dtype=float)
            real_price = False  # daily_price 是复权价（相对比例对，绝对值非真实美元）
            src = "截至2025(源限制)"
        if s.empty:
            out.append({"ticker": t, "name": name, "status": "no_data", "action": "无数据"})
            continue

        asof = s.index[-1].strftime("%Y-%m-%d")
        if len(s) < ma_days:
            out.append({"ticker": t, "name": name, "status": "insufficient", "asof": asof,
                        "days": int(len(s)), "src": src,
                        "action": f"样本不足（年线需{ma_days}日，仅{len(s)}日）"})
            continue

        ma = float(s.rolling(ma_days).mean().iloc[-1])
        price = float(s.iloc[-1])
        dist = price / ma - 1
        if dist > 0.05:
            status, action = "strong", "✅ 站稳年线上方，趋势健康 → 持有"
        elif dist >= 0:
            status, action = "near_up", "🟡 在年线附近偏上 → 持有，密切观察"
        elif dist > -0.05:
            status, action = "near_down", "🟠 刚跌破年线 → 警惕，考虑减仓或设止损"
        else:
            status, action = "weak", "🔴 明显跌破年线，趋势转弱 → 考虑减仓/止损"

        rec = {"ticker": t, "name": name, "status": status, "asof": asof, "src": src,
               "dist": f"{dist:+.1%}", "ma_days": ma_days, "action": action,
               "real_price": real_price}
        if real_price:   # 真实价才展示绝对数值
            rec["price"] = round(price, 2)
            rec["ma"] = round(ma, 2)
        out.append(rec)
    return out
