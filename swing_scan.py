"""
美股短线动量扫描（Martin Luk 式：回调 / 突破 / EP + 紧止损 + R 仓位）——盘前作业 + 纸面跟踪。

为什么这样设计：
  冠军打法（2025 USIC 股票组 +969.8%，940 笔亏 731 笔，胜率约 22%）的核心不是"选得准"，
  而是"亏多少自己定"：只在止损能放得很近的位置买，看错马上认亏（平均 -2%），看对多拿（平均 +14%）。
  所以这里不预测涨跌，只做三件事：
    1) 从流动性好、波动够大的强势股里建观察名单（等它们回调/收窄）；
    2) 每天收盘后按**规范日线规则 v1**识别三种形态，只保留止损 ≤ min(ADR/2, 3%) 的，
       并按"每笔打到止损亏账户 0.5%"算好建议股数（R 仓位）；
    3) 把每个信号写进 swing_signals 做纸面跟踪，用后续真实 K 线按同一套规则推进，
       用 R 倍数记账——上线后它和回测（bt_swing.py）用同一套规则，才能互相验证。

规范日线规则 v1（参数集中在 config.SWING；回测与生产必须同一套）：
  资格(t 日收盘)：价≥5$；20日均成交额≥5000万$；ADR20≥3.5%（=近20日 mean(high/low-1)）；close>EMA21
  趋势叠层：EMA9>EMA21>EMA50 且 EMA21 > 5 个交易日前的 EMA21
  观察名单：资格池里日涨幅前 20 且涨幅≥3% 进入；保留 20 个交易日（入选当天算第 1 天），收盘跌破 EMA50 提前移出
  形态（t 日收盘判定、t+1 执行；同一天多种形态成立时按 EP > 突破 > 回调 只取一种，触发/止损都是 high_t/low_t）：
    回调 pullback：名单内且 t 日流动性合格(价≥5$、20日均额≥5000万$)；叠层；low ≤ EMA9×1.005 且 close ≥ EMA21；
                   close/EMA50-1 ≤ 25%
    突破 breakout：同上前提；叠层；inside day 或 振幅 ≤ 0.6×ADR20×close；|close/EMA9-1| ≤ 3%；
                   近 10 日(含当日) (最高-最低)/close ≤ 3×ADR20
    EP           ：t 日资格合格的任意股票（不要求在名单里）；open ≥ 昨收×1.10 且 量 ≥ 3×前 50 日均量（不含当日）
    止损过滤      ：stop_pct=(触发-止损)/触发 ≤ min(ADR20/2, max_stop)，且 high>low；ADR 缺失不做
  执行(t+1)：open>触发 → 按 open 成交（(open-止损)/open > max_stop×1.5 则放弃）；否则 high≥触发 → 按触发价成交；
             都没有 → 过期。单边成本 0.1%。成交当根 K 线 low≤止损 → 一律判止损（保守）。
  管理：成交后满 3 个交易日（成交日为第 0 天，即第 3 天收盘起）且 EMA9>成交价 → 止损上移到成交价（次日生效）；
        close ≥ EMA9×1.20 → 收盘卖 15%（每笔一次）；close < EMA9 → 收盘全卖；
        盘中 low ≤ 止损 → 按止损出（开盘就低于止损则按开盘价出，亏损可超 1R）。
  R 记账：1R = 成交价 - 初始止损（每股）；r = Σ 份额×(卖价×(1-成本) - 成交价×(1+成本)) / 1R。

生产与回测的差别（取数限制所致，均已在 open issues 里说明）：
  - 观察名单入选按东财行情榜的当日涨幅/成交额/市值初筛，取完 K 线后再按完整资格复核（不合格不入名单）；
  - 冷启动/名单不足时用东财 60 日涨幅前列补（source=rs），回测不需要；
  - EP 只能在"取了 K 线"的股票上判：名单 + 东财行情榜上当日跳空 ≥10% 的（回测是全池）。
  - 纸面跟踪是逐笔 R 记账，不模拟组合层面的现金/总敞口约束（回测有），所以每个止损合格的信号都记。

quality（0-100，排序用；与 bt_swing.build_signals 同一公式 ×100）
  = 100 × (0.5×相对强度 + 0.3×止损紧度 + 0.2×趋势斜率)
  相对强度 = 60 日涨幅在东财初筛池里的百分位（回测用 63 日涨幅在流动性池里的百分位；未知取 0.5）
  止损紧度 = 1 - stop_pct/止损上限；趋势斜率 = clip((EMA9/EMA50-1)/20%, 0, 1)；止损不合格的再 ×0.5（仅展示）。
仅技术信号，非投资建议。
"""
from __future__ import annotations

import json
import math
from datetime import datetime

import numpy as np
import pandas as pd

import config
import data
import db
import market_env

SETUP_CN = {"pullback": "回调", "breakout": "突破", "ep": "EP"}
TIER_CN = {"strong": "强", "medium": "中", "weak": "弱"}
SETUP_PRIORITY = ("ep", "breakout", "pullback")
WATCH_SOURCES = ("gainer", "rs", "carry")
# 剔除 ETF / 基金 / 信托 / 杠杆产品（与 bt_swing.py 建池同一份关键词）
EXCLUDE_KW = ["ETF", "ETN", "FUND", "TRUST", "ISHARES", "SPDR", "PROSHARES", "DIREXION",
              "INVESCO", "VANGUARD", "WISDOMTREE", "GLOBAL X", "VANECK", "GRANITESHARES",
              "基金", "信托", "指数", "杠杆", "做多", "做空", "反向", "两倍", "二倍", "三倍",
              "2X", "3X", "优先", "权证", "认股", "单位", "PREF", "WARRANT", " UNIT", "RIGHTS"]


def _cfg(over: dict = None) -> dict:
    c = dict(config.SWING)
    if over:
        c.update(over)
    return c


def _nan(x) -> bool:
    try:
        return x is None or (isinstance(x, float) and math.isnan(x)) or pd.isna(x)
    except (TypeError, ValueError):
        return False


def _r(x, n=2):
    return None if _nan(x) else round(float(x), n)


# ============================================================================
# 一、指标
# ============================================================================
def ema(s: pd.Series, span: int) -> pd.Series:
    """EMA（pandas ewm adjust=False，前 span-1 根为 NaN，避免用没预热好的值）。"""
    return s.ewm(span=span, adjust=False, min_periods=span).mean()


