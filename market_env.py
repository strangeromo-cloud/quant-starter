"""
交易环境评估：今天适不适合开新仓（Martin Luk 的"何时少做"三看）。

为什么要有它：冠军打法胜率只有约 22%，靠"亏小赚大"活着。最伤的不是单笔亏损，而是在
不适合的行情里连续被止损——2025 年 12 月震荡里她多空反复被扫，从高点回撤 26.6%，85 笔里
近一半不该做。所以每天开盘前先看三样，再决定出手力度：
  ① 指数：QQQ 和 IWM 站上**向上的** 9EMA 和 21EMA 最适合出手；
     指数连涨几周没回调（延伸）→ 别大量开新仓。
  ② 观察名单宽度：强档(close>EMA50>EMA150) vs 弱档(close<EMA50<EMA150)，强档越来越少 → 行情变差。
     名单不足 10 只时退用 20 只板块/主题 ETF（sector_rotation.SECTORS+THEMES），source 标明。
  ③ 自己的单子：新仓接连止损 → 少做降风险；连亏一串 → 停 1-2 天（最重要）。
     来自 journal.streak_status()（web 模块，懒加载；没有/失败就是 None，不影响其余判断）。

结论三档：
  🔴 stop 休息观望   ：自己要求暂停 / 两个指数都在 21EMA 下 / 强档占比 <20%
  🟡 selective 精选少做：指数延伸 / 不是两个指数都在出手窗口 / 强档占比 5 日内掉 ≥10pp / 自己连亏 ≥3
  🟢 go 适合出手      ：以上都没有
（"不是两个都在窗口"包含一个都不在、但又没到两个都跌破 21EMA 的情形——那种时候更不该放手做。）

口径：rising9 = EMA9 比前一日高；rising21 = EMA21 比 5 个交易日前高（与个股趋势叠层同一口径）。
QQQ/IWM 日线用 Twelve Data（每天 2 次），缓存进 recent_price（含 open/volume）。仅技术信号，非投资建议。
"""
from __future__ import annotations

import pandas as pd

import config
import data

_AUTO = object()          # own 参数哨兵：默认懒加载 journal.streak_status()
EM_MARKET = {"QQQ": 105, "IWM": 107, "SPY": 107}  # 东财兜底用的市场号（行情榜 f13 实测）
VERDICT_META = {"go": ("🟢", "适合出手"), "selective": ("🟡", "精选少做"), "stop": ("🔴", "休息观望")}
# decide() 由 env['own']（个人交易日志）生成的理由句一律以这两个前缀开头——web 与公开快照靠它脱敏，别改
OWN_REASON_PREFIXES = ("自己的单子", "自己连亏")


def is_own_reason(r) -> bool:
    return str(r).startswith(OWN_REASON_PREFIXES)


def _c(key, default=None):
    return config.SWING.get(key, default)


def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False, min_periods=span).mean()


