"""
Martin Luk 式短线动量（回调 / 突破 / EP + 紧止损 + R 仓位）日线回测。

为什么要有这个脚本：
  视频里的冠军打法是盘中精细入场，胜率只有约 22%，全靠"亏小赚大"。这种打法最容易被
  幸存者偏差和"看着漂亮的几张图"骗。所以这里用**规范日线规则 v1**（与生产纸面跟踪同一套）
  在一个随机抽样的美股池上做组合层面的逐日模拟，诚实回答：扣成本以后，它比"同池等权买入持有"
  和 QQQ 到底强不强、两段样本外是否都成立。

用法（不连数据库、不发 Lark；只有 pull 访问外网）：
  python bt_swing.py pull      # 建池（东财行情榜，502 时退到 Nasdaq screener；种子 42 抽 300 只）
                               # + Twelve Data 拉 5000 根日线缓存（需要 TWELVEDATA_KEY）
  python bt_swing.py run       # 基准规则 + 全部变体 + 对照 + 诊断，写 _results.json（只读缓存）
  python bt_swing.py run --quick   # 只跑基准规则（调试用）
  python bt_swing.py report    # 把 _results.json 渲染成 Markdown 表（docs/SWING_RESEARCH.md 用）

数据：Twelve Data 日线（拆股已复权、不含分红），缓存在 SWING_CACHE 目录 <SYM>.csv.gz。
      全池拉完写 _PULL_DONE 标记（别的任务在等它才敢调 Twelve Data，免费额度与生产共用）。
结论与方法说明见 docs/SWING_RESEARCH.md。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

# ============================================================================
# 路径与数据源常量
# ============================================================================
# 缓存放仓库 output/swing_cache（已 gitignore，300 只日线约几十 MB）；环境变量 SWING_CACHE 可覆盖
SWING_CACHE = os.environ.get("SWING_CACHE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "output", "swing_cache")
UNIVERSE_CSV = os.path.join(SWING_CACHE, "universe.csv")
PULL_DONE = os.path.join(SWING_CACHE, "_PULL_DONE")
RESULT_JSON = os.path.join(SWING_CACHE, "_results.json")

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}
EM_LIST = ("https://push2.eastmoney.com/api/qt/clist/get?pn={pn}&pz=100&po=1&np=1"
           "&fltt=2&invt=2&fid=f6&fs=m:105,m:106,m:107&fields=f12,f13,f14,f2,f3,f6,f20")

UNIVERSE_N = 300
UNIVERSE_SEED = 42
UNIV_MCAP_MIN = 1e9        # 建池：市值 ≥ 10 亿美元
UNIV_TURNOVER_MIN = 2e7    # 建池：当日成交额 ≥ 2000 万美元（比交易资格 5000 万宽，留出历史上变大/变小的空间）
BENCH_SYMS = ["SPY", "QQQ", "IWM"]
TD_INTERVAL = 9.0          # Twelve Data 免费版 8 次/分钟，与生产共用 → 间隔 ≥9 秒
TD_BUDGET = 330            # 本任务调用预算上限
# 剔除 ETF / 基金 / 信托 / 杠杆产品（东财名称多为中文，中英文关键词都要）
EXCLUDE_KW = ["ETF", "ETN", "FUND", "TRUST", "ISHARES", "SPDR", "PROSHARES", "DIREXION",
              "INVESCO", "VANGUARD", "WISDOMTREE", "GLOBAL X", "VANECK", "GRANITESHARES",
              "基金", "信托", "指数", "杠杆", "做多", "做空", "反向", "两倍", "二倍", "三倍",
              "2X", "3X", "优先", "权证", "认股", "单位", "PREF", "WARRANT", " UNIT", "RIGHTS",
              " NOTES", "DEBENTURE"]

# ============================================================================
# 规范日线规则 v1 参数（与 config.SWING 对齐；变体用 dataclasses.replace 覆盖）
# ============================================================================
BT_START = "2012-01-01"
OOS_SPLIT = "2019-01-01"     # 样本外切分：2012-2018 / 2019-至今


@dataclass(frozen=True)
class Params:
    # ---- 资格 ----
    price_min: float = 5.0
    dollar_vol_min: float = 5e7      # 20 日均成交额
    adr_min: float = 0.035           # ADR20 = mean(high/low - 1)
    # ---- 观察名单 ----
    watch_topk: int = 20             # 资格池按日涨幅前 20
    watch_min_gain: float = 0.03     # 且日涨幅 ≥3%
    watch_days: int = 20             # 保留 20 个交易日
    # ---- 形态 ----
    pb_touch: float = 1.005          # 回调：low ≤ EMA9×1.005
    pb_max_ext50: float = 0.25       # 回调：close/EMA50-1 ≤ 25%
    bo_range_adr: float = 0.6        # 突破：振幅 ≤ 0.6×ADR20×close（或 inside day）
    bo_near9: float = 0.03           # 突破：|close/EMA9-1| ≤ 3%
    bo_base_adr: float = 3.0         # 突破：近 10 日 (最高-最低)/close ≤ 3×ADR20
    ep_gap: float = 0.10             # EP：跳空 ≥10%
    ep_vol_mult: float = 3.0         # EP：量 ≥ 3×近 50 日均量
    max_stop: float = 0.03           # 止损距离上限（另受 ADR20/2 约束）
    open_skip_mult: float = 1.5      # 高开后 (open-stop)/open > max_stop×1.5 则放弃
    # ---- 成本与仓位 ----
    cost: float = 0.001              # 单边成本/滑点 0.1%
    risk_pct: float = 0.005          # 每笔打到止损亏权益 0.5%
    max_pos_pct: float = 0.25        # 单仓 ≤25% 权益
    max_gross: float = 1.0           # 总敞口 ≤100%（现金账户）
    account: float = 10000.0
    # ---- 管理 ----
    be_days: int = 3                 # 持满 3 天且 EMA9>成交价 → 止损上移到成本
    trim_ext: float = 0.20           # close ≥ EMA9×1.20 → 收盘卖 15%（每笔一次）
    trim_frac: float = 0.15
    trail: str = "ema9"              # 跟踪均线 ema9 / ema21（收盘跌破则全出）
    trail_start: str = "entry"       # entry=成交当日起生效（v1 规范） / after_be=止损挪到保本后才生效
    # ---- 变体开关 ----
    mkt_filter: str = "F0"           # F0 无 / F1 QQQ 趋势 / F2 资格池宽度 ≥40% / F2L 流动池宽度 ≥40%
    breadth_min: float = 0.40
    breaker: bool = False            # 连亏熔断：3 连亏风险减半、5 连亏停 2 天
    same_bar: str = "conservative"   # conservative / path / optimistic，见 simulate() 撮合段注释
    setups: tuple = ("pullback", "breakout", "ep")
    placebo_seed: int = -1           # ≥0 时：每天用"同数量的随机趋势股窄幅日"替换真实形态（安慰剂对照）


BASE = Params()


# ============================================================================
# 一、建池 + 拉数据
# ============================================================================
def _http_json(url: str, timeout: int = 25) -> dict:
    req = urllib.request.Request(url, headers=UA)
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())


def _td_symbol(em_code: str) -> str | None:
    """东财代码 → Twelve Data 代码。BRK_B→BRK.B；_U/_W/_R 等单位/权证/配股权剔除。"""
    if "_" in em_code:
        base, suf = em_code.split("_", 1)
        if suf in ("A", "B", "C"):
            return f"{base}.{suf}"
        return None
    if not em_code.replace(".", "").isalnum():
        return None
    return em_code


def _em_universe_rows() -> list:
    """东财行情榜按成交额倒序翻页，直到成交额跌破建池下限。失败返回 None（交给兜底）。"""
    rows, pn = [], 1
    while True:
        d = None
        for attempt in range(6):
            try:
                d = _http_json(EM_LIST.format(pn=pn))
                break
            except Exception as e:  # 东财偶发 502/掐断：指数退避重试
                print(f"[bt_swing] 东财第{pn}页失败 {repr(e)[:80]}，重试")
                time.sleep(min(60, 5 * 2 ** attempt))
        if d is None:
            print(f"[bt_swing] 东财第{pn}页连续失败 → 放弃东财（不能用不完整的榜单建池）")
            return None
        diff = (d.get("data") or {}).get("diff") or []
        if not diff:
            break
        rows.extend(diff)
        last_turnover = diff[-1].get("f6")
        if not isinstance(last_turnover, (int, float)) or last_turnover < UNIV_TURNOVER_MIN:
            break
        pn += 1
        time.sleep(1.5)
    print(f"[bt_swing] 东财行情榜取到 {len(rows)} 行（{pn} 页）")
    out = []
    for r in rows:
        mcap, turnover, mkt = r.get("f20"), r.get("f6"), r.get("f13")
        if not isinstance(mcap, (int, float)) or not isinstance(turnover, (int, float)):
            continue  # ETF/基金在东财没有总市值字段（'-'）
        if mkt not in (105, 106, 107):
            continue
        sym = _td_symbol(str(r.get("f12")))
        if sym:
            out.append({"symbol": sym, "em_code": r.get("f12"), "market": mkt,
                        "name": str(r.get("f14") or ""), "price": r.get("f2"),
                        "turnover": float(turnover), "mcap": float(mcap), "source": "eastmoney"})
    return out


def _nasdaq_universe_rows() -> list:
    """兜底：Nasdaq 官网 screener（一次请求全美股，含市值/价/量；成交额=价×量）。"""
    url = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&download=true"
    hdr = dict(UA, **{"Accept": "application/json, text/plain, */*",
                      "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"})
    d = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=60).read().decode())
    out = []
    for r in d["data"]["rows"]:
        try:
            px = float(str(r.get("lastsale", "")).replace("$", "").replace(",", ""))
            vol = float(r.get("volume") or 0)
            mcap = float(r.get("marketCap") or 0)
        except ValueError:
            continue
        raw = str(r.get("symbol", "")).strip()
        if "^" in raw:          # 优先股
            continue
        sym = _td_symbol(raw.replace("/", "_"))
        if sym:
            out.append({"symbol": sym, "em_code": raw, "market": None, "name": str(r.get("name") or ""),
                        "price": px, "turnover": px * vol, "mcap": mcap, "source": "nasdaq",
                        "sector": r.get("sector")})
    print(f"[bt_swing] Nasdaq screener 取到 {len(out)} 只")
    return out


def build_universe() -> pd.DataFrame:
    """建池 → 过滤普通股 → 种子 42 随机打乱（前 300 为正选，其余备选）。
    首选东财行情榜（BRIEF 指定）；东财连续 502 时退到 Nasdaq screener（字段等价，记录在 source 列）。"""
    if os.path.exists(UNIVERSE_CSV):
        print(f"[bt_swing] 股票池已存在：{UNIVERSE_CSV}")
        return pd.read_csv(UNIVERSE_CSV)
    os.makedirs(SWING_CACHE, exist_ok=True)
    rows = _em_universe_rows()
    if rows is None:
        rows = _nasdaq_universe_rows()
    keep = []
    for r in rows:
        if r["mcap"] < UNIV_MCAP_MIN or r["turnover"] < UNIV_TURNOVER_MIN:
            continue
        up = r["name"].upper()
        if any(k in up for k in EXCLUDE_KW):
            continue
        if r["symbol"] in BENCH_SYMS:
            continue
        keep.append(r)
    elig = pd.DataFrame(keep).drop_duplicates("symbol").sort_values("symbol").reset_index(drop=True)
    rng = np.random.default_rng(UNIVERSE_SEED)
    elig = elig.iloc[rng.permutation(len(elig))].reset_index(drop=True)
    elig["draw_order"] = np.arange(len(elig))
    elig["status"] = "reserve"
    elig.loc[elig.index < UNIVERSE_N, "status"] = "selected"
    elig.to_csv(UNIVERSE_CSV, index=False)
    print(f"[bt_swing] 合格普通股 {len(elig)} 只（来源 {elig['source'].iloc[0]}），"
          f"抽前 {UNIVERSE_N} 只（其余作备选，拉不到时顺补）")
    return elig


def _cache_path(sym: str, tag: str = "") -> str:
    return os.path.join(SWING_CACHE, f"{sym.replace('/', '_')}{tag}.csv.gz")


_last_td_call = [0.0]
_td_calls = [0]


def _td_fetch(sym: str, adjust: str | None = None) -> pd.DataFrame:
    """Twelve Data 5000 根日线 OHLCV。429 等 65 秒重试；其他错误抛 RuntimeError。"""
    key = os.environ.get("TWELVEDATA_KEY", "")
    if not key:
        raise RuntimeError("TWELVEDATA_KEY 未设置")
    q = {"symbol": sym, "interval": "1day", "outputsize": 5000, "order": "ASC", "apikey": key}
    if adjust:
        q["adjust"] = adjust
    url = "https://api.twelvedata.com/time_series?" + urllib.parse.urlencode(q)
    for attempt in range(4):
        wait = TD_INTERVAL - (time.time() - _last_td_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_td_call[0] = time.time()
        _td_calls[0] += 1
        try:
            d = _http_json(url, timeout=40)
        except Exception as e:
            if attempt < 3:
                time.sleep(10)
                continue
            raise RuntimeError(f"网络错误 {repr(e)[:80]}")
        if d.get("status") == "ok" and d.get("values"):
            df = pd.DataFrame(d["values"])
            df = df.rename(columns={"datetime": "date"})
            for c in ("open", "high", "low", "close", "volume"):
                df[c] = pd.to_numeric(df.get(c), errors="coerce")
            return df[["date", "open", "high", "low", "close", "volume"]]
        msg = str(d.get("message", d))
        if str(d.get("code")) == "429" or "credits" in msg.lower() and "minute" in msg.lower():
            print(f"[bt_swing] {sym} 429，等 65s")
            time.sleep(65)
            continue
        raise RuntimeError(msg[:150])
    raise RuntimeError("429 重试耗尽")


def check_split_adjust(df: pd.DataFrame, sym: str = "NVDA", day: str = "2024-06-10",
                       ratio: float = 10.0) -> str:
    """核对拆股是否已复权：拆股前一日 / 拆股日收盘比 ≈ ratio → 未复权。"""
    s = df.set_index(pd.to_datetime(df["date"]))["close"]
    before = s[s.index < day].iloc[-1]
    after = s[s.index >= day].iloc[0]
    r = before / after
    verdict = "未复权" if r > ratio * 0.6 else "已复权"
    print(f"[bt_swing] {sym} 拆股核对：{day} 前收 {before:.2f} / 当日收 {after:.2f} = {r:.2f} → {verdict}")
    return verdict


def fix_unadjusted_splits(df: pd.DataFrame) -> tuple[pd.DataFrame, list]:
    """兜底：检测单日 close 比值接近 2/3/4/5/10/20 等整数拆股比例（且 open 同样跳）→ 向前复权。
    只在 NVDA 核对结果为"未复权"时由 load_panels() 启用（2026-10 实测 Twelve Data 已复权，未启用）。"""
    df = df.copy().reset_index(drop=True)
    fixes = []
    c = df["close"].values
    for i in range(1, len(df)):
        if not (c[i - 1] > 0 and c[i] > 0):
            continue
        r = c[i - 1] / c[i]
        for k in (2, 3, 4, 5, 8, 10, 15, 20, 25, 30, 40, 50):
            for ratio in (k, 1.0 / k):
                o = df.at[i, "open"]
                if abs(r / ratio - 1) < 0.04 and o > 0 and abs(c[i - 1] / o / ratio - 1) < 0.06:
                    df.loc[:i - 1, ["open", "high", "low", "close"]] /= ratio
                    df.loc[:i - 1, "volume"] *= ratio
                    fixes.append((df.at[i, "date"], round(ratio, 4)))
                    c = df["close"].values
                    break
            else:
                continue
            break
    return df, fixes


def pull(budget: int = TD_BUDGET):
    """建池 + 拉全部历史（断点续传：已缓存不重复拉）。完成后写 _PULL_DONE。"""
    os.makedirs(SWING_CACHE, exist_ok=True)
    status = {}
    sel_n = 0

    def _get(sym, adjust=None, tag=""):
        path = _cache_path(sym, tag)
        if os.path.exists(path):
            return pd.read_csv(path), True
        if _td_calls[0] >= budget:
            raise RuntimeError("预算用尽")
        df = _td_fetch(sym, adjust=adjust)
        df.to_csv(path, index=False, compression="gzip")
        return df, False

    # 1) NVDA 拆股核对（先做：决定后面要不要自己修复）
    try:
        nv, _ = _get("NVDA", tag="_chk")
        status["split_check"] = check_split_adjust(nv)
    except Exception as e:
        status["split_check"] = f"失败 {e}"
        print(f"[bt_swing] NVDA 核对失败：{e}")
    # 2) 基准（拆股复权价，供过滤器）+ 含分红全复权（供买入持有基准）
    for sym in BENCH_SYMS:
        for adj, tag in ((None, ""), ("all", "_tr")):
            try:
                _get(sym, adjust=adj, tag=tag)
            except Exception as e:
                print(f"[bt_swing] 基准 {sym}{tag} 失败：{e}")
    univ = build_universe()
    status["universe_source"] = str(univ["source"].iloc[0]) if "source" in univ else "eastmoney"
    status["universe_eligible"] = int(len(univ))
    # 3) 股票池：按 draw_order 顺序，正选拉不到就顺补备选，直到凑满 300 或预算用尽
    univ = univ.sort_values("draw_order").reset_index(drop=True)
    univ["status"] = "reserve"
    for i, r in univ.iterrows():
        if sel_n >= UNIVERSE_N:
            break
        sym = r["symbol"]
        try:
            df, cached = _get(sym)
            if len(df) < 60:
                raise RuntimeError(f"仅 {len(df)} 根")
            univ.at[i, "status"] = "selected"
            sel_n += 1
            if not cached:
                print(f"[bt_swing] {sel_n:3}/{UNIVERSE_N} {sym:6} {len(df)} 根 "
                      f"{df['date'].iloc[0]}~{df['date'].iloc[-1]}  (调用 {_td_calls[0]})")
        except Exception as e:
            univ.at[i, "status"] = f"fail:{str(e)[:40]}"
            print(f"[bt_swing] {sym} 失败：{e}")
            if "预算用尽" in str(e):
                break
    univ.to_csv(UNIVERSE_CSV, index=False)
    status.update({"selected": int(sel_n), "td_calls_this_run": int(_td_calls[0]),
                   "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    with open(PULL_DONE, "w") as f:
        json.dump(status, f, ensure_ascii=False)
    print(f"[bt_swing] 拉取完成：{status}  → 已写 {PULL_DONE}")


# ============================================================================
# 二、读缓存 → 面板 → 指标（全部 t 日收盘可得，无未来函数）
# ============================================================================
def _read_cached(sym: str, tag: str = "") -> pd.DataFrame | None:
    path = _cache_path(sym, tag)
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    return df


def _sanitize(df: pd.DataFrame) -> pd.DataFrame:
    """清洗坏 tick：非正价格剔除；high/low 包住 open/close；单日 ×3 又回落 <0.4 的孤立尖刺剔除。"""
    df = df.copy()
    for c in ("open", "high", "low", "close"):
        df.loc[~(df[c] > 0), c] = np.nan
    df = df.dropna(subset=["close"])
    df["open"] = df["open"].fillna(df["close"])
    df["high"] = df[["high", "open", "close"]].max(axis=1)
    df["low"] = df[["low", "open", "close"]].min(axis=1)
    df["volume"] = df["volume"].fillna(0)
    c = df["close"].values
    bad = np.zeros(len(df), dtype=bool)
    for i in range(1, len(df) - 1):
        up, down = c[i] / c[i - 1], c[i + 1] / c[i]
        if (up > 3 and down < 0.4) or (up < 0.33 and down > 2.5):
            bad[i] = True
    return df.loc[~bad].reset_index(drop=True)


@dataclass
class Panels:
    dates: pd.DatetimeIndex
    syms: list
    O: np.ndarray
    H: np.ndarray
    L: np.ndarray
    C: np.ndarray
    V: np.ndarray
    ind: dict = field(default_factory=dict)
    qqq: dict = field(default_factory=dict)
    bench: dict = field(default_factory=dict)


def load_panels(cal_start: str = "2007-01-01") -> Panels:
    """读池内全部缓存 → 以 SPY 交易日为主日历的 [T, N] 数组（缺失=NaN，不前向填充）。"""
    univ = pd.read_csv(UNIVERSE_CSV)
    syms = univ.loc[univ["status"] == "selected", "symbol"].tolist()
    spy = _read_cached("SPY")
    cal = pd.DatetimeIndex(spy.loc[spy["date"] >= cal_start, "date"])
    # 拉数时 NVDA 核对出"未复权"才启用自修复（误把真实的单日腰斩当拆股的风险比漏修大）
    need_fix = False
    if os.path.exists(PULL_DONE):
        with open(PULL_DONE) as f:
            need_fix = str(json.load(f).get("split_check", "")).startswith("未复权")
    frames, kept = {}, []
    for s in syms:
        df = _read_cached(s)
        if df is None or len(df) < 60:
            continue
        if need_fix:
            df, fixes = fix_unadjusted_splits(df)
            if fixes:
                print(f"[bt_swing] {s} 自修复拆股 {fixes}")
        df = _sanitize(df).set_index("date").reindex(cal)
        frames[s] = df
        kept.append(s)
    arr = {k: np.column_stack([frames[s][k].values.astype(float) for s in kept])
           for k in ("open", "high", "low", "close", "volume")}
    P = Panels(cal, kept, arr["open"], arr["high"], arr["low"], arr["close"], arr["volume"])
    # 基准：拆股复权收盘（过滤器）+ 全收益收盘（买入持有对照）
    for b in BENCH_SYMS:
        px = _read_cached(b).set_index("date").reindex(cal)
        tr = _read_cached(b, "_tr")
        P.bench[b] = {"close": px["close"].values.astype(float),
                      "tr": (tr.set_index("date").reindex(cal)["close"].values.astype(float)
                             if tr is not None else px["close"].values.astype(float))}
    q = pd.Series(P.bench["QQQ"]["close"], index=cal).ffill()
    e9, e21 = q.ewm(span=9, adjust=False).mean(), q.ewm(span=21, adjust=False).mean()
    P.qqq = {"ok": ((q > e21) & (e9 > e21) & (e21 > e21.shift(5))).values}
    print(f"[bt_swing] 面板：{len(kept)} 只 × {len(cal)} 天（{cal[0].date()} ~ {cal[-1].date()}）")
    return P


def compute_indicators(P: Panels) -> dict:
    """所有指标都只用 ≤t 的数据。EMA 用 adjust=False 递推、min_periods=span。"""
    C = pd.DataFrame(P.C)
    H = pd.DataFrame(P.H)
    L = pd.DataFrame(P.L)
    V = pd.DataFrame(P.V)
    ema = {n: C.ewm(span=n, adjust=False, min_periods=n).mean() for n in (9, 21, 50, 150)}
    ind = {
        "ema9": ema[9].values, "ema21": ema[21].values, "ema50": ema[50].values,
        "ema150": ema[150].values,
        "ema21_5": ema[21].shift(5).values,
        "adr20": (H / L - 1).rolling(20).mean().values,
        "dv20": (C * V).rolling(20).mean().values,
        "vol50p": V.rolling(50).mean().shift(1).values,       # 不含当日，避免当日巨量抬高均量
        "gain": (C / C.shift(1) - 1).values,
        "prev_close": C.shift(1).values,
        "prev_high": H.shift(1).values,
        "prev_low": L.shift(1).values,
        "base10": ((H.rolling(10).max() - L.rolling(10).min()) / C).values,
        "rs63": (C / C.shift(63) - 1).values,
    }
    P.ind = ind
    return ind


# ============================================================================
# 三、信号：资格 → 观察名单 → 形态（t 收盘判定，t+1 执行）
# ============================================================================
SETUP_CN = {"pullback": "回调", "breakout": "突破", "ep": "EP跳空"}


def build_signals(P: Panels, p: Params) -> dict:
    """返回每日候选 {i: [cand,...]} 和每日宽度 breadth[i]。候选已按 quality 降序。

    quality（排队优先级，0~1 越大越优先，三项线性加权）：
      0.5 × 相对强度：63 日涨幅在当日"流动性合格池"里的百分位（同板块挑最强的日线近似）
      0.3 × 止损紧度：1 - stop_pct / 允许上限（越紧同样风险买得越多、R 倍数越大）
      0.2 × 趋势斜率：clip((EMA9/EMA50 - 1) / 20%, 0, 1)
    """
    ind = P.ind
    C, H, L, O, V = P.C, P.H, P.L, P.O, P.V
    with np.errstate(invalid="ignore", divide="ignore"):
        liquid = (C >= p.price_min) & (ind["dv20"] >= p.dollar_vol_min)
        elig = liquid & (ind["adr20"] >= p.adr_min) & (C > ind["ema21"])
        stack = ((ind["ema9"] > ind["ema21"]) & (ind["ema21"] > ind["ema50"])
                 & (ind["ema21"] > ind["ema21_5"]))
        strong = (C > ind["ema50"]) & (ind["ema50"] > ind["ema150"])
        pull = (stack & (L <= ind["ema9"] * p.pb_touch) & (C >= ind["ema21"])
                & (C / ind["ema50"] - 1 <= p.pb_max_ext50))
        inside = (H <= ind["prev_high"]) & (L >= ind["prev_low"])
        narrow = (H - L) <= p.bo_range_adr * ind["adr20"] * C
        brk = (stack & (inside | narrow) & (np.abs(C / ind["ema9"] - 1) <= p.bo_near9)
               & (ind["base10"] <= p.bo_base_adr * ind["adr20"]))
        ep = (O >= ind["prev_close"] * (1 + p.ep_gap)) & (V >= p.ep_vol_mult * ind["vol50p"])
        stop_pct = (H - L) / H
        allowed = np.minimum(ind["adr20"] / 2, p.max_stop)
        ok_stop = (stop_pct <= allowed) & (H > L)
        ext = np.clip((ind["ema9"] / ind["ema50"] - 1) / 0.20, 0, 1)
    T, N = C.shape
    # 观察名单：逐日推进（加入日记 last_add；满 watch_days 或收盘跌破 EMA50 移出）
    last_add = np.full(N, -10 ** 9)
    member = np.zeros(N, dtype=bool)
    cands = {}
    rng = np.random.default_rng(p.placebo_seed if p.placebo_seed >= 0 else 0)
    breadth = np.full(T, np.nan)
    breadth_liq = np.full(T, np.nan)
    for i in range(T):
        e = elig[i]
        if e.sum() >= 5:
            breadth[i] = strong[i][e].mean()
        lq = liquid[i]
        if lq.sum() >= 10:
            breadth_liq[i] = strong[i][lq].mean()
        g = np.where(e & (ind["gain"][i] >= p.watch_min_gain), ind["gain"][i], -np.inf)
        if np.isfinite(g).any():
            top = np.argsort(-g)[:p.watch_topk]
            top = top[np.isfinite(g[top])]
            last_add[top] = i
            member[top] = True
        with np.errstate(invalid="ignore"):
            member &= ~(C[i] < ind["ema50"][i])
        member &= (i - last_add) < p.watch_days
        # 形态判定
        rs = ind["rs63"][i]
        rs_ok = lq & np.isfinite(rs)
        rs_rank = np.full(N, 0.5)
        if rs_ok.sum() >= 5:
            order = rs[rs_ok].argsort().argsort()
            rs_rank[rs_ok] = order / max(1, rs_ok.sum() - 1)
        day = []
        for j in np.nonzero((member & liquid[i]) | (elig[i] & ep[i]))[0]:
            setup = None
            if "ep" in p.setups and ep[i, j] and elig[i, j]:
                setup = "ep"
            elif member[j] and liquid[i, j]:
                if "breakout" in p.setups and brk[i, j]:
                    setup = "breakout"
                elif "pullback" in p.setups and pull[i, j]:
                    setup = "pullback"
            if setup is None or not ok_stop[i, j]:
                continue
            q = (0.5 * rs_rank[j] + 0.3 * (1 - stop_pct[i, j] / allowed[i, j])
                 + 0.2 * (ext[i, j] if np.isfinite(ext[i, j]) else 0))
            day.append({"j": int(j), "setup": setup, "trigger": float(H[i, j]),
                        "stop": float(L[i, j]), "stop_pct": float(stop_pct[i, j]),
                        "quality": float(q)})
        if day and p.placebo_seed >= 0:
            # 安慰剂：同一天、同样数量，从"流动+资格+趋势叠层+止损够窄"的股票里随机抽，触发/止损同样取当日高/低。
            # 若真实形态没有增量信息，两者的期望 R 应该差不多。
            pool = np.nonzero(liquid[i] & elig[i] & stack[i] & ok_stop[i])[0]
            if len(pool):
                pick = rng.choice(pool, size=min(len(day), len(pool)), replace=False)
                day = [{"j": int(j), "setup": "placebo", "trigger": float(H[i, j]), "stop": float(L[i, j]),
                        "stop_pct": float(stop_pct[i, j]), "quality": float(rng.random())} for j in pick]
        if day:
            day.sort(key=lambda c: -c["quality"])
            cands[i] = day
    return {"cands": cands, "breadth": breadth, "breadth_liq": breadth_liq,
            "elig_n": elig.sum(axis=1)}


# ============================================================================
# 四、组合层面逐日模拟
# ============================================================================
@dataclass
class Pos:
    j: int
    sym: str
    setup: str
    entry_i: int
    entry_px: float
    stop: float
    init_stop: float
    shares: float
    risk_dollars: float
    trimmed: bool = False
    pnl: float = 0.0          # 已实现盈亏（含两侧成本）
    be_moved: bool = False


def simulate(P: Panels, p: Params, sig: dict, start: str = BT_START) -> dict:
    """按日推进：先处理持仓（开盘跳空止损 / 盘中止损 / 收盘规则），再撮合昨晚的挂单，最后收盘出新挂单。"""
    C, H, L, O = P.C, P.H, P.L, P.O
    ind = P.ind
    trail = ind["ema9"] if p.trail == "ema9" else ind["ema21"]
    T, N = C.shape
    i0 = int(np.searchsorted(P.dates.values, np.datetime64(start)))
    cash = p.account
    positions: dict = {}
    last_close = np.full(N, np.nan)
    trades = []
    eq_curve, expo_curve = [], []
    pending = []
    loss_streak, pause_until = 0, -1
    risk_mult = 1.0
    cost = p.cost

    def _sell(pos: Pos, px: float, frac: float, i: int, reason: str):
        nonlocal cash
        sh = pos.shares * frac
        cash += sh * px * (1 - cost)
        pos.pnl += sh * (px * (1 - cost) - pos.entry_px * (1 + cost))
        pos.shares -= sh
        if pos.shares <= 1e-9:
            _close(pos, i, px, reason)

    def _close(pos: Pos, i: int, px: float, reason: str):
        nonlocal loss_streak, pause_until, risk_mult
        r = pos.pnl / pos.risk_dollars if pos.risk_dollars > 0 else 0.0
        trades.append({"sym": pos.sym, "setup": pos.setup, "entry_date": P.dates[pos.entry_i],
                       "exit_date": P.dates[i], "entry_px": pos.entry_px, "exit_px": px,
                       "init_stop": pos.init_stop, "hold_days": i - pos.entry_i,
                       "r": r, "pnl": pos.pnl, "reason": reason, "trimmed": pos.trimmed})
        del positions[pos.j]
        if p.breaker:
            if r < 0:
                loss_streak += 1
                if loss_streak >= 3:
                    risk_mult = 0.5
                if loss_streak % 5 == 0:
                    pause_until = i + 2      # 暂停接下来 2 个交易日开新仓
            else:
                loss_streak, risk_mult = 0, 1.0

    def _manage_close(pos: Pos, i: int):
        """收盘规则：跌破跟踪均线全出 → 远离 EMA9 减 15% → 满 3 天且 EMA9>成本 止损上移保本。"""
        j = pos.j
        c = C[i, j]
        tr_ok = (p.trail_start == "entry") or pos.be_moved
        if tr_ok and np.isfinite(trail[i, j]) and c < trail[i, j]:
            _sell(pos, c, 1.0, i, "trail")
            return
        if (not pos.trimmed) and np.isfinite(ind["ema9"][i, j]) and c >= ind["ema9"][i, j] * (1 + p.trim_ext):
            pos.trimmed = True
            _sell(pos, c, p.trim_frac, i, "trim")
            if pos.j not in positions:
                return
        if (i - pos.entry_i) >= p.be_days and ind["ema9"][i, j] > pos.entry_px and pos.stop < pos.entry_px:
            pos.stop = pos.entry_px
            pos.be_moved = True

    for i in range(i0, T):
        # 0) 当日开盘前的权益（用于仓位计算）= 昨收市值
        mv_prev = sum(pos.shares * last_close[pos.j] for pos in positions.values())
        equity_prev = cash + mv_prev
        # 1) 持仓：跳空低开 / 盘中止损 / 收盘规则
        for pos in list(positions.values()):
            j = pos.j
            if not np.isfinite(C[i, j]):
                continue                    # 停牌/缺数据：不动
            if O[i, j] <= pos.stop:
                _sell(pos, O[i, j], 1.0, i, "gap_stop")
            elif L[i, j] <= pos.stop:
                _sell(pos, pos.stop, 1.0, i, "stop")
            else:
                _manage_close(pos, i)
        # 2) 撮合昨晚挂单（quality 降序；现金账户：用可用现金约束总敞口）
        for c in pending:
            j = c["j"]
            if j in positions or not np.isfinite(O[i, j]):
                continue
            o, h, lo, cl = O[i, j], H[i, j], L[i, j], C[i, j]
            if o > c["trigger"]:
                if (o - c["stop"]) / o > p.max_stop * p.open_skip_mult:
                    continue                # 高开太多，止损变得太宽 → 放弃
                fill, at_open = o, True
            elif h >= c["trigger"]:
                fill, at_open = c["trigger"], False
            else:
                continue
            per_share_risk = fill - c["stop"]
            if per_share_risk <= 0:
                continue
            risk_d = p.risk_pct * risk_mult * equity_prev
            shares = risk_d / per_share_risk
            cap_val = min(p.max_pos_pct * equity_prev,
                          p.max_gross * equity_prev - sum(pp.shares * last_close[pp.j] for pp in positions.values()),
                          cash / (1 + cost))
            if cap_val < 0.01 * equity_prev:
                continue                    # 没有敞口空间了
            shares = min(shares, cap_val / fill)
            pos = Pos(j, P.syms[j], c["setup"], i, fill, c["stop"], c["stop"], shares,
                      shares * per_share_risk)
            cash -= shares * fill * (1 + cost)
            positions[j] = pos
            # 同一根 K 线歧义（只有"盘中触发成交"的单子才有歧义；开盘成交的单子最低价必然在成交之后）：
            #   conservative = 成交后最低价 ≤ 止损即判出局（v1 规范）
            #   path         = OHLC 路径假设：阳线按 O→L→H→C（低点在触发之前，不算止损），
            #                  阴线按 O→H→L→C（触发后才探低，最低 ≤ 止损即出）—— 折中估计
            #   optimistic   = 一律假设低点在成交之前，只有收盘仍 ≤ 止损才出
            if at_open or p.same_bar == "conservative":
                hit = lo <= pos.stop
            elif p.same_bar == "path":
                hit = (lo <= pos.stop) if cl < o else (cl <= pos.stop)
            else:
                hit = cl <= pos.stop
            if hit:
                _sell(pos, pos.stop, 1.0, i, "stop_d0")
                continue
            _manage_close(pos, i)
        # 3) 收盘：盯市
        last_close = np.where(np.isfinite(C[i]), C[i], last_close)
        mv = sum(pos.shares * last_close[pos.j] for pos in positions.values())
        eq = cash + mv
        eq_curve.append(eq)
        expo_curve.append(mv / eq if eq > 0 else 0)
        # 4) 收盘出新挂单（市场过滤 / 熔断暂停）
        pending = []
        allow = i + 1 > pause_until
        if p.mkt_filter == "F1":
            allow = allow and bool(P.qqq["ok"][i])
        elif p.mkt_filter == "F2":
            b = sig["breadth"][i]
            allow = allow and np.isfinite(b) and b >= p.breadth_min
        elif p.mkt_filter == "F2L":
            b = sig["breadth_liq"][i]
            allow = allow and np.isfinite(b) and b >= p.breadth_min
        if allow and i in sig["cands"]:
            pending = [c for c in sig["cands"][i] if c["j"] not in positions]
    # 期末：未平仓按最后收盘计入权益（不计入已平仓统计）
    eq = pd.Series(eq_curve, index=P.dates[i0:])
    expo = pd.Series(expo_curve, index=P.dates[i0:])
    open_pos = [{"sym": pp.sym, "setup": pp.setup, "entry_date": str(P.dates[pp.entry_i].date()),
                 "entry_px": pp.entry_px, "stop": pp.stop} for pp in positions.values()]
    return {"equity": eq, "exposure": expo, "trades": pd.DataFrame(trades), "open": open_pos}


# ============================================================================
# 五、对照组与指标
# ============================================================================
def ew_buy_hold(P: Panels, start: str = BT_START, cost: float = 0.001) -> pd.Series:
    """同一股票池等权买入持有，月末再平衡（只纳入再平衡日已有收盘价的股票，扣再平衡换手成本）。
    这是剔除"股票池本身是今天的赢家"这一幸存者偏差后最公平的比较对象：两者吃同一份偏差。"""
    C = pd.DataFrame(P.C, index=P.dates)
    Cf = C.ffill()
    dates = P.dates[P.dates >= start]
    me = pd.Series(dates, index=dates).groupby(dates.to_period("M")).max().tolist()
    nav = pd.Series(np.nan, index=dates)
    v = 1.0
    w_drift = None
    reb = [dates[0]] + [d for d in me if d > dates[0]]
    for k, d0 in enumerate(reb):
        d1 = reb[k + 1] if k + 1 < len(reb) else dates[-1]
        base = C.loc[d0]
        live = base[base > 0].index
        w_new = pd.Series(1.0 / len(live), index=live)
        if w_drift is not None:
            turn = w_new.subtract(w_drift, fill_value=0).abs().sum()
            v *= (1 - cost * turn)
        seg = Cf.loc[d0:d1, live]
        g = seg.div(base[live]).mean(axis=1)
        nav.loc[seg.index] = v * g.values
        v = float(v * g.iloc[-1])
        last = seg.iloc[-1] / base[live]
        w_drift = last / last.sum()
        if d1 == dates[-1]:
            break
    return nav.ffill()


def perf(eq: pd.Series) -> dict:
    eq = eq.dropna()
    r = eq.pct_change().dropna()
    yrs = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1
    dd = (eq / eq.cummax() - 1).min()
    sharpe = r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else float("nan")
    return {"cagr": float(cagr), "maxdd": float(dd), "sharpe": float(sharpe),
            "calmar": float(cagr / abs(dd)) if dd < 0 else float("nan"),
            "vol": float(r.std() * np.sqrt(252)),
            "total": float(eq.iloc[-1] / eq.iloc[0] - 1)}


def seg(eq: pd.Series, a: str, b: str | None = None) -> pd.Series:
    """切片并把前一日权益作为起点（区间收益 = 区间末 / 区间前一日 - 1）。"""
    eq = eq.dropna()
    before = eq[eq.index < a]
    part = eq[(eq.index >= a) & ((eq.index <= b) if b else True)]
    if len(before):
        part = pd.concat([before.iloc[-1:], part])
    return part


def trade_stats(tr: pd.DataFrame, years: float) -> dict:
    if tr is None or tr.empty:
        return {"n": 0}
    w, l = tr[tr["r"] > 0], tr[tr["r"] <= 0]
    return {"n": int(len(tr)), "win_rate": float(len(w) / len(tr)),
            "avg_win_r": float(w["r"].mean()) if len(w) else 0.0,
            "avg_loss_r": float(l["r"].mean()) if len(l) else 0.0,
            "expectancy_r": float(tr["r"].mean()), "total_r": float(tr["r"].sum()),
            # 期望 R 的 95% 置信区间（正态近似）：胜率 22% 的分布极度右偏，样本少时区间会很宽
            "exp_r_ci95": [float(tr["r"].mean() - 1.96 * tr["r"].std() / np.sqrt(len(tr))),
                           float(tr["r"].mean() + 1.96 * tr["r"].std() / np.sqrt(len(tr)))]
            if len(tr) > 1 else None,
            "median_r": float(tr["r"].median()),
            "trades_per_year": float(len(tr) / years) if years > 0 else float("nan"),
            "avg_hold_days": float(tr["hold_days"].mean()),
            "max_r": float(tr["r"].max()),
            "pct_stop_d0": float((tr["reason"] == "stop_d0").mean()),
            "pct_gap_stop": float((tr["reason"] == "gap_stop").mean())}


def yearly(eq: pd.Series) -> dict:
    eq = eq.dropna()
    ye = eq.groupby(eq.index.year).last()
    prev = pd.concat([pd.Series([eq.iloc[0]], index=[ye.index[0] - 1]), ye]).shift(1).dropna()
    return {int(y): float(ye[y] / prev[y] - 1) for y in ye.index}


PERIODS = [("2015-16", "2015-01-01", "2016-12-31"), ("2018Q4", "2018-10-01", "2018-12-31"),
           ("2020", "2020-01-01", "2020-12-31"), ("2022", "2022-01-01", "2022-12-31"),
           ("2025", "2025-01-01", "2025-12-31"), ("2026YTD", "2026-01-01", None)]
SEGMENTS = [("IS 2012-2018", BT_START, "2018-12-31"), ("OOS 2019-至今", OOS_SPLIT, None)]


def summarize(res: dict) -> dict:
    eq, tr = res["equity"], res["trades"]
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    out = {"perf": perf(eq), "trades": trade_stats(tr, yrs),
           "avg_exposure": float(res["exposure"].mean()),
           "yearly": yearly(eq),
           "periods": {k: perf(seg(eq, a, b))["total"] for k, a, b in PERIODS},
           "segments": {}}
    for name, a, b in SEGMENTS:
        e = seg(eq, a, b)
        t = tr[(tr["entry_date"] >= a) & ((tr["entry_date"] <= b) if b else True)] if len(tr) else tr
        y = (e.index[-1] - e.index[0]).days / 365.25
        out["segments"][name] = {"perf": perf(e), "trades": trade_stats(t, y)}
    if len(tr):
        out["by_setup"] = {s: trade_stats(g, yrs) for s, g in tr.groupby("setup")}
        out["by_year_trades"] = {int(y): trade_stats(g, 1.0) for y, g in tr.groupby(tr["entry_date"].dt.year)}
    return out


def bench_summary(nav: pd.Series) -> dict:
    nav = nav.dropna()
    out = {"perf": perf(nav), "yearly": yearly(nav),
           "periods": {k: perf(seg(nav, a, b))["total"] for k, a, b in PERIODS},
           "segments": {n: {"perf": perf(seg(nav, a, b))} for n, a, b in SEGMENTS}}
    return out


# ============================================================================
# 六之二、诊断：入场后的漂移 / 子池自助抽样
# ============================================================================
def entry_drift(P: Panels, sig: dict, p: Params, start: str = BT_START) -> dict:
    """不管止损和卖出规则，只看"按 v1 撮合规则能成交的信号"成交后 k 日的收盘涨跌，
    并与同期资格池全部股票日的无条件前向收益比较。若入场本身没有方向性优势，
    再好的仓位/止损管理也只能把 ≈0 的漂移变成负期望（成本 + 跳空止损）。"""
    C, H, L, O = P.C, P.H, P.L, P.O
    ind = P.ind
    T, N = C.shape
    i0 = int(np.searchsorted(P.dates.values, np.datetime64(start)))
    ks = (0, 1, 5, 10, 20)
    rows = []
    for i, cl in sig["cands"].items():
        if i < i0 or i + 1 >= T:
            continue
        for c in cl:
            j = c["j"]
            o = O[i + 1, j]
            if not np.isfinite(o):
                continue
            if o > c["trigger"]:
                if (o - c["stop"]) / o > p.max_stop * p.open_skip_mult:
                    continue
                fill = o
            elif H[i + 1, j] >= c["trigger"]:
                fill = c["trigger"]
            else:
                continue
            rec = {"setup": c["setup"], "stop_hit_d0": bool(L[i + 1, j] <= c["stop"])}
            for k in ks:
                if i + 1 + k < T and np.isfinite(C[i + 1 + k, j]):
                    rec[f"ret{k}"] = C[i + 1 + k, j] / fill - 1
            rows.append(rec)
    d = pd.DataFrame(rows)
    with np.errstate(invalid="ignore"):
        elig = ((C >= p.price_min) & (ind["dv20"] >= p.dollar_vol_min) & (ind["adr20"] >= p.adr_min)
                & (C > ind["ema21"]))
    elig[:i0] = False
    uncond = {}
    for k in ks[1:]:
        fwd = np.full_like(C, np.nan)
        fwd[:-k] = C[k:] / C[:-k] - 1
        uncond[k] = float(np.nanmean(fwd[elig]))
    return {"n_filled": int(len(d)),
            "stop_hit_d0": float(d["stop_hit_d0"].mean()) if len(d) else None,
            "signal_fwd": {k: float(d[f"ret{k}"].mean()) for k in ks if f"ret{k}" in d},
            "signal_fwd_t": {k: float(d[f"ret{k}"].mean() / d[f"ret{k}"].std() * np.sqrt(d[f"ret{k}"].count()))
                             for k in ks if f"ret{k}" in d},
            "elig_uncond_fwd": uncond}


def subpool_bootstrap(P: Panels, p: Params, n_iter: int = 30, frac: float = 0.5, seed: int = 7) -> dict:
    """单一随机股票池的偏差检查：从 300 只里反复随机抽一半，各自重跑信号 + 组合模拟 + 同池等权，
    看结论（期望 R、相对同池等权）是否随抽样翻转。"""
    rng = np.random.default_rng(seed)
    N = len(P.syms)
    out = []
    for _ in range(n_iter):
        idx = np.sort(rng.choice(N, size=int(N * frac), replace=False))
        Q = Panels(P.dates, [P.syms[j] for j in idx], P.O[:, idx], P.H[:, idx], P.L[:, idx],
                   P.C[:, idx], P.V[:, idx], {k: v[:, idx] for k, v in P.ind.items()}, P.qqq, P.bench)
        sig = build_signals(Q, p)
        res = simulate(Q, p, sig)
        tr = res["trades"]
        ew = perf(ew_buy_hold(Q))
        pf = perf(res["equity"])
        out.append({"exp_r": float(tr["r"].mean()) if len(tr) else np.nan, "n": int(len(tr)),
                    "cagr": pf["cagr"], "sharpe": pf["sharpe"], "ew_cagr": ew["cagr"], "ew_sharpe": ew["sharpe"]})
    df = pd.DataFrame(out)
    return {"n_iter": n_iter, "frac": frac,
            "exp_r_mean": float(df["exp_r"].mean()), "exp_r_min": float(df["exp_r"].min()),
            "exp_r_max": float(df["exp_r"].max()), "exp_r_pos_share": float((df["exp_r"] > 0).mean()),
            "sharpe_mean": float(df["sharpe"].mean()), "sharpe_max": float(df["sharpe"].max()),
            "beat_ew_sharpe_share": float((df["sharpe"] > df["ew_sharpe"]).mean()),
            "ew_sharpe_mean": float(df["ew_sharpe"].mean())}


# ============================================================================
# 六、跑全部变体
# ============================================================================
SIGNAL_KEYS = ("price_min", "dollar_vol_min", "adr_min", "watch_topk", "watch_min_gain", "watch_days",
               "pb_touch", "pb_max_ext50", "bo_range_adr", "bo_near9", "bo_base_adr", "ep_gap",
               "ep_vol_mult", "max_stop", "setups", "placebo_seed")

VARIANTS = [
    ("base", "基准 v1（F0/止损≤3%/风险0.5%/EMA9跟踪/保守同K线）", {}),
    ("F1", "F1：QQQ 站上向上的 9/21EMA 才开新仓", {"mkt_filter": "F1"}),
    ("F2", "F2：资格池强档占比≥40% 才开新仓", {"mkt_filter": "F2"}),
    ("F2L", "F2'：流动池强档占比≥40%（宽度口径对照）", {"mkt_filter": "F2L"}),
    ("stop5", "止损上限 5%", {"max_stop": 0.05}),
    ("risk1", "每笔风险 1%", {"risk_pct": 0.01}),
    ("ema21", "跟踪均线 EMA21", {"trail": "ema21"}),
    ("trail_be", "EMA9 跟踪仅在止损挪到保本后启用", {"trail_start": "after_be"}),
    ("breaker", "连亏熔断（3连亏减半/5连亏停2天）", {"breaker": True}),
    ("F1_breaker", "F1 + 连亏熔断", {"mkt_filter": "F1", "breaker": True}),
    ("path", "折中同K线（阳线先探低后触发、阴线先触发后探低）", {"same_bar": "path"}),
    ("optimistic", "乐观同K线（盘中触发单当日只看收盘）", {"same_bar": "optimistic"}),
    ("cost0", "零成本（只看毛期望，诊断用）", {"cost": 0.0}),
    ("cost2", "单边成本 0.2%", {"cost": 0.002}),
    ("trail_be21", "EMA21 跟踪且仅在保本后启用（探索性）", {"trail": "ema21", "trail_start": "after_be"}),
    ("watch_top3", "观察名单只收前 3 名（按池子规模折算全市场「前 20」的选择性）", {"watch_topk": 3}),
    ("only_pullback", "只做回调", {"setups": ("pullback",)}),
    ("only_breakout", "只做突破", {"setups": ("breakout",)}),
    ("only_ep", "只做 EP", {"setups": ("ep",)}),
] + [(f"placebo{k}", f"安慰剂 #{k}：同日同数量随机趋势股窄幅日", {"placebo_seed": k}) for k in range(5)]


def run_all(quick: bool = False, variants=None) -> dict:
    P = load_panels()
    compute_indicators(P)
    sig_cache = {}
    results = {}
    todo = [v for v in (variants or VARIANTS) if (not quick or v[0] == "base")]
    trades_base = None
    for key, label, over in todo:
        p = replace(BASE, **over)
        skey = tuple(getattr(p, k) for k in SIGNAL_KEYS)
        if skey not in sig_cache:
            t0 = time.time()
            sig_cache[skey] = build_signals(P, p)
            print(f"[bt_swing] 信号 {key}: {sum(len(v) for v in sig_cache[skey]['cands'].values())} 个候选 "
                  f"({time.time() - t0:.0f}s)")
        res = simulate(P, p, sig_cache[skey])
        s = summarize(res)
        s["label"] = label
        results[key] = s
        if key == "base":
            trades_base = res["trades"]
            s["open"] = res["open"]
            sig = sig_cache[skey]
            dates = P.dates
            mask = dates >= BT_START
            s["diag"] = {"elig_n_mean": float(np.mean(sig["elig_n"][mask])),
                         "breadth_mean": float(np.nanmean(sig["breadth"][mask])),
                         "breadth_ge40": float(np.mean(sig["breadth"][mask][np.isfinite(sig["breadth"][mask])] >= 0.4)),
                         "breadth_valid_days": float(np.mean(np.isfinite(sig["breadth"][mask]))),
                         "breadth_liq_mean": float(np.nanmean(sig["breadth_liq"][mask])),
                         "breadth_liq_ge40": float(np.mean(sig["breadth_liq"][mask][np.isfinite(sig["breadth_liq"][mask])] >= 0.4)),
                         "qqq_ok_pct": float(np.mean(P.qqq["ok"][mask])),
                         "cand_days": int(sum(1 for i in sig["cands"] if dates[i] >= pd.Timestamp(BT_START)))}
        pf, ts = s["perf"], s["trades"]
        print(f"  {key:14} 年化{pf['cagr']:+7.1%} 回撤{pf['maxdd']:+6.1%} Sharpe{pf['sharpe']:5.2f} "
              f"笔数{ts.get('n', 0):5} 胜率{ts.get('win_rate', 0):5.1%} 期望{ts.get('expectancy_r', 0):+.2f}R "
              f"赢{ts.get('avg_win_r', 0):+.2f}R 亏{ts.get('avg_loss_r', 0):+.2f}R 敞口{s['avg_exposure']:.0%}")
    # 对照组
    dates = P.dates[P.dates >= BT_START]
    bench = {}
    for b in ("SPY", "QQQ"):
        for kind in ("close", "tr"):
            nav = pd.Series(P.bench[b][kind], index=P.dates).loc[dates]
            bench[f"{b}_{'price' if kind == 'close' else 'tr'}"] = bench_summary(nav)
    bench["EW_pool"] = bench_summary(ew_buy_hold(P))
    for k, v in bench.items():
        pf = v["perf"]
        print(f"  {k:14} 年化{pf['cagr']:+7.1%} 回撤{pf['maxdd']:+6.1%} Sharpe{pf['sharpe']:5.2f}")
    out = {"variants": results, "bench": bench, "universe_n": len(P.syms),
           "period": f"{dates[0].date()} ~ {dates[-1].date()}"}
    if not quick:
        diag = {}
        for key in ("base", "placebo0"):
            p = replace(BASE, **dict((v[0], v[2]) for v in VARIANTS)[key])
            skey = tuple(getattr(p, k) for k in SIGNAL_KEYS)
            diag[f"drift_{key}"] = entry_drift(P, sig_cache[skey], p)
            d = diag[f"drift_{key}"]
            print(f"[bt_swing] 入场漂移 {key}: 成交 {d['n_filled']}，当日触及止损 {d['stop_hit_d0']:.0%}，"
                  f"信号 5/10/20 日 {d['signal_fwd'][5]:+.2%}/{d['signal_fwd'][10]:+.2%}/{d['signal_fwd'][20]:+.2%}"
                  f" vs 资格池无条件 {d['elig_uncond_fwd'][5]:+.2%}/{d['elig_uncond_fwd'][10]:+.2%}/"
                  f"{d['elig_uncond_fwd'][20]:+.2%}")
        for key in ("base", "F2L"):
            p = replace(BASE, **dict((v[0], v[2]) for v in VARIANTS)[key])
            diag[f"boot_{key}"] = subpool_bootstrap(P, p)
            print(f"[bt_swing] 子池自助 {key}: {diag[f'boot_{key}']}")
        out["diag"] = diag
    if trades_base is not None and not quick:
        trades_base.to_csv(os.path.join(SWING_CACHE, "_trades_base.csv"), index=False)
    if not quick:
        with open(RESULT_JSON, "w") as f:
            json.dump(out, f, ensure_ascii=False, default=str, indent=1)
        print(f"[bt_swing] 结果已写 {RESULT_JSON}")
    return out




# ============================================================================
# 七、把 _results.json 渲染成 Markdown 表（docs/SWING_RESEARCH.md 的表格由此生成）
# ============================================================================
def _pct(x, d=1):
    return "—" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{x * 100:+.{d}f}%"


def _f(x, d=2):
    return "—" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{x:+.{d}f}"


def report(path: str = RESULT_JSON) -> str:
    with open(path) as f:
        R = json.load(f)
    V, B = R["variants"], R["bench"]
    lines = []
    add = lines.append
    add(f"区间 {R['period']}，股票池 {R['universe_n']} 只\n")
    add("### 组合层面（扣成本）\n")
    add("| 方案 | 年化 | 最大回撤 | Sharpe | Calmar | 笔数 | 年均笔数 | 胜率 | 平均赢R | 平均亏R | 期望R/笔 (95%CI) | 平均持有天 | 平均敞口 |")
    add("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for k, v in V.items():
        pf, ts = v["perf"], v["trades"]
        ci = ts.get("exp_r_ci95")
        cis = f"{_f(ts.get('expectancy_r'))} ({_f(ci[0])}~{_f(ci[1])})" if ci else _f(ts.get("expectancy_r"))
        add(f"| {k} {v['label']} | {_pct(pf['cagr'])} | {_pct(pf['maxdd'])} | {pf['sharpe']:.2f} | "
            f"{pf['calmar']:.2f} | {ts.get('n', 0)} | {ts.get('trades_per_year', 0):.0f} | "
            f"{ts.get('win_rate', 0) * 100:.1f}% | {_f(ts.get('avg_win_r'))} | {_f(ts.get('avg_loss_r'))} | "
            f"{cis} | {ts.get('avg_hold_days', 0):.1f} | {v['avg_exposure'] * 100:.0f}% |")
    add("\n### 对照组（买入持有）\n")
    add("| 对照 | 年化 | 最大回撤 | Sharpe | Calmar |")
    add("|---|---|---|---|---|")
    for k, v in B.items():
        pf = v["perf"]
        add(f"| {k} | {_pct(pf['cagr'])} | {_pct(pf['maxdd'])} | {pf['sharpe']:.2f} | {pf['calmar']:.2f} |")
    add("\n### 样本内 / 样本外\n")
    add("| 方案 | 段 | 年化 | 最大回撤 | Sharpe | 笔数 | 胜率 | 期望R |")
    add("|---|---|---|---|---|---|---|---|")
    keys = [k for k in ("base", "F1", "F2", "F2L", "breaker", "F1_breaker", "trail_be", "stop5", "path",
                        "optimistic", "placebo0")
            if k in V]
    for k in keys:
        for sn, sv in V[k]["segments"].items():
            pf, ts = sv["perf"], sv["trades"]
            add(f"| {k} | {sn} | {_pct(pf['cagr'])} | {_pct(pf['maxdd'])} | {pf['sharpe']:.2f} | "
                f"{ts.get('n', 0)} | {ts.get('win_rate', 0) * 100:.1f}% | {_f(ts.get('expectancy_r'))} |")
    for k in ("EW_pool", "QQQ_price", "SPY_price"):
        if k in B:
            for sn, sv in B[k]["segments"].items():
                pf = sv["perf"]
                add(f"| {k} | {sn} | {_pct(pf['cagr'])} | {_pct(pf['maxdd'])} | {pf['sharpe']:.2f} | | | |")
    add("\n### 分年度收益\n")
    cols = [k for k in ("base", "F1", "F2L", "F1_breaker") if k in V] + ["EW_pool", "QQQ_price", "SPY_price"]
    add("| 年 | " + " | ".join(cols) + " | base 笔数 | base 期望R |")
    add("|---" * (len(cols) + 3) + "|")
    years = sorted(V["base"]["yearly"].keys(), key=int)
    for y in years:
        row = []
        for c in cols:
            src = V.get(c) or B.get(c)
            row.append(_pct(src["yearly"].get(y)))
        bt = V["base"].get("by_year_trades", {}).get(y, {})
        add(f"| {y} | " + " | ".join(row) + f" | {bt.get('n', 0)} | {_f(bt.get('expectancy_r'))} |")
    add("\n### 关键时段区间收益\n")
    add("| 时段 | " + " | ".join(cols) + " |")
    add("|---" * (len(cols) + 1) + "|")
    for name, _, _ in PERIODS:
        row = []
        for c in cols:
            src = V.get(c) or B.get(c)
            row.append(_pct(src["periods"].get(name)))
        add(f"| {name} | " + " | ".join(row) + " |")
    add("\n### 分形态（基准 v1 组合内）\n")
    add("| 形态 | 笔数 | 胜率 | 平均赢R | 平均亏R | 期望R (95%CI) | 平均持有天 | 当日即止损占比 |")
    add("|---|---|---|---|---|---|---|---|")
    for s, ts in V["base"].get("by_setup", {}).items():
        ci = ts.get("exp_r_ci95")
        cis = f"{_f(ts.get('expectancy_r'))} ({_f(ci[0])}~{_f(ci[1])})" if ci else _f(ts.get("expectancy_r"))
        add(f"| {SETUP_CN.get(s, s)} | {ts['n']} | {ts['win_rate'] * 100:.1f}% | {_f(ts['avg_win_r'])} | "
            f"{_f(ts['avg_loss_r'])} | {cis} | {ts['avg_hold_days']:.1f} | {ts['pct_stop_d0'] * 100:.0f}% |")
    D = R.get("diag", {})
    if D:
        add("\n### 诊断 A：入场后的漂移（不含止损/卖出规则，成交价 → 第 k 日收盘）\n")
        add("| 信号 | 成交笔数 | 当日触及止损 | 当日收盘 | 1日 | 5日 | 10日 | 20日 | 资格池无条件 5/10/20日 |")
        add("|---|---|---|---|---|---|---|---|---|")
        for key in ("base", "placebo0"):
            d = D.get(f"drift_{key}")
            if not d:
                continue
            f, u = d["signal_fwd"], d["elig_uncond_fwd"]
            add(f"| {key} | {d['n_filled']} | {d['stop_hit_d0'] * 100:.0f}% | {_pct(f.get('0'), 2)} | "
                f"{_pct(f.get('1'), 2)} | {_pct(f.get('5'), 2)} | {_pct(f.get('10'), 2)} | {_pct(f.get('20'), 2)} | "
                f"{_pct(u.get('5'), 2)} / {_pct(u.get('10'), 2)} / {_pct(u.get('20'), 2)} |")
        add("\n### 诊断 B：子池自助抽样（300 只里随机抽 150 只 × 30 次）\n")
        add("| 方案 | 期望R 均值 | 期望R 最小~最大 | 期望R>0 的比例 | Sharpe 均值 | Sharpe 最大 | 跑赢同子池等权 Sharpe 的比例 | 同子池等权 Sharpe 均值 |")
        add("|---|---|---|---|---|---|---|---|")
        for key in ("base", "F2L"):
            d = D.get(f"boot_{key}")
            if not d:
                continue
            add(f"| {key} | {_f(d['exp_r_mean'])} | {_f(d['exp_r_min'])} ~ {_f(d['exp_r_max'])} | "
                f"{d['exp_r_pos_share'] * 100:.0f}% | {d['sharpe_mean']:.2f} | {d['sharpe_max']:.2f} | "
                f"{d['beat_ew_sharpe_share'] * 100:.0f}% | {d['ew_sharpe_mean']:.2f} |")
    return "\n".join(lines)


# ============================================================================
# CLI
# ============================================================================
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "run"
    if cmd == "universe":
        build_universe()
    elif cmd == "pull":
        pull()
    elif cmd == "run":
        run_all(quick="--quick" in argv)
    elif cmd == "report":
        print(report())
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