def compute_indicators(bars: pd.DataFrame, c: dict = None) -> pd.DataFrame:
    """在 OHLCV（index=日期）上加：ema9/21/50/150（名义名，跨度取 config）、adr（ADR20）、
    dvol（20日均成交额$）、vol_avg_prior（前 50 日均量，不含当日）、chg（日涨幅）、ret20、ret63、
    stack（趋势叠层）、liquid（价格+成交额）、eligible（交易资格）、tier（strong/medium/weak，EMA150 未预热为 None）。"""
    c = _cfg(c)
    df = bars.copy()
    df.index = pd.to_datetime(df.index)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce") if col in df else np.nan
    close = df["close"]
    df["ema9"] = ema(close, int(c["ema_fast"]))
    df["ema21"] = ema(close, int(c["ema_mid"]))
    df["ema50"] = ema(close, int(c["ema_slow"]))
    df["ema150"] = ema(close, int(c["ema_long"]))
    n_adr, n_dv, n_ev = int(c["adr_days"]), int(c["dollar_vol_days"]), int(c["ep_vol_days"])
    df["adr"] = (df["high"] / df["low"] - 1).rolling(n_adr, min_periods=n_adr).mean()
    df["dvol"] = (close * df["volume"]).rolling(n_dv, min_periods=n_dv).mean()
    df["vol_avg_prior"] = df["volume"].shift(1).rolling(n_ev, min_periods=n_ev).mean()
    df["chg"] = close / close.shift(1) - 1
    df["ret20"] = close / close.shift(20) - 1
    df["ret63"] = close / close.shift(63) - 1
    rise = int(c["ema21_rise_days"])
    df["stack"] = ((df["ema9"] > df["ema21"]) & (df["ema21"] > df["ema50"])
                   & (df["ema21"] > df["ema21"].shift(rise)))
    df["liquid"] = (close >= float(c["price_min"])) & (df["dvol"] >= float(c["dollar_vol_min"]))
    df["eligible"] = df["liquid"] & (df["adr"] >= float(c["adr_min"])) & (close > df["ema21"])
    strong = (close > df["ema50"]) & (df["ema50"] > df["ema150"])
    weak = (close < df["ema50"]) & (df["ema50"] < df["ema150"])
    tier = np.where(strong, "strong", np.where(weak, "weak", "medium")).astype(object)
    tier[df["ema150"].isna().values | df["ema50"].isna().values] = None
    df["tier"] = tier
    return df


def eligibility_fails(row: pd.Series, c: dict = None) -> list:
    """资格不符的具体原因（中文短句），合格返回 []。"""
    c = _cfg(c)
    out = []
    if _nan(row.get("close")) or row["close"] < float(c["price_min"]):
        out.append(f"价<{c['price_min']}$")
    if _nan(row.get("dvol")) or row["dvol"] < float(c["dollar_vol_min"]):
        out.append(f"20日均成交额<{float(c['dollar_vol_min']) / 1e6:.0f}M$")
    if _nan(row.get("adr")) or row["adr"] < float(c["adr_min"]):
        out.append(f"ADR<{float(c['adr_min']):.1%}")
    if _nan(row.get("ema21")) or not row["close"] > row["ema21"]:
        out.append("收盘不在21EMA上")
    return out


# ============================================================================
# 二、形态识别 + 止损过滤 + R 仓位 + 打分
# ============================================================================
def _flag(r, key) -> bool:
    v = r.get(key)
    return False if _nan(v) else bool(v)


def detect_setups(ind: pd.DataFrame, i: int = -1, c: dict = None, member: bool = True) -> list:
    """第 i 根（默认最新）收盘时成立的形态，按优先级 EP > 突破 > 回调 排列：[(setup, 说明)]。
    member=False（不在观察名单里）时只看 EP。EP 要求 t 日资格合格；回调/突破要求 t 日流动性合格。
    只看 t 日及以前的数据（无未来函数）。"""
    c = _cfg(c)
    n = len(ind)
    i = i if i >= 0 else n + i
    if n < 2 or i < 1 or i >= n:
        return []
    r, p = ind.iloc[i], ind.iloc[i - 1]
    o, h, lo, cl = r["open"], r["high"], r["low"], r["close"]
    if any(_nan(x) for x in (h, lo, cl)) or cl <= 0:
        return []
    out = []
    # EP：财报等消息跳空高开 ≥10% 且巨量
    va = r.get("vol_avg_prior")
    if (_flag(r, "eligible") and not _nan(o) and not _nan(p["close"]) and p["close"] > 0
            and not _nan(va) and va > 0
            and not _nan(r["volume"])
            and o >= p["close"] * (1 + float(c["ep_gap"])) and r["volume"] >= float(c["ep_vol_mult"]) * va):
        out.append(("ep", f"跳空 {o / p['close'] - 1:+.1%} · 量 {r['volume'] / va:.1f}×50日均量"))
    stack = member and _flag(r, "stack") and _flag(r, "liquid")
    adr = r.get("adr")
    # 突破：横盘收窄，贴 9EMA（inside day 或振幅 ≤ 0.6×ADR）
    nb = int(c["bo_base_days"])
    if stack and not _nan(adr) and i + 1 >= nb and not _nan(p["high"]) and not _nan(p["low"]):
        inside = h <= p["high"] and lo >= p["low"]
        narrow = (h - lo) <= float(c["bo_range_adr_mult"]) * adr * cl
        near9 = abs(cl / r["ema9"] - 1) <= float(c["bo_ema9_tol"])
        seg = ind.iloc[i - nb + 1:i + 1]
        base = (seg["high"].max() - seg["low"].min()) / cl
        if (inside or narrow) and near9 and base <= float(c["bo_base_adr_mult"]) * adr:
            kind = "inside day" if inside else f"振幅 {(h - lo) / cl:.1%}≤0.6×ADR"
            out.append(("breakout", f"{kind} · 距9EMA {cl / r['ema9'] - 1:+.1%} · {nb}日箱体 {base:.1%}"))
    # 回调：回到 9EMA 附近、守住 21EMA、不追远离 50EMA 的
    if stack and lo <= r["ema9"] * (1 + float(c["pb_touch_tol"])) and cl >= r["ema21"] \
            and cl / r["ema50"] - 1 <= float(c["pb_max_ext50"]):
        out.append(("pullback", f"低点回踩9EMA({lo / r['ema9'] - 1:+.1%}) · 收在21EMA上 · "
                                f"距50EMA {cl / r['ema50'] - 1:+.1%}"))
    order = {k: j for j, k in enumerate(SETUP_PRIORITY)}
    return sorted(out, key=lambda x: order[x[0]])


def stop_check(trigger: float, stop: float, adr: float, c: dict = None) -> tuple:
    """止损过滤：返回 (ok, stop_pct, cap)。cap = min(ADR20/2, max_stop, hard_stop_cap)。"""
    c = _cfg(c)
    if _nan(trigger) or _nan(stop) or trigger <= 0:
        return False, None, None
    sp = (trigger - stop) / trigger
    if _nan(adr):                                   # 没有 ADR 就没法定上限 → 不做（与回测一致）
        return False, float(sp), None
    cap = min(float(c["max_stop_pct"]), float(c["hard_stop_cap"]), float(adr) / 2)
    ok = trigger > stop and sp >= float(c.get("min_stop_pct", 0.0)) and sp <= cap + 1e-12
    return bool(ok), float(sp), float(cap)