def index_state(bars: pd.DataFrame) -> dict | None:
    """单个指数 ETF 的出手窗口 + 延伸判定。bars: index=日期，至少含 close（low 可选）。数据不足返回 None。"""
    if bars is None or bars.empty or "close" not in bars:
        return None
    df = bars.copy()
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    close = pd.to_numeric(df["close"], errors="coerce").dropna()
    if len(close) < 30:
        return None
    e9, e21 = _ema(close, 9), _ema(close, 21)
    lag21 = int(_c("ema21_rise_days", 5))
    if pd.isna(e21.iloc[-1]) or pd.isna(e21.iloc[-1 - lag21]) or pd.isna(e9.iloc[-2]):
        return None
    cl, v9, v21 = float(close.iloc[-1]), float(e9.iloc[-1]), float(e21.iloc[-1])
    above9, above21 = cl > v9, cl > v21
    rising9 = v9 > float(e9.iloc[-2])
    rising21 = v21 > float(e21.iloc[-1 - lag21])
    ext = close / e21 - 1
    ext_pct = float(ext.iloc[-1])
    # 连续在 21EMA 上方的天数
    above = (close > e21).values
    days = 0
    for a in above[::-1]:
        if not a:
            break
        days += 1
    # 延伸 A：连续 ≥N 天在 21EMA 上 且 距 21EMA 超过近一年 80 分位
    yr = ext.dropna().tail(252)
    pctl = float(yr.quantile(float(_c("env_ext_pctl", 0.80)))) if len(yr) >= 60 else None
    ext_a = bool(pctl is not None and days >= int(_c("env_ext_days", 15)) and ext_pct > pctl)
    # 延伸 B：连续 4 周上涨（周收盘逐周走高）且这 4 周里日线低点没回到过 21EMA
    weeks = int(_c("env_ext_weeks", 4))
    wk = close.resample("W-FRI").last().dropna()
    ext_b = False
    if len(wk) >= weeks + 1:
        last = wk.tail(weeks + 1)
        up = bool((last.diff().dropna() > 0).all())
        if up:
            since = last.index[0]                     # 4 周前那个周五之后的所有日线
            low = pd.to_numeric(df["low"], errors="coerce") if "low" in df else close
            low = low.reindex(close.index).fillna(close)
            seg = close.index > since
            ext_b = bool(seg.any() and (low[seg] > e21[seg]).all())
    return {"close": round(cl, 2), "ema9": round(v9, 2), "ema21": round(v21, 2),
            "above9": bool(above9), "above21": bool(above21),
            "rising9": bool(rising9), "rising21": bool(rising21),
            "ext_pct": round(ext_pct, 4), "days_above21": int(days),
            "ext_pctl": round(pctl, 4) if pctl is not None else None,
            "in_window": bool(above9 and above21 and rising9 and rising21),
            "extended": bool(ext_a or ext_b), "ext_a": ext_a, "ext_b": ext_b,
            "asof": close.index[-1].strftime("%Y-%m-%d")}


def _tier_at(close: pd.Series, e50: pd.Series, e150: pd.Series, i: int):
    try:
        c, a, b = float(close.iloc[i]), float(e50.iloc[i]), float(e150.iloc[i])
    except (IndexError, TypeError, ValueError):
        return None
    if any(pd.isna(x) for x in (c, a, b)):
        return None
    if c > a > b:
        return "strong"
    if c < a < b:
        return "weak"
    return "medium"


def breadth_from_frames(frames: dict, source: str, lag: int = None) -> dict | None:
    """强/中/弱档计数（最新一根）+ lag 个交易日前的强档占比。frames: {代码: 含 close 的 DataFrame 或 Series}。
    已算好 ema50/ema150 列就直接用，否则自己算。可用标的不足 3 只返回 None。"""
    lag = int(_c("env_breadth_lag", 5)) if lag is None else lag
    cnt = {"strong": 0, "medium": 0, "weak": 0}
    prev_strong, prev_total = 0, 0
    for _, f in (frames or {}).items():
        if f is None or len(f) == 0:
            continue
        if isinstance(f, pd.Series):
            f = f.to_frame("close")
        close = pd.to_numeric(f["close"], errors="coerce")
        e50 = f["ema50"] if "ema50" in f else _ema(close, 50)
        e150 = f["ema150"] if "ema150" in f else _ema(close, 150)
        t = _tier_at(close, e50, e150, -1)
        if t is None:
            continue
        cnt[t] += 1
        tp = _tier_at(close, e50, e150, -1 - lag) if len(close) > lag + 1 else None
        if tp is not None:
            prev_total += 1
            prev_strong += int(tp == "strong")
    total = sum(cnt.values())
    if total < 3:
        return None
    return {**cnt, "total": total, "strong_pct": round(cnt["strong"] / total, 4),
            "strong_pct_prev": round(prev_strong / prev_total, 4) if prev_total >= 3 else None,
            "source": source}


