"""
买入点 / 卖出点分析，并给出每个点位的理由。回测验证后的三档逻辑：

买点   = 近 high_lookback 日高点 ×(1 - buy_pullback)（低吸位）
         【趋势门】trend_gate=True 时，价跌破上升年线(MA trend_ma) → 不发买入信号（避免接飞刀）
止损   = Chandelier Exit = 近 atr_period 日最高 - chandelier_mult × ATR(atr_period)（波动率自适应，随行情上移）
         use_chandelier=False 时退回固定：成本×(1-stop_loss) 或 移动止损 近高×(1-stop_loss)
止盈   = 成本价 ×(1 + take_profit)（需 cost）

ATR 用真实波幅（high/low/昨收，Wilder 平滑）。high/low 缺失（旧数据未回填）时自动降级为
收益率滚动标准差×价 的收盘代理。价位与年线均基于 recent_price 真实原始价。仅技术信号，非投资建议。
"""
import numpy as np
import pandas as pd

import config
import data

_VOL_TO_ATR = 1.4  # 收益率std → ATR 的粗略换算（降级代理用）


def _params(ticker: str) -> dict:
    p = dict(config.LEVEL_DEFAULTS)
    p.update(config.WATCH_LEVELS.get(ticker, {}))
    return p


def _pct(x: float) -> str:
    return f"{x:.0%}"


def _atr_series(ohlc: pd.DataFrame, period: int):
    """真 ATR（Wilder 平滑）。high/low 齐全→真实波幅；否则降级为收盘代理。返回 (atr_series, is_true)。"""
    c = ohlc["close"]
    has_hl = ("high" in ohlc and "low" in ohlc
              and ohlc["high"].notna().sum() >= period
              and ohlc["low"].notna().sum() >= period)
    if has_hl:
        h, l = ohlc["high"], ohlc["low"]
        pc = c.shift(1)
        tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / period, adjust=False).mean()
        return atr, True
    # 降级：收益率std×价×换算
    proxy = c.pct_change().rolling(period).std() * c * _VOL_TO_ATR
    return proxy, False