def size_position(entry: float, stop: float, account: float = None, risk_pct: float = None,
                  max_pos_pct: float = None, risk_mult: float = 1.0) -> dict:
    """R 仓位：股数 = 风险额/(入场-止损)，风险额 = risk_pct×账户×risk_mult；单仓市值 ≤ max_pos_pct×账户（截断并注明）。
    取整到股（买不起 1 股就是 0 股）。"""
    c = _cfg()
    account = float(c["account_size"] if account is None else account)
    risk_pct = float(c["risk_pct"] if risk_pct is None else risk_pct)
    max_pos_pct = float(c["max_position_pct"] if max_pos_pct is None else max_pos_pct)
    budget = account * risk_pct * float(risk_mult)
    per = (entry - stop) if not (_nan(entry) or _nan(stop)) else float("nan")
    if _nan(per) or per <= 0 or entry <= 0 or account <= 0:
        return {"size_shares": 0, "size_value": 0.0, "size_pct": 0.0, "risk_value": 0.0,
                "capped": False, "size_note": "止损无效"}
    shares = int(math.floor(budget / per + 1e-9))
    max_sh = int(math.floor(account * max_pos_pct / entry + 1e-9))
    capped = shares > max_sh
    if capped:
        shares = max_sh
    value, risk = shares * entry, shares * per
    if shares == 0:
        note = "账户太小：1 股都超出单笔风险/单仓上限"
    elif capped:
        note = f"单仓上限 {max_pos_pct:.0%} 截断，实际风险 ${risk:.0f}（计划 ${budget:.0f}）"
    else:
        note = ""
    return {"size_shares": shares, "size_value": round(value, 2), "size_pct": round(value / account, 4),
            "risk_value": round(risk, 2), "capped": bool(capped), "size_note": note}


def quality_score(stop_pct, stop_cap, rs_rank, ema9, ema50, ok_stop) -> float:
    """排序用的质量分 0-100（与 bt_swing.build_signals 同一公式 ×100，见模块 docstring）。"""
    def clip(x):
        return 0.0 if _nan(x) else float(min(1.0, max(0.0, x)))
    rs = 0.5 if _nan(rs_rank) else clip(rs_rank)
    tight = clip(1 - stop_pct / stop_cap) if (stop_pct is not None and stop_cap) else 0.0
    slope = clip((ema9 / ema50 - 1) / 0.20) if not (_nan(ema9) or _nan(ema50) or not ema50) else 0.0
    q = 100 * (0.5 * rs + 0.3 * tight + 0.2 * slope)
    if not ok_stop:
        q *= 0.5
    return round(q, 1)


def rs_percentile(value, pool_values) -> float:
    """value 在 pool_values 里的百分位（0~1；与回测 argsort 秩/(n-1) 同口径）；算不了返回 None。"""
    if _nan(value):
        return None
    arr = pd.to_numeric(pd.Series(pool_values), errors="coerce").dropna().values
    if len(arr) < 5:
        return None
    return float(min(1.0, (arr < value).sum() / max(1, len(arr) - 1)))


def make_setup(ticker: str, name: str, ind: pd.DataFrame, i: int = -1, c: dict = None,
               risk_mult: float = 1.0, member: bool = True, rs_rank: float = None):
    """第 i 根若有形态 → 返回 BRIEF 约定的形态 dict（含止损过滤、R 仓位、quality、note）；否则 None。
    rs_rank：相对强度百分位（0~1），不给取 0.5。"""
    c = _cfg(c)
    found = detect_setups(ind, i, c, member=member)
    if not found:
        return None
    setup, detail = found[0]
    r = ind.iloc[i]
    trigger, stop = float(r["high"]), float(r["low"])
    ok, sp, cap = stop_check(trigger, stop, r["adr"], c)
    size = size_position(trigger, stop, float(c["account_size"]), float(c["risk_pct"]),
                         float(c["max_position_pct"]), risk_mult)
    notes = [detail]
    if len(found) > 1:
        notes.append("同时满足" + "/".join(SETUP_CN[k] for k, _ in found[1:]))
    if not ok and sp is not None:
        if cap is None:
            notes.append("ADR 数据不足，不做")
        elif sp <= float(c.get("min_stop_pct", 0)) or trigger <= stop:
            notes.append(f"止损 {sp:.1%} 无效（K线异常）")
        else:
            notes.append(f"止损 {sp:.1%} > 上限 {cap:.1%}，不做，等收窄")
    if size["size_note"]:
        notes.append(size["size_note"])
    tier = r.get("tier") if not _nan(r.get("tier")) else None
    return {
        "ticker": ticker, "name": name or ticker, "setup": setup, "setup_cn": SETUP_CN[setup],
        "close": _r(r["close"]), "trigger": _r(trigger), "stop": _r(stop),
        "stop_pct": _r(sp, 4), "stop_cap": _r(cap, 4), "adr_pct": _r(r["adr"], 4),
        "dollar_vol_m": _r(r["dvol"] / 1e6 if not _nan(r["dvol"]) else None, 1),
        "tier": tier or "medium", "ema9": _r(r["ema9"]), "ema21": _r(r["ema21"]), "ema50": _r(r["ema50"]),
        "chg_pct": _r(r["chg"], 4), "asof": ind.index[i].strftime("%Y-%m-%d"),
        "size_shares": size["size_shares"], "size_value": size["size_value"], "size_pct": size["size_pct"],
        "risk_value": size["risk_value"], "size_capped": size["capped"], "ok_stop": bool(ok),
        "quality": quality_score(sp, cap, rs_rank, r["ema9"], r["ema50"], ok),
        "rs_rank": _r(rs_rank, 3),
        "note": "；".join(n for n in notes if n),
    }