def breadth_from_etfs(loader=None) -> dict | None:
    """退路：20 只板块/主题 ETF 的强弱档（读 recent_price 缓存，sector_rotation 每天 8:00 已刷新，零额度）。"""
    try:
        import sector_rotation
        loader = loader or data.load_recent_price
        frames = {}
        for sym in sector_rotation.SECTORS + sector_rotation.THEMES:
            s = loader(sym)
            if s is not None and len(s) >= 160:
                frames[sym] = s
        return breadth_from_frames(frames, source=f"板块/主题ETF {len(frames)}只")
    except Exception as e:
        print("[env] ETF 宽度失败:", repr(e)[:100])
        return None


def _own_status():
    """懒加载 journal.streak_status()（web 模块）。没有这个模块或任何异常 → None。"""
    try:
        import journal  # noqa: WPS433（web 任务实现）
        return journal.streak_status()
    except ImportError:
        return None
    except Exception as e:
        print("[env] journal.streak_status 失败:", repr(e)[:100])
        return None


def _pct(x) -> str:
    return f"{x:.0%}" if x is not None else "—"


def decide(index: dict, breadth: dict | None, own: dict | None) -> tuple:
    """根据三看给结论。返回 (verdict, reasons, extended)。纯函数，便于测试。"""
    names = [k for k in _c("env_indices", ["QQQ", "IWM"])]
    stop_r, sel_r, ok_r = [], [], []
    # ③ 自己的单子（最重要，放最前）
    if own:
        if own.get("pause"):
            stop_r.append(f"自己的单子：{own.get('note') or '连亏一串'}，先停 1-2 天")
        elif int(own.get("consecutive_losses") or 0) >= int(_c("env_loss_streak", 3)):
            sel_r.append(f"自己连亏 {int(own['consecutive_losses'])} 笔，降风险、只挑最好的")
    # ① 指数
    have = {n: index.get(n) for n in names if index.get(n)}
    missing = [n for n in names if n not in have]
    below = [n for n, s in have.items() if not s["above21"]]
    in_win = [n for n, s in have.items() if s["in_window"]]
    if len(have) == len(names) and len(below) == len(names):
        stop_r.append(f"{'、'.join(names)} 都在 21EMA 下方，指数不配合")
    elif len(in_win) == len(names):
        ok_r.append(f"{'、'.join(names)} 都站上向上的 9EMA 和 21EMA")
    else:
        why = []
        for n, s in have.items():
            if s["in_window"]:
                continue
            bits = []
            if not s["above9"]:
                bits.append("跌破9EMA")
            elif not s["rising9"]:
                bits.append("9EMA拐头")
            if not s["above21"]:
                bits.append("跌破21EMA")
            elif not s["rising21"]:
                bits.append("21EMA走平")
            why.append(f"{n}{'/'.join(bits) or '不在窗口'}")
        why += [f"{n}缺数据" for n in missing]
        if in_win:
            sel_r.append(f"只有 {'、'.join(in_win)} 在出手窗口（{'；'.join(why)}）")
        else:
            sel_r.append(f"两个指数都不在出手窗口（{'；'.join(why)}）")
    extended = False
    for n, s in have.items():
        if s.get("extended"):
            extended = True
            if s.get("ext_a"):
                sel_r.append(f"{n} 延伸：连续 {s['days_above21']} 天在 21EMA 上，距 21EMA "
                             f"{s['ext_pct']:+.1%}（高于近一年 80 分位 {s['ext_pctl']:+.1%}），别大量开新仓")
            else:
                sel_r.append(f"{n} 延伸：连续 {int(_c('env_ext_weeks', 4))} 周上涨没回过 21EMA，别大量开新仓")
    # ② 宽度
    if breadth and breadth.get("strong_pct") is not None:
        sp, prev = breadth["strong_pct"], breadth.get("strong_pct_prev")
        if sp < float(_c("env_strong_stop", 0.20)):
            stop_r.append(f"强档仅 {_pct(sp)}（<20%），{breadth['source']}普遍走弱")
        elif prev is not None and prev - sp >= float(_c("env_breadth_drop", 0.10)) - 1e-9:
            sel_r.append(f"强档占比 5 日内 {_pct(prev)}→{_pct(sp)}，强势股在变少")
        else:
            trend = f"（5日前 {_pct(prev)}）" if prev is not None else ""
            ok_r.append(f"强档占比 {_pct(sp)}{trend}，{breadth['source']}")
    else:
        ok_r.append("宽度数据不足，未参与判断")
    if stop_r:
        return "stop", stop_r + sel_r, extended
    if sel_r:
        return "selective", sel_r + ok_r, extended
    return "go", ok_r, extended