def compute(tickers: list) -> list:
    out = []
    for t in tickers:
        name = config.TICKER_NAMES.get(t, t)
        p = _params(t)
        lookback = int(p["high_lookback"])
        atr_p = int(p["atr_period"])
        trend_ma = int(p["trend_ma"])

        ohlc = data.load_recent_ohlc(t)
        if ohlc.empty or len(ohlc) < 2:
            out.append({"ticker": t, "name": name, "status": "no_data",
                        "triggers": [], "action": "无近期行情数据"})
            continue
        s = ohlc["close"].dropna()
        asof = s.index[-1].strftime("%Y-%m-%d")
        price = float(s.iloc[-1])
        window = s.tail(lookback)
        recent_high = round(float(window.max()), 2)
        recent_low = round(float(window.min()), 2)
        cost = p.get("cost")
        held = p.get("held", True)

        # --- 年线趋势门 ---
        ma = round(float(s.rolling(trend_ma).mean().iloc[-1]), 2) if len(s) >= trend_ma else None
        ma_slope_up = None
        if len(s) >= trend_ma + 20:
            ma_now = s.rolling(trend_ma).mean().iloc[-1]
            ma_prev = s.rolling(trend_ma).mean().iloc[-21]
            ma_slope_up = bool(ma_now > ma_prev)
        uptrend = (ma is not None and price > ma and (ma_slope_up is not False))
        gate_on = bool(p.get("trend_gate", True))

        # --- ATR & Chandelier ---
        atr_s, atr_true = _atr_series(ohlc, atr_p)
        atr = float(atr_s.iloc[-1]) if atr_s.notna().any() else None
        hh = ohlc["high"] if ("high" in ohlc and ohlc["high"].notna().sum() >= atr_p) else s
        chandelier = None
        if atr is not None:
            chandelier = round(float(hh.rolling(atr_p).max().iloc[-1]) - p["chandelier_mult"] * atr, 2)

        # --- 买点 ---
        buy = round(recent_high * (1 - p["buy_pullback"]), 2)

        # --- 止盈 / 止损 ---
        take_profit = round(cost * (1 + p["take_profit"]), 2) if (held and cost) else None
        # 备兑 call 参考行权价：max(成本, 现价×1.05)——贴近现价有租金，但永不低于成本（避免锁损陷阱）
        cc_strike = round(max(cost, price * 1.05), 2) if (held and cost) else None
        use_chand = bool(p.get("use_chandelier", True)) and chandelier is not None
        if not held:
            stop_loss, sl_kind = None, "空仓"
        elif use_chand:
            stop_loss, sl_kind = chandelier, ("Chandelier" if atr_true else "Chandelier(近似)")
        elif cost:
            stop_loss, sl_kind = round(cost * (1 - p["stop_loss"]), 2), "止损"
        else:
            stop_loss, sl_kind = round(recent_high * (1 - p["stop_loss"]), 2), "移动止损"

        # --- 触发判断 ---
        triggers = []
        buy_gated = gate_on and (ma is not None) and (not uptrend)  # 趋势门拦截买入
        if price <= buy and not buy_gated:
            triggers.append("buy")
        if take_profit is not None and price >= take_profit:
            triggers.append("take_profit")
        if stop_loss is not None and price <= stop_loss:
            triggers.append("stop_loss")
        if "stop_loss" in triggers and "buy" in triggers:
            triggers.remove("buy")

        # --- 理由 ---
        buy_reason = f"近{lookback}日高点 {recent_high}，回撤 {_pct(p['buy_pullback'])} 至 {buy}（低吸位）"
        if ma is not None:
            if buy_gated:
                buy_reason += f"；⚠️ 已跌破上升年线({ma})，趋势破位，趋势门拦截买入信号（勿接飞刀）"
            elif uptrend:
                buy_reason += f"；价在上升年线({ma})上方，趋势健康，回调可低吸"
            else:
                buy_reason += f"；年线 {ma} 附近，趋势中性"
        if sl_kind.startswith("Chandelier"):
            atr_tag = "真ATR" if atr_true else "波动率近似"
            sell_reason = (f"Chandelier 移动止损 = 近{atr_p}日最高 − {p['chandelier_mult']:g}×ATR({atr_p}) = {stop_loss}"
                           f"（{atr_tag} {round(atr, 2)}，随行情自适应）")
            if take_profit:
                sell_reason = f"止盈=成本{round(cost,2)}上浮{_pct(p['take_profit'])}={take_profit}；" + sell_reason
        elif sl_kind == "空仓":
            sell_reason = "当前空仓，仅提示回补买点；买回后在 config 填成本价即启用止盈/止损"
        elif cost:
            sell_reason = (f"止盈=成本{round(cost,2)}上浮{_pct(p['take_profit'])}={take_profit}；"
                           f"止损=成本{round(cost,2)}回落{_pct(p['stop_loss'])}={stop_loss}")
        else:
            sell_reason = f"移动止损=近高{recent_high}回撤{_pct(p['stop_loss'])}={stop_loss}，跌破视为趋势破位"

        out.append({
            "ticker": t, "name": name, "status": "ok", "asof": asof,
            "price": round(price, 2), "buy": buy,
            "take_profit": take_profit, "stop_loss": stop_loss, "sl_kind": sl_kind,
            "cc_strike": cc_strike,
            "cost": round(cost, 2) if cost else None,
            "recent_high": recent_high, "recent_low": recent_low, "ma": ma,
            "uptrend": uptrend, "buy_gated": buy_gated,
            "atr": round(atr, 2) if atr is not None else None, "atr_true": atr_true,
            "chandelier": chandelier, "lookback": lookback,
            "to_buy": f"{price / buy - 1:+.1%}",
            "buy_reason": buy_reason, "sell_reason": sell_reason,
            "triggers": triggers,
        })
    return out