# ============================================================================
# 三、纸面跟踪状态机（每天从信号日开始重放，幂等）
# ============================================================================
def simulate_signal(ind: pd.DataFrame, signal_date, trigger: float, stop: float, c: dict = None) -> dict:
    """用 signal_date 之后的真实 K 线按规范规则推进一个信号。ind 需含 open/high/low/close/ema9。
    返回 {status, entry_date, entry_price, stop_now, exit_date, exit_price, r_multiple, partial_done,
          shares_frac, bars_held, note, exits}。status ∈ pending/open/closed/expired/skipped；
    open 时 r_multiple 为按最新收盘计的浮动 R。"""
    c = _cfg(c)
    cost = float(c["cost_pct"])
    res = {"status": "pending", "entry_date": None, "entry_price": None, "stop_now": float(stop),
           "exit_date": None, "exit_price": None, "r_multiple": None, "partial_done": False,
           "shares_frac": None, "bars_held": 0, "note": "", "exits": []}
    sd = pd.Timestamp(signal_date)
    after = ind[pd.to_datetime(ind.index) > sd]
    if after.empty:
        return res
    b0 = after.iloc[0]
    max_stop = float(c["max_stop_pct"])
    if b0["open"] > trigger:
        if (b0["open"] - stop) / b0["open"] > max_stop * float(c["open_skip_mult"]):
            res.update(status="skipped",
                       note=f"高开 {b0['open']:.2f} 离止损 {(b0['open'] - stop) / b0['open']:.1%}，放弃不追")
            return res
        fill = float(b0["open"])
    elif b0["high"] >= trigger:
        fill = float(trigger)
    else:
        res.update(status="expired", note="次日未突破触发价，过期")
        return res
    risk_ps = fill - float(stop)
    if risk_ps <= 0:
        res.update(status="skipped", note="止损无效")
        return res
    entry_cost = fill * (1 + cost)
    stop_now, frac, partial = float(stop), 1.0, False
    exits = []

    def _sell(dt, px, f, why):
        exits.append({"date": pd.Timestamp(dt).strftime("%Y-%m-%d"), "price": round(float(px), 4),
                      "frac": round(f, 4), "why": why})

    res.update(entry_date=after.index[0].strftime("%Y-%m-%d"), entry_price=round(fill, 4))
    closed_on = None
    be_days = int(c["be_days"])
    ext, pfrac = float(c["partial_ext"]), float(c["partial_frac"])
    for k in range(len(after)):
        dt, bar = after.index[k], after.iloc[k]
        if k == 0:
            if bar["low"] <= stop_now:              # 同根 K 线成交后触及止损 → 保守判出
                _sell(dt, stop_now, frac, "成交当日触及止损")
                frac, closed_on = 0.0, dt
                break
        else:
            if bar["open"] <= stop_now:              # 跳空低开到止损下方 → 按开盘价出
                _sell(dt, bar["open"], frac, "跳空低开破止损")
                frac, closed_on = 0.0, dt
                break
            if bar["low"] <= stop_now:
                _sell(dt, stop_now, frac, "保本止损" if stop_now >= fill else "触及止损")
                frac, closed_on = 0.0, dt
                break
        e9 = bar.get("ema9")
        if not _nan(e9) and bar["close"] < e9:      # 收盘跌破 9EMA → 收盘全卖
            _sell(dt, bar["close"], frac, "收盘跌破9EMA")
            frac, closed_on = 0.0, dt
            break
        if not partial and not _nan(e9) and bar["close"] >= e9 * (1 + ext):
            _sell(dt, bar["close"], pfrac, f"远离9EMA {bar['close'] / e9 - 1:+.0%} 减仓")
            frac, partial = frac - pfrac, True
        if k >= be_days and not _nan(e9) and e9 > fill and stop_now < fill:
            stop_now = fill                          # 成交日为第 0 天；第 be_days 天收盘起、次日生效
    res["bars_held"] = (k + 1) if len(after) else 0
    realized = sum(e["frac"] * (e["price"] * (1 - cost) - entry_cost) for e in exits)
    res.update(stop_now=round(stop_now, 4), partial_done=partial, shares_frac=round(max(frac, 0.0), 4),
               exits=exits)
    if closed_on is not None or frac <= 1e-9:
        tot = sum(e["frac"] for e in exits)
        res.update(status="closed", exit_date=exits[-1]["date"],
                   exit_price=round(sum(e["frac"] * e["price"] for e in exits) / tot, 4),
                   r_multiple=round(realized / risk_ps, 3), note=exits[-1]["why"])
    else:
        last = float(after["close"].iloc[-1])
        unreal = frac * (last * (1 - cost) - entry_cost)
        res.update(status="open", r_multiple=round((realized + unreal) / risk_ps, 3),
                   note=("已保本" if stop_now >= fill else "持有中") + ("·已减仓15%" if partial else ""))
    return res


def summarize_paper(df: pd.DataFrame) -> dict:
    """swing_signals 全表 → BRIEF 约定的纸面统计（纯函数）。"""
    out = {"n_closed": 0, "win_rate": None, "avg_win_r": None, "avg_loss_r": None,
           "expectancy_r": None, "total_r": None, "open": [], "recent": [],
           "n_pending": 0, "n_open": 0, "n_expired": 0, "n_skipped": 0}
    if df is None or df.empty:
        return out
    df = df.copy()
    df.columns = [str(x).lower() for x in df.columns]
    st = df["status"].astype(str)
    out.update(n_pending=int((st == "pending").sum()), n_open=int((st == "open").sum()),
               n_expired=int((st == "expired").sum()), n_skipped=int((st == "skipped").sum()))
    closed = df[(st == "closed") & df["r_multiple"].notna()]
    r = pd.to_numeric(closed["r_multiple"], errors="coerce").dropna()
    if len(r):
        wins, losses = r[r > 0], r[r <= 0]
        out.update(n_closed=int(len(r)), win_rate=round(float((r > 0).mean()), 4),
                   avg_win_r=round(float(wins.mean()), 3) if len(wins) else None,
                   avg_loss_r=round(float(losses.mean()), 3) if len(losses) else None,
                   expectancy_r=round(float(r.mean()), 3), total_r=round(float(r.sum()), 3))

    def _row(x):
        d = {"id": int(x["id"]) if "id" in x and not _nan(x["id"]) else None,
             "signal_date": str(x.get("signal_date"))[:10], "ticker": x.get("ts_code"),
             "name": x.get("name"), "setup": x.get("setup"),
             "setup_cn": SETUP_CN.get(x.get("setup"), x.get("setup")), "status": x.get("status")}
        for k in ("trigger", "stop", "stop_pct", "entry_price", "stop_now", "exit_price", "r_multiple",
                  "shares_frac", "quality"):
            d[k] = _r(x.get(k), 4)
        for k in ("entry_date", "exit_date"):
            v = x.get(k)
            d[k] = None if _nan(v) else str(v)[:10]
        d["partial_done"] = bool(x.get("partial_done")) if not _nan(x.get("partial_done")) else False
        d["note"] = x.get("note")
        return d
    opn = df[st.isin(["open", "pending"])].sort_values("signal_date", ascending=False)
    out["open"] = [_row(x) for _, x in opn.iterrows()]
    rec = closed.sort_values(["exit_date", "signal_date"], ascending=False).head(10)
    out["recent"] = [_row(x) for _, x in rec.iterrows()]
    return out


# ============================================================================
# 四、观察名单
# ============================================================================
def prelim_pool(univ: pd.DataFrame, c: dict = None) -> pd.DataFrame:
    """东财行情榜 → 资格初筛（价格、当日成交额代理 20 日均额、市值；剔除 ETF/杠杆/权证等）。"""
    c = _cfg(c)
    if univ is None or univ.empty:
        return pd.DataFrame(columns=["ts_code", "name", "market", "pct_chg", "chg_60d"])
    df = univ.copy()
    mkt_ok = df["market"].isin([105, 106, 107]) | df["market"].isna()     # Nasdaq 兜底源没有市场号
    m = ((df["price"] >= float(c["price_min"])) & (df["dollar_vol"] >= float(c["dollar_vol_min"]))
         & (df["mcap"] >= float(c["mcap_min"])) & mkt_ok)
    up = df["name"].astype(str).str.upper()
    kw = up.apply(lambda s: any(k in s for k in EXCLUDE_KW))
    idx = df["ts_code"].isin(list(c.get("env_indices", [])) + ["SPY"])
    return df[m & ~kw & ~idx].reset_index(drop=True)