def assess(index_frames: dict = None, watch_frames: dict = None, own=_AUTO,
           use_db: bool = True, asof: str = None) -> dict | None:
    """交易环境结论（接口约定见 BRIEF）。任何环节失败返回 None（调用方不显示环境块，不影响推送）。
    index_frames: {QQQ: OHLCV DataFrame, IWM: ...}；不传则自己取（Twelve Data → 东财兜底，use_db 时缓存）。
    watch_frames: 观察名单 {代码: DataFrame}；不足 env_min_watch 只时退用 ETF 宽度（需 use_db）。
    own: 默认懒加载 journal.streak_status()；dry-run 传 None。
    额外字段 public：own 非空时给出"不含个人交易日志"的 {verdict, emoji, label, reasons}（公开快照用），否则 None。"""
    try:
        names = list(_c("env_indices", ["QQQ", "IWM"]))
        if index_frames is None:
            index_frames = {}
            for n in names:
                df, src = data.get_ohlcv(n, market=EM_MARKET.get(n), use_db=use_db)
                print(f"[env] {n} 日线 {len(df)} 根（{src}）")
                index_frames[n] = df
        index = {}
        for n in names:
            st = index_state(index_frames.get(n))
            if st:
                index[n] = st
        if not index:
            print("[env] 指数数据全部缺失，不评估环境")
            return None
        wf = {k: v for k, v in (watch_frames or {}).items() if v is not None and len(v)}
        breadth = None
        if len(wf) >= int(_c("env_min_watch", 10)):
            breadth = breadth_from_frames(wf, source=f"观察名单 {len(wf)}只")
        elif use_db:
            breadth = breadth_from_etfs()
        if breadth is None and len(wf) >= 3:
            breadth = breadth_from_frames(wf, source=f"观察名单(仅{len(wf)}只)")
        if own is _AUTO:
            own = _own_status() if use_db else None
        # 指数行情过期（今日 TD/东财都失败、用了旧缓存）：照旧评估，但明说，且不给"适合出手"
        stale = [n for n, s in index.items() if asof and s["asof"] < str(asof)[:10]]
        stale_msg = ("、".join(f"{n} 行情停在 {index[n]['asof']}" for n in stale)
                     + "（今日取数失败，用的旧数据），环境判断可能滞后") if stale else None

        def _judge(o):
            v, rs, ext = decide(index, breadth, o)
            if stale_msg:
                v, rs = ("selective", [stale_msg] + rs) if v == "go" else (v, rs + [stale_msg])
            return v, rs, ext
        verdict, reasons, extended = _judge(own)
        emoji, label = VERDICT_META[verdict]
        # 公开版结论：不含个人交易日志（own）——公开快照/网页用它，个人连亏状态不外泄
        public = None
        if own:
            pv, pr, _ = _judge(None)
            public = {"verdict": pv, "emoji": VERDICT_META[pv][0], "label": VERDICT_META[pv][1], "reasons": pr}
        idx_out = {n: {k: s[k] for k in ("close", "ema9", "ema21", "above9", "above21", "rising9",
                                         "rising21", "ext_pct", "days_above21")}
                   for n, s in index.items()}
        return {"verdict": verdict, "emoji": emoji, "label": label, "reasons": reasons,
                "index": idx_out, "extended": bool(extended), "breadth": breadth,
                "own": own if own else None, "public": public,
                "asof": asof or max(s["asof"] for s in index.values())}
    except Exception as e:
        print("[env] 评估失败:", repr(e)[:160])
        return None