def select_candidates(pool: pd.DataFrame, prev_watch: dict, position_tickers: list,
                      c: dict = None, limit: int = None) -> list:
    """取数前的候选（有序）：纸面持仓 → 当日涨幅榜(gainer) → 当日跳空≥10%(只判 EP) → 已在名单(carry)
    → 60 日强势补充(rs)。
    返回 [{ts_code, name, market, source(gainer/rs/carry/None), position(bool), ep_check(bool),
           added_date, pct_chg}]。
    观察名单部分 ≤ watch_cap；limit 限制总取数只数（冷启动且有 limit 时一半给涨幅榜、一半给 60 日强势股，
    让 dry-run 也能看到"整理中"的股票，不全是刚大涨完的）。"""
    c = _cfg(c)
    prev_watch = prev_watch or {}
    cands = {}

    def _add(ts, **kw):
        if ts in cands:
            for k, v in kw.items():
                if v is not None and (k != "source" or cands[ts].get("source") is None):
                    cands[ts][k] = v
            return
        base = {"ts_code": ts, "name": ts, "market": None, "source": None, "position": False,
                "ep_check": False, "added_date": None, "pct_chg": None}
        base.update({k: v for k, v in kw.items() if v is not None})
        cands[ts] = base

    pinfo = pool.set_index("ts_code") if pool is not None and not pool.empty else pd.DataFrame()
    for ts in position_tickers or []:
        pw = prev_watch.get(ts, {})
        _add(ts, position=True, name=pw.get("name"), market=pw.get("market"))
    gain = pd.DataFrame()
    if not pinfo.empty:
        g = pool[pool["pct_chg"] >= float(c["watch_min_gain"])]
        gain = g.sort_values("pct_chg", ascending=False).head(int(c["watch_topk"]))
    carry_rows = sorted(prev_watch.values(), key=lambda x: str(x.get("added_date") or ""), reverse=True)
    cold = not carry_rows
    rs_avail = "chg_60d" in pool and pool["chg_60d"].notna().any() if not pinfo.empty else False
    n_gain_cap = int(c["watch_topk"])
    if limit is not None and cold and rs_avail:   # 没有 60 日涨幅（Nasdaq 兜底源）就全给涨幅榜
        n_gain_cap = min(n_gain_cap, int(math.ceil(limit / 2)))
    for _, g in gain.head(n_gain_cap).iterrows():
        pw = prev_watch.get(g["ts_code"], {})
        _add(g["ts_code"], source="gainer", name=g["name"], market=g["market"],
             pct_chg=float(g["pct_chg"]), added_date=pw.get("added_date"))
    if not pinfo.empty and {"open", "prev_close"} <= set(pool.columns):
        gap = pool[(pool["open"] >= pool["prev_close"] * (1 + float(c["ep_gap"]))) & (pool["prev_close"] > 0)]
        n_ep, ep_max = 0, int(c.get("ep_cand_max", 10))
        for _, x in gap.sort_values("pct_chg", ascending=False).iterrows():
            if x["ts_code"] not in cands and x["ts_code"] not in prev_watch:   # 本来就要取的只打标记，不占名额
                if n_ep >= ep_max:
                    continue
                n_ep += 1
            _add(x["ts_code"], ep_check=True, name=x["name"], market=x["market"], pct_chg=float(x["pct_chg"]))
    for pw in carry_rows:
        _add(pw["ts_code"], source="carry", name=pw.get("name"), market=pw.get("market"),
             added_date=pw.get("added_date"))
    n_watch = sum(1 for v in cands.values() if v["source"])
    room = int(c["rs_fill_to"]) - n_watch
    if limit is not None and cold:
        room = max(room, limit - len(cands))
    if room > 0 and not pinfo.empty and "chg_60d" in pool:
        rs = pool[(pool["chg_60d"] > 0) & (pool["chg_60d"] < 10) & ~pool["ts_code"].isin(list(cands))]
        for _, x in rs.sort_values("chg_60d", ascending=False).head(min(room, int(c["rs_topk"]))).iterrows():
            _add(x["ts_code"], source="rs", name=x["name"], market=x["market"], pct_chg=float(x["pct_chg"]))
    # 观察名单上限（按 gainer → carry → rs 的顺序保留），持仓不占名额
    out, n_w = [], 0
    for src in ("gainer", "carry", "rs"):
        for v in cands.values():
            if v["source"] == src:
                if n_w >= int(c["watch_cap"]):
                    v["source"] = None
                else:
                    n_w += 1
    ordered = ([v for v in cands.values() if v["position"]]
               + [v for v in cands.values() if v["source"] == "gainer" and not v["position"]]
               + [v for v in cands.values() if v["ep_check"] and not v["source"] and not v["position"]]
               + [v for s in ("carry", "rs") for v in cands.values()
                  if v["source"] == s and not v["position"]])
    if limit is not None:
        ordered = ordered[:max(0, int(limit))]
    # 回填行情榜信息（名称/市场）
    for v in ordered:
        if not pinfo.empty and v["ts_code"] in pinfo.index:
            row = pinfo.loc[v["ts_code"]]
            v["name"] = v["name"] if v["name"] != v["ts_code"] else row["name"]
            v["market"] = v["market"] or row["market"]
            if v["pct_chg"] is None:
                v["pct_chg"] = float(row["pct_chg"])
    return ordered


def finalize_watchlist(cands: list, frames: dict, asof: str, c: dict = None) -> tuple:
    """取完 K 线后定名单：新进(gainer/rs)按完整资格复核；名单内满 watch_days 个交易日（入选当天算第 1 天）
    或收盘跌破 EMA50 → 移出。返回 (active:list[dict], removed:list[dict])。"""
    c = _cfg(c)
    asof_ts = pd.Timestamp(asof) if asof else None
    active, removed = [], []
    for v in cands:
        src = v.get("source")
        if not src:
            continue
        ts, ind = v["ts_code"], frames.get(v["ts_code"])
        item = {"ticker": ts, "name": v.get("name") or ts, "market": v.get("market"), "source": src,
                "added_date": v.get("added_date"), "eligible": None, "stale": True}
        if ind is None or ind.empty:
            if src == "carry":                         # 取不到数据：老成员先留着，下次再判
                item.update(note="今日取数失败，暂留")
                active.append(item)
            else:
                removed.append({**item, "why": "取不到K线"})
            continue
        last = ind.iloc[-1]
        last_dt = ind.index[-1]
        stale = asof_ts is not None and last_dt < asof_ts
        fails = eligibility_fails(last, c)
        item.update(stale=bool(stale), eligible=not fails, close=_r(last["close"]),
                    chg_pct=_r(last["chg"], 4), tier=last.get("tier") if not _nan(last.get("tier")) else None,
                    adr_pct=_r(last["adr"], 4),
                    dollar_vol_m=_r(last["dvol"] / 1e6 if not _nan(last["dvol"]) else None, 1),
                    ema21_dist=_r(last["close"] / last["ema21"] - 1 if not _nan(last["ema21"]) else None, 4),
                    last_bar=last_dt.strftime("%Y-%m-%d"))
        prev_added = v.get("added_date")
        hit_dt, why = None, None
        if stale:
            why = "行情过期"
        elif src == "rs":
            hit_dt = last_dt if not fails else None
            why = None if hit_dt is not None else "资格不符：" + "、".join(fails)
        elif src == "gainer":
            # 用 K 线复核"资格合格且日涨 ≥3%"，不信行情榜的涨幅：Nasdaq 兜底源实测滞后一个交易日。
            # t 日没涨够但 t-1 日够 → 按回测口径它 t-1 日就进了名单，入选日记为 t-1。
            for back in (0, 1):
                j = len(ind) - 1 - back
                if j < 1:
                    break
                row = ind.iloc[j]
                if not eligibility_fails(row, c) and not _nan(row["chg"]) \
                        and row["chg"] >= float(c["watch_min_gain"]):
                    hit_dt = ind.index[j]
                    break
            if hit_dt is None:
                why = ("资格不符：" + "、".join(fails)) if fails else f"K线日涨幅不足 {float(c['watch_min_gain']):.0%}"
        if src in ("gainer", "rs"):
            if hit_dt is None:
                if prev_added:                         # 原本就在名单里：按 carry 处理，不重置计时
                    src = "carry"
                    item["source"] = "carry"
                else:
                    removed.append({**item, "why": why})
                    continue
            else:                                      # 新进/再次上榜：重置 20 日计时
                if prev_added and pd.Timestamp(prev_added) > hit_dt:
                    hit_dt = pd.Timestamp(prev_added)
                item["added_date"] = hit_dt.strftime("%Y-%m-%d")
        added = pd.Timestamp(item["added_date"]) if item.get("added_date") else last_dt
        days_in = int((pd.to_datetime(ind.index) >= added).sum())   # 入选当天算第 1 天
        item["days_in"] = days_in
        item["added_date"] = added.strftime("%Y-%m-%d")
        if src == "carry" and days_in > int(c["watch_days"]):
            removed.append({**item, "why": f"已满 {c['watch_days']} 个交易日"})
            continue
        if not _nan(last["ema50"]) and last["close"] < last["ema50"]:
            removed.append({**item, "why": "收盘跌破EMA50"})
            continue
        active.append(item)
    return active, removed


# ============================================================================
# 五、数据库读写（swing_* 表；trigger/stop 是 MySQL 关键字，一律反引号）
# ============================================================================
def _exec(sql: str, rows):
    from sqlalchemy import text
    with db.get_engine().begin() as conn:
        conn.execute(text(sql), rows)


def _clean(v):
    if isinstance(v, (np.floating, float)):
        return None if math.isnan(float(v)) or math.isinf(float(v)) else float(v)
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.bool_):
        return bool(v)
    return v


def _load_watchlist() -> dict:
    df = db.read_sql("SELECT ts_code, name, market, added_date, last_seen, source, active "
                     "FROM swing_watchlist WHERE active=1")
    out = {}
    for _, r in df.iterrows():
        out[r["ts_code"]] = {"ts_code": r["ts_code"], "name": r["name"],
                             "market": int(r["market"]) if not _nan(r["market"]) else None,
                             "added_date": None if _nan(r["added_date"]) else str(r["added_date"])[:10],
                             "source": r["source"]}
    return out


def _save_watchlist(active: list, removed: list, prev: dict, asof: str):
    sql = ("INSERT INTO swing_watchlist (ts_code, name, market, added_date, last_seen, source, active) "
           "VALUES (:ts_code, :name, :market, :added_date, :last_seen, :source, :active) "
           "ON DUPLICATE KEY UPDATE name=VALUES(name), market=VALUES(market), added_date=VALUES(added_date), "
           "last_seen=VALUES(last_seen), source=VALUES(source), active=VALUES(active)")
    rows = [{"ts_code": a["ticker"], "name": str(a.get("name") or a["ticker"])[:80],
             "market": _clean(a.get("market")), "added_date": a.get("added_date"),
             "last_seen": asof, "source": a["source"], "active": 1} for a in active]
    rows += [{"ts_code": r["ticker"], "name": str(r.get("name") or r["ticker"])[:80],
              "market": _clean(r.get("market")), "added_date": r.get("added_date"),
              "last_seen": asof, "source": r.get("source"), "active": 0}
             for r in removed if r["ticker"] in prev]       # 从没进过名单的不落库
    if rows:
        _exec(sql, rows)


_SIG_COLS = ("id, signal_date, ts_code, name, setup, `trigger`, `stop`, stop_pct, adr_pct, quality, status, "
             "entry_date, entry_price, shares_frac, stop_now, exit_date, exit_price, r_multiple, partial_done, note")


def _load_open_signals() -> list:
    df = db.read_sql(f"SELECT {_SIG_COLS} FROM swing_signals WHERE status IN ('pending','open')")
    df.columns = [str(x).lower() for x in df.columns]
    return df.to_dict("records")


def _update_signal(sig_id: int, res: dict):
    _exec("UPDATE swing_signals SET status=:status, entry_date=:entry_date, entry_price=:entry_price, "
          "shares_frac=:shares_frac, stop_now=:stop_now, exit_date=:exit_date, exit_price=:exit_price, "
          "r_multiple=:r_multiple, partial_done=:partial_done, note=:note WHERE id=:id",
          [{"id": int(sig_id), "status": res["status"], "entry_date": res["entry_date"],
            "entry_price": _clean(res["entry_price"]), "shares_frac": _clean(res["shares_frac"]),
            "stop_now": _clean(res["stop_now"]), "exit_date": res["exit_date"],
            "exit_price": _clean(res["exit_price"]), "r_multiple": _clean(res["r_multiple"]),
            "partial_done": int(bool(res["partial_done"])), "note": str(res.get("note") or "")[:255]}])


def _insert_signals(setups: list, env_verdict: str):
    if not setups:
        return
    rows = [{"signal_date": s["asof"], "ts_code": s["ticker"], "name": str(s["name"])[:80],
             "setup": s["setup"], "trigger": s["trigger"], "stop": s["stop"], "stop_pct": s["stop_pct"],
             "adr_pct": s["adr_pct"], "quality": s["quality"],
             "note": f"env={env_verdict or '-'}；{s.get('note') or ''}"[:255]} for s in setups]
    _exec("INSERT IGNORE INTO swing_signals (signal_date, ts_code, name, setup, `trigger`, `stop`, stop_pct, "
          "adr_pct, quality, status, partial_done, note) VALUES (:signal_date, :ts_code, :name, :setup, "
          ":trigger, :stop, :stop_pct, :adr_pct, :quality, 'pending', 0, :note)", rows)


def jsonable(obj):
    """numpy/pandas/日期/NaN → 可 JSON 序列化。"""
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, (pd.Timestamp, datetime)):
        return obj.strftime("%Y-%m-%d")
    if hasattr(obj, "isoformat") and not isinstance(obj, str):
        return obj.isoformat()
    obj = _clean(obj)
    return obj


def _save_snapshot(payload: dict):
    _exec("INSERT INTO swing_snapshot (asof, payload, created_at) VALUES (:asof, :payload, :created_at) "
          "ON DUPLICATE KEY UPDATE payload=VALUES(payload), created_at=VALUES(created_at)",
          [{"asof": payload["asof"], "payload": json.dumps(jsonable(payload), ensure_ascii=False),
            "created_at": datetime.now()}])


def public_payload(out: dict, setups_pub: list = None) -> dict:
    """存进 swing_snapshot 的公开版本（web 的 /api/swing/snapshot 不需要令牌）：
    剥掉一切由个人交易日志派生的内容——env.own、被自己连亏改写过的结论/理由（换成 market_env 给的
    不含 own 的 env.public）、按降风险算的建议股数（换成标准风险的 setups_pub）。
    私有的 Lark 卡片仍用 run() 的完整结果。纯函数，不改入参。"""
    p = dict(out)
    env = p.get("env")
    if isinstance(env, dict):
        env = dict(env)
        pub = env.pop("public", None)
        if isinstance(pub, dict) and pub.get("verdict"):
            env.update({k: pub[k] for k in ("verdict", "emoji", "label", "reasons") if k in pub})
        elif env.get("own") is not None:     # 没有公开结论可换：至少去掉由 own 生成的理由句
            env["reasons"] = [r for r in env.get("reasons") or [] if not market_env.is_own_reason(r)]
        env["own"] = None
        p["env"] = env
    if setups_pub is not None:
        p["setups"] = setups_pub
    return jsonable(p)


def latest_snapshot() -> dict | None:
    """最近一次扫描结果（只读库，不调外部接口）：{asof, env, setups, watchlist, paper, ...}；无数据/失败 None。"""
    try:
        df = db.read_sql("SELECT asof, payload FROM swing_snapshot ORDER BY asof DESC LIMIT 1")
        if df.empty:
            return None
        p = df.iloc[0]["payload"]
        p = p if isinstance(p, dict) else json.loads(p)
        for k in ("env", "paper"):
            p.setdefault(k, None)
        for k in ("setups", "watchlist"):
            p.setdefault(k, [])
        p["asof"] = str(p.get("asof") or df.iloc[0]["asof"])[:10]
        return p
    except Exception as e:
        print("[swing] 读快照失败:", repr(e)[:120])
        return None


def paper_stats() -> dict | None:
    """纸面跟踪统计（只读库）：{n_closed, win_rate, avg_win_r, avg_loss_r, expectancy_r, total_r, open, recent}。"""
    try:
        df = db.read_sql(f"SELECT {_SIG_COLS} FROM swing_signals")
        return summarize_paper(df)
    except Exception as e:
        print("[swing] 读纸面跟踪失败:", repr(e)[:120])
        return None


# ============================================================================
# 六、一次完整扫描
# ============================================================================
def run(dry_run: bool = False, limit: int = None) -> dict:
    """执行一次完整扫描。返回 {asof, env, setups, watchlist, paper, universe_size, credits_used, errors}。
    dry_run=True：完全不读写数据库（纯内存，K 线全量现取）；limit：最多取多少只个股的行情（省额度，指数另算）。"""
    c = _cfg()
    use_db = not dry_run
    credits0 = data.td_credits_used()
    data.em_reset_breaker()
    errors = []
    if use_db:
        try:
            db.ensure_swing_schema()
        except Exception as e:   # 库不可用/迁移失败：降级为不读写库照样出卡片（纸面跟踪下次重放补上），不让推送崩
            use_db = False
            errors.append(f"数据库不可用，本次不读写库：{type(e).__name__}")
            print("[swing] ensure_swing_schema 失败，降级为不读写库:", repr(e)[:160])

    # 1) 候选池：东财行情榜（按成交额）
    try:
        univ = data.us_liquid_list(pages=int(c["em_pages"]), sort="f6",
                                   pause_s=float(c["em_page_pause_s"]),
                                   min_dollar_vol=float(c["dollar_vol_min"]))
    except Exception as e:
        univ = pd.DataFrame()
        print("[swing] 东财行情榜失败，改用 Nasdaq screener:", repr(e)[:120])
        try:
            univ = data.us_list_nasdaq()
            errors.append("东财行情榜失败，已改用 Nasdaq screener（无 60 日涨幅/开盘价：强势补充与 EP 跳空候选停用）")
        except Exception as e2:
            errors.append(f"行情榜全部失败：{repr(e)[:50]} / {repr(e2)[:50]}")
            print("[swing] Nasdaq screener 也失败:", repr(e2)[:120])
    pool = prelim_pool(univ, c)
    print(f"[swing] 行情榜 {len(univ)} 行 → 初筛 {len(pool)} 只")

    prev_watch, open_sigs, sig_ok = {}, [], True
    if use_db:
        try:
            prev_watch = _load_watchlist()
        except Exception as e:
            errors.append(f"读观察名单失败：{type(e).__name__}")
            print("[swing] 读观察名单失败:", repr(e)[:160])
        try:
            open_sigs = _load_open_signals()
        except Exception as e:      # 读不到在途信号就不写新信号，免得同一只重复记
            sig_ok = False
            errors.append(f"读纸面信号失败：{type(e).__name__}")
            print("[swing] 读纸面信号失败:", repr(e)[:160])
    pos_tickers = sorted({s["ts_code"] for s in open_sigs})
    cands = select_candidates(pool, prev_watch, pos_tickers, c, limit)
    print(f"[swing] 候选 {len(cands)} 只（纸面 {len(pos_tickers)}，名单内 {len(prev_watch)}）")

    # 2) 指数（交易环境）+ 个股 K 线
    idx_frames = {}
    for n in c["env_indices"]:
        try:
            df, src = data.get_ohlcv(n, market=market_env.EM_MARKET.get(n), use_db=use_db)
        except Exception as e:
            df, src = pd.DataFrame(), "none"
            errors.append(f"{n} 取数失败")
            print(f"[swing] {n} 取数异常: {repr(e)[:80]}")
        idx_frames[n] = df
        print(f"[swing] {n} {len(df)} 根（{src}）")
    frames, srcs = {}, {}
    for v in cands:
        ts = v["ts_code"]
        try:
            df, src = data.get_ohlcv(ts, market=v.get("market"), use_db=use_db)
        except Exception as e:
            df, src = pd.DataFrame(), "none"
            errors.append(f"{ts} 取数失败")
            print(f"[swing] {ts} 取数异常: {repr(e)[:80]}")
        srcs[ts] = src
        frames[ts] = compute_indicators(df, c) if len(df) >= 30 else None
        print(f"[swing] {ts:6} {len(df):3} 根（{src}）")
    last_dates = [pd.to_datetime(f.index).max() for f in idx_frames.values() if f is not None and len(f)]
    last_dates += [f.index.max() for f in frames.values() if f is not None and len(f)]
    asof = max(last_dates).strftime("%Y-%m-%d") if last_dates else datetime.now().strftime("%Y-%m-%d")

    # 3) 观察名单
    active, removed = finalize_watchlist(cands, frames, asof, c)
    if use_db:
        try:
            _save_watchlist(active, removed, prev_watch, asof)
        except Exception as e:
            errors.append(f"名单落库失败：{type(e).__name__}")
            print("[swing] 名单落库失败:", repr(e)[:160])

    # 4) 交易环境
    wf = {a["ticker"]: frames[a["ticker"]] for a in active if frames.get(a["ticker"]) is not None}
    env = market_env.assess(index_frames=idx_frames, watch_frames=wf,
                            own=(None if dry_run else market_env._AUTO), use_db=use_db, asof=asof)
    own = (env or {}).get("own") if env else (market_env._own_status() if use_db else None)
    risk_mult = 1.0
    if own and not _nan(own.get("risk_mult")):
        risk_mult = float(own["risk_mult"])

    # 5) 纸面跟踪推进（每天从信号日重放）
    holding = set()
    for s in open_sigs:
        own_today = str(s["signal_date"])[:10] >= asof   # 同一交易日重跑：今天自己记的信号不算"已有持仓"
        ind = frames.get(s["ts_code"])
        if ind is None:
            if not own_today:
                holding.add(s["ts_code"])
            continue
        try:
            res = simulate_signal(ind, s["signal_date"], float(s["trigger"]), float(s["stop"]), c)
            if use_db:
                _update_signal(s["id"], res)
            if res["status"] in ("pending", "open") and not own_today:
                holding.add(s["ts_code"])
        except Exception as e:
            errors.append(f"纸面 {s['ts_code']} 推进失败")
            print(f"[swing] 纸面 {s['ts_code']} 推进失败: {repr(e)[:80]}")

    # 6) 形态：名单内看三种；名单外只看 EP（行情榜上当日跳空 ≥10% 的）
    pool_rs = pool["chg_60d"] if "chg_60d" in pool else pd.Series(dtype=float)
    pool_rs_map = {}
    if len(pool_rs.dropna()) >= 5:
        rk = pool_rs.rank(method="min") - 1
        pool_rs_map = dict(zip(pool["ts_code"], (rk / max(1, pool_rs.notna().sum() - 1)).values))
    act_set = {a["ticker"] for a in active}
    scan = [(a, True) for a in active]
    for v in cands:
        if v.get("ep_check") and v["ts_code"] not in act_set:
            scan.append(({"ticker": v["ts_code"], "name": v.get("name") or v["ts_code"], "source": "ep",
                          "stale": False}, False))
    # setups：给私有卡片（按纪律降风险后的股数）；setups_pub：给公开快照/纸面信号（标准风险，
    # 不带"风险系数×"字样）——risk_mult 来自个人交易日志，公开接口里不能看出自己是否在连亏。
    setups, setups_pub = [], []
    for a, member in scan:
        ind = frames.get(a["ticker"])
        if ind is None or a.get("stale") or (asof and ind.index[-1] < pd.Timestamp(asof)):
            continue
        rs_rank = pool_rs_map.get(a["ticker"])
        if _nan(rs_rank):
            rs_rank = rs_percentile(ind["ret63"].iloc[-1], pool_rs.values)
        st = make_setup(a["ticker"], a["name"], ind, -1, c, risk_mult, member=member, rs_rank=rs_rank)
        if st is None:
            continue
        pub = (make_setup(a["ticker"], a["name"], ind, -1, c, 1.0, member=member, rs_rank=rs_rank)
               if risk_mult != 1.0 else None)
        for x in (st, pub):
            if x is None:
                continue
            if a["ticker"] in holding:
                x["note"] = "已有纸面持仓，不重复记；" + x["note"]
            x["source"] = a["source"]
        if risk_mult != 1.0:
            st["note"] += f"；风险系数×{risk_mult:g}（按纪律降风险）"
        a["setup"] = st["setup"]
        setups.append(st)
        setups_pub.append(pub or st)
    setups.sort(key=lambda x: (not x["ok_stop"], -x["quality"]))
    setups_pub.sort(key=lambda x: (not x["ok_stop"], -x["quality"]))
    env_pub_verdict = ((env or {}).get("public") or env or {}).get("verdict")
    if use_db and sig_ok:
        try:
            _insert_signals([s for s in setups_pub if s["ok_stop"] and s["ticker"] not in holding],
                            env_pub_verdict)
        except Exception as e:
            errors.append(f"信号落库失败：{type(e).__name__}")
            print("[swing] 信号落库失败:", repr(e)[:160])

    # 7) 纸面统计 + 快照
    paper = paper_stats() if use_db else None
    active.sort(key=lambda x: ({"gainer": 0, "carry": 1, "rs": 2}.get(x["source"], 3),
                               -(x.get("chg_pct") or 0)))
    out = {"asof": asof, "env": env, "setups": setups, "watchlist": active, "paper": paper,
           "universe_size": int(len(pool)), "credits_used": data.td_credits_used() - credits0,
           "errors": errors, "removed": [{"ticker": r["ticker"], "why": r["why"]} for r in removed],
           "sources": srcs, "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    out = jsonable(out)
    if use_db:
        try:
            _save_snapshot(public_payload(out, setups_pub))
        except Exception as e:
            out["errors"].append(f"快照落库失败：{type(e).__name__}")
            print("[swing] 快照落库失败:", repr(e)[:160])
    print(f"[swing] {asof} 名单 {len(active)} 只 · 形态 {len(setups)} 个"
          f"（止损合格 {sum(1 for s in setups if s['ok_stop'])}）· Twelve Data {out['credits_used']} 次")
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="美股短线动量扫描")
    ap.add_argument("--dry-run", action="store_true", help="不读写数据库")
    ap.add_argument("--limit", type=int, default=None, help="最多取多少只个股行情")
    a = ap.parse_args()
    r = run(dry_run=a.dry_run, limit=a.limit)
    print(json.dumps({k: r[k] for k in ("asof", "universe_size", "credits_used", "errors")}, ensure_ascii=False))
