"""
数据层：下载 → 后复权 → 存入服务器 MySQL(quant 库) → 回测从库里读。

核心要点（Tushare 新手必知）：
1. daily 接口返回的是【不复权价】，除权除息日会假跳空，必须用 adj_factor 复权。
2. 后复权价 = close * adj_factor / 最新adj_factor，最新价=真实价，历史连续。
3. Tushare 有每分钟调用限制，所以一次性拉到 MySQL，回测从库里读，不撞限频。

数据落在 quant 库三张表：daily_price / index_member / benchmark。
"""
import time
import pandas as pd
import tushare as ts
from tqdm import tqdm

import config
import db

_pro = None


def get_pro():
    global _pro
    if _pro is None:
        ts.set_token(config.TUSHARE_TOKEN)
        _pro = ts.pro_api()
    return _pro


def get_trade_dates(start: str, end: str) -> list:
    """区间内所有交易日（升序）。"""
    pro = get_pro()
    cal = pro.trade_cal(exchange="SSE", start_date=start, end_date=end, is_open="1")
    return sorted(cal["cal_date"].tolist())


def get_month_end_dates(start: str, end: str) -> list:
    """每月最后一个交易日（调仓日）。"""
    dates = get_trade_dates(start, end)
    s = pd.Series(dates, index=pd.to_datetime(dates))
    return s.groupby(s.index.to_period("M")).last().tolist()


def sync_index_members(index_code: str, month_ends: list) -> dict:
    """
    下载每个调仓月底的指数成分股，存入 index_member 表，返回 {trade_date: [con_code]}。
    动态股票池 = 避开幸存者偏差的关键。已有数据则直接读库。
    """
    # 先尝试读库
    rows = db.read_sql(
        "SELECT trade_date, con_code FROM index_member WHERE index_code=:ic",
        {"ic": index_code},
    )
    if not rows.empty:
        print("[data] index_member 命中数据库")
    else:
        pro = get_pro()
        start, end = month_ends[0], month_ends[-1]
        frames = []
        cur, end_dt = pd.to_datetime(start), pd.to_datetime(end)
        while cur <= end_dt:
            seg_end = min(cur + pd.Timedelta(days=120), end_dt)
            df = pro.index_weight(index_code=index_code,
                                  start_date=cur.strftime("%Y%m%d"),
                                  end_date=seg_end.strftime("%Y%m%d"))
            if df is not None and not df.empty:
                frames.append(df)
            cur = seg_end + pd.Timedelta(days=1)
            time.sleep(0.4)
        allw = pd.concat(frames).drop_duplicates(["trade_date", "con_code"])
        allw = allw.rename(columns={})[["index_code", "trade_date", "con_code"]]
        allw["trade_date"] = pd.to_datetime(allw["trade_date"]).dt.date
        db.upsert_df(allw, "index_member", key_cols=["index_code", "trade_date", "con_code"])
        print(f"[data] index_member 已写入 {len(allw)} 行")
        rows = allw[["trade_date", "con_code"]].copy()
        rows["trade_date"] = pd.to_datetime(rows["trade_date"])

    rows["trade_date"] = pd.to_datetime(rows["trade_date"])
    avail_dates = sorted(rows["trade_date"].unique())
    members = {}
    for me in month_ends:
        me_dt = pd.to_datetime(me)
        usable = [d for d in avail_dates if d <= me_dt]
        if not usable:
            continue
        pick = usable[-1]
        members[me_dt] = rows.loc[rows["trade_date"] == pick, "con_code"].tolist()
    return members


def sync_prices(ts_codes: list, start: str, end: str):
    """
    批量下载日线+复权因子 → 后复权 → 写入 daily_price 表。
    已在库里的 (ts_code) 跳过，支持断点续传。
    """
    pro = get_pro()
    existing = db.read_sql("SELECT DISTINCT ts_code FROM daily_price")
    done = set(existing["ts_code"].tolist()) if not existing.empty else set()
    todo = [c for c in ts_codes if c not in done]
    if not todo:
        print("[data] daily_price 已全部就绪，跳过下载")
        return
    print(f"[data] 下载 {len(todo)} 只股票后复权价（已有 {len(done)} 只）...")

    for code in tqdm(todo):
        daily = adj = None
        for attempt in range(3):
            try:
                daily = pro.daily(ts_code=code, start_date=start, end_date=end,
                                  fields="ts_code,trade_date,close")
                adj = pro.adj_factor(ts_code=code, start_date=start, end_date=end,
                                     fields="trade_date,adj_factor")
                break
            except Exception as e:
                if attempt == 2:
                    print(f"  跳过 {code}: {e}")
                time.sleep(1)
        if daily is None or daily.empty or adj is None or adj.empty:
            continue
        d = daily.merge(adj, on="trade_date", how="left")
        d["adj_factor"] = d["adj_factor"].ffill().bfill()
        latest = d.sort_values("trade_date")["adj_factor"].iloc[-1]
        d["close_hfq"] = d["close"] * d["adj_factor"] / latest
        out = d[["ts_code", "trade_date", "close_hfq"]].copy()
        out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.date
        db.upsert_df(out, "daily_price", key_cols=["ts_code", "trade_date"])
        time.sleep(0.15)
    print("[data] daily_price 下载完成")


def load_price_panel(ts_codes: list = None) -> pd.DataFrame:
    """从库里读后复权价，转宽表：index=trade_date, columns=ts_code。
    传 ts_codes 则只读这些股票（港美股回测用，避免混入 A 股日历）。"""
    if ts_codes:
        ph = ",".join([f":c{i}" for i in range(len(ts_codes))])
        params = {f"c{i}": c for i, c in enumerate(ts_codes)}
        df = db.read_sql(f"SELECT ts_code, trade_date, close_hfq FROM daily_price "
                         f"WHERE ts_code IN ({ph})", params)
    else:
        df = db.read_sql("SELECT ts_code, trade_date, close_hfq FROM daily_price")
    if df.empty:
        return pd.DataFrame()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    panel = df.pivot(index="trade_date", columns="ts_code", values="close_hfq")
    return panel.sort_index()


def sync_daily_basic(month_ends: list):
    """
    下载每个调仓月底的全市场估值（PB/市值）→ daily_basic 表。
    按交易日查询（每月 1 次调用，~96 次），比逐股快得多。
    多因子策略只需调仓日的 PB，无需每日数据。
    """
    pro = get_pro()
    existing = db.read_sql("SELECT DISTINCT trade_date FROM daily_basic")
    done = set()
    if not existing.empty:
        done = set(pd.to_datetime(existing["trade_date"]).dt.strftime("%Y%m%d"))
    todo = [pd.to_datetime(m).strftime("%Y%m%d") for m in month_ends]
    todo = [d for d in todo if d not in done]
    if not todo:
        print("[data] daily_basic 已就绪，跳过")
        return
    print(f"[data] 下载 {len(todo)} 个调仓日的估值数据（PB/市值）...")
    for d in tqdm(todo):
        for attempt in range(3):
            try:
                df = pro.daily_basic(trade_date=d,
                                     fields="ts_code,trade_date,pb,total_mv")
                break
            except Exception as e:
                if attempt == 2:
                    print(f"  跳过 {d}: {e}")
                    df = None
                time.sleep(1)
        if df is None or df.empty:
            continue
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        db.upsert_df(df, "daily_basic", key_cols=["ts_code", "trade_date"])
        time.sleep(0.3)
    print("[data] daily_basic 下载完成")


def sync_fina_indicator(ts_codes: list):
    """
    下载财务指标 ROE（季度）→ fina_indicator 表。逐股下载（含公告日 ann_date 用于时点对齐）。
    从 2017 年起，保证 2019+ 调仓日有已公布的财报可用。
    """
    pro = get_pro()
    existing = db.read_sql("SELECT DISTINCT ts_code FROM fina_indicator")
    done = set(existing["ts_code"].tolist()) if not existing.empty else set()
    todo = [c for c in ts_codes if c not in done]
    if not todo:
        print("[data] fina_indicator 已就绪，跳过")
        return
    print(f"[data] 下载 {len(todo)} 只股票的 ROE 财务数据（已有 {len(done)} 只）...")
    for code in tqdm(todo):
        df = None
        for attempt in range(3):
            try:
                df = pro.fina_indicator(ts_code=code, start_date="20170101",
                                        end_date="20251231",
                                        fields="ts_code,ann_date,end_date,roe")
                break
            except Exception as e:
                if attempt == 2:
                    print(f"  跳过 {code}: {e}")
                time.sleep(1)
        if df is None or df.empty:
            continue
        df = df.drop_duplicates(subset=["ts_code", "end_date"])
        df["end_date"] = pd.to_datetime(df["end_date"]).dt.date
        df["ann_date"] = pd.to_datetime(df["ann_date"], errors="coerce").dt.date
        db.upsert_df(df[["ts_code", "end_date", "ann_date", "roe"]],
                     "fina_indicator", key_cols=["ts_code", "end_date"])
        time.sleep(0.2)
    print("[data] fina_indicator 下载完成")


# ==== 港美股同步（存进同一张 daily_price / benchmark 表，ts_code 天然不冲突）====

def _apply_us_splits(df: pd.DataFrame, splits: list) -> pd.DataFrame:
    """美股后向复权：拆股日之前的价格除以累计拆股比例，使序列连续。"""
    import numpy as np
    df = df.sort_values("trade_date").copy()
    factor = np.ones(len(df))
    td = pd.to_datetime(df["trade_date"])
    for sdate, ratio in splits:
        mask = (td < pd.to_datetime(sdate)).values
        factor[mask] *= ratio
    df["close_hfq"] = df["close"] / factor
    return df


def sync_global_prices(market: str, start: str = "20180101", end: str = "20251231",
                       force: bool = False):
    """下载美股/港股行情 → 复权（美股按拆股表）→ 存 daily_price 表。
    港股 hk_daily 限频 1次/分钟，故每只间隔 62 秒、限频报错时等待重试。
    force=True 时重下已有标的（用于扩展到更新日期）。"""
    pro = get_pro()
    universe = config.MARKET_UNIVERSE[market]
    delay = 62 if market == "HK" else 0.2   # HK 限速
    existing = db.read_sql("SELECT DISTINCT ts_code FROM daily_price")
    done = set() if force else (set(existing["ts_code"].tolist()) if not existing.empty else set())
    todo = [c for c in universe if c not in done]
    if not todo:
        print(f"[data] {market} 行情已就绪，跳过")
        return
    print(f"[data] 下载 {market} {len(todo)} 只股票行情（间隔 {delay}s）...")
    for code in tqdm(todo):
        df = None
        for attempt in range(4):
            try:
                if market == "US":
                    df = pro.us_daily(ts_code=code, start_date=start, end_date=end,
                                      fields="ts_code,trade_date,close")
                else:  # HK
                    df = pro.hk_daily(ts_code=code, start_date=start, end_date=end,
                                      fields="ts_code,trade_date,close")
                break
            except Exception as e:
                if attempt == 3:
                    print(f"  跳过 {code}: {e}")
                else:
                    time.sleep(delay if delay > 1 else 1)  # 限频报错时等一个周期再重试
        if df is None or df.empty:
            continue
        if market == "US" and code in config.US_SPLITS:
            df = _apply_us_splits(df, config.US_SPLITS[code])
        else:
            df["close_hfq"] = df["close"]
        out = df[["ts_code", "trade_date", "close_hfq"]].copy()
        out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.date
        db.upsert_df(out, "daily_price", key_cols=["ts_code", "trade_date"])
        time.sleep(delay)
    print(f"[data] {market} 行情下载完成")


def sync_hk_prices_akshare(start: str = "20180101", end: str = "20251231",
                           codes: list = None, force: bool = False):
    """用 akshare 下港股行情（Tushare hk_daily 限频 1次/分钟太狠）。
    akshare 免费不限频，且自带后复权(hfq)。存进 daily_price 表，ts_code 用 xxxxx.HK。
    codes 不传则用 config.HK_UNIVERSE（回测池）；传则下指定代码（如持仓里的额外股）。
    force=True 时重下已有标的（用于扩展到更新日期）。"""
    import akshare as ak
    universe = codes if codes else config.HK_UNIVERSE
    existing = db.read_sql("SELECT DISTINCT ts_code FROM daily_price")
    done = set() if force else (set(existing["ts_code"].tolist()) if not existing.empty else set())
    todo = [c for c in universe if c not in done]
    if not todo:
        print("[data] HK 行情已就绪，跳过")
        return
    print(f"[data] akshare 下载 HK {len(todo)} 只股票行情...")
    for code in tqdm(todo):
        sym = code.replace(".HK", "")  # akshare 用 5 位数字代码
        df = None
        for attempt in range(4):
            try:
                df = ak.stock_hk_hist(symbol=sym, period="daily",
                                      start_date=start, end_date=end, adjust="hfq")
                break
            except Exception as e:
                if attempt == 3:
                    print(f"  跳过 {code}: {e}")
                else:
                    time.sleep(5)  # 连接被掐断，等 5 秒重试
        if df is None or df.empty:
            continue
        df = df.rename(columns={"日期": "trade_date", "收盘": "close_hfq"})
        if "trade_date" not in df.columns or "close_hfq" not in df.columns:
            print(f"  跳过 {code}: 列名异常 {list(df.columns)}")
            continue
        df["ts_code"] = code
        out = df[["ts_code", "trade_date", "close_hfq"]].copy()
        out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.date
        db.upsert_df(out, "daily_price", key_cols=["ts_code", "trade_date"])
        time.sleep(3)  # 间隔 3 秒，避开 eastmoney 连接突发限制
    print("[data] HK(akshare) 行情下载完成")


def sync_us_prices_akshare(codes: list, start: str = "20180101", end: str = "20261231"):
    """用 akshare 下美股行情（Tushare us_daily 只到 2025，akshare 到当前）。
    后复权(hfq)，自动试纳斯达克(105.)/纽交所(106.)前缀。覆盖写入（持仓刷新到最新）。"""
    import akshare as ak
    print(f"[data] akshare 刷新美股 {len(codes)} 只到 {end}...")
    for code in tqdm(codes):
        df = None
        for prefix in ("105.", "106."):
            for attempt in range(4):
                try:
                    d = ak.stock_us_hist(symbol=prefix + code, period="daily",
                                         start_date=start, end_date=end, adjust="hfq")
                    if d is not None and not d.empty:
                        df = d
                    break  # 成功或空（前缀不对）→ 不再重试本前缀
                except Exception as e:
                    if attempt == 3:
                        print(f"  {code} ({prefix}) 连接失败: {repr(e)[:60]}")
                    else:
                        time.sleep(4)  # 连接被掐断，等 4 秒重试
            if df is not None:
                break
        if df is None or df.empty:
            print(f"  跳过 {code}: 取不到数据")
            continue
        df = df.rename(columns={"日期": "trade_date", "收盘": "close_hfq"})
        df["ts_code"] = code
        out = df[["ts_code", "trade_date", "close_hfq"]].copy()
        out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.date
        db.upsert_df(out, "daily_price", key_cols=["ts_code", "trade_date"])
        time.sleep(1)
    print("[data] 美股(akshare) 刷新完成")


def sync_recent_prices(tickers: list, start: str = "20250101", end: str = "20261231"):
    """抓近一年【原始价(不复权)】到 recent_price 表，供持仓择时信号用。
    近期无拆股，原始价做均线信号准确，且避开 akshare 复权 bug。
    港股用 stock_hk_hist，美股用 stock_us_hist(105./106.)，均 adjust=""。"""
    import akshare as ak
    # 港股用 stock_hk_hist，美股用 stock_us_hist(105./106.)，均原始价(adjust="")。
    # eastmoney 偶发限流，故每只多次重试 + 间隔。
    print(f"[data] 抓取 {len(tickers)} 只持仓近期原始价（信号用）...")
    for code in tqdm(tickers):
        df = None
        if code.endswith(".HK"):
            sym = code.replace(".HK", "")
            for attempt in range(5):
                try:
                    df = ak.stock_hk_hist(symbol=sym, period="daily",
                                          start_date=start, end_date=end, adjust="")
                    break
                except Exception:
                    if attempt < 4:
                        time.sleep(4)
        else:  # 美股：试纳斯达克(105.)/纽交所(106.)
            for prefix in ("105.", "106."):
                for attempt in range(5):
                    try:
                        d = ak.stock_us_hist(symbol=prefix + code, period="daily",
                                             start_date=start, end_date=end, adjust="")
                        if d is not None and not d.empty:
                            df = d
                        break
                    except Exception:
                        if attempt < 4:
                            time.sleep(4)
                if df is not None and not df.empty:
                    break
        if df is None or df.empty:
            print(f"  跳过 {code}: 取不到近期价")
            continue
        df = df.rename(columns={"日期": "trade_date", "收盘": "close",
                                "最高": "high", "最低": "low"})
        df["ts_code"] = code
        cols = ["ts_code", "trade_date", "close"] + \
               [c for c in ("high", "low") if c in df.columns]
        out = df[cols].copy()
        out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.date
        db.upsert_df(out, "recent_price", key_cols=["ts_code", "trade_date"])
        time.sleep(2)
    print("[data] 持仓近期价抓取完成")


def _fetch_twelvedata(symbol: str, outputsize: int = 300) -> pd.DataFrame:
    """Twelve Data 拉日线收盘（美股可靠、不限流）。key 从 TWELVEDATA_KEY 读，默认 demo（仅 AAPL）。"""
    import os
    import urllib.request
    import json
    key = os.environ.get("TWELVEDATA_KEY", "demo")
    url = (f"https://api.twelvedata.com/time_series?symbol={symbol}"
           f"&interval=1day&outputsize={outputsize}&apikey={key}")
    d = json.loads(urllib.request.urlopen(url, timeout=25).read())
    if d.get("status") != "ok" or not d.get("values"):
        raise RuntimeError(str(d.get("message", d))[:120])
    rows = [{"ts_code": symbol, "trade_date": pd.to_datetime(v["datetime"]).date(),
             "close": float(v["close"]),
             "high": float(v["high"]), "low": float(v["low"])} for v in d["values"]]
    return pd.DataFrame(rows)


def _fetch_us_akshare(code: str, start: str, end: str) -> pd.DataFrame:
    """akshare 美股原始价（Twelve Data 失败时的兜底，eastmoney 偶发限流）。"""
    import akshare as ak
    for prefix in ("105.", "106."):
        for attempt in range(3):
            try:
                d = ak.stock_us_hist(symbol=prefix + code, period="daily",
                                     start_date=start, end_date=end, adjust="")
                if d is not None and not d.empty:
                    df = d.rename(columns={"日期": "trade_date", "收盘": "close",
                                           "最高": "high", "最低": "low"})
                    df["ts_code"] = code
                    return df[["ts_code", "trade_date", "close", "high", "low"]]
                break
            except Exception:
                if attempt < 2:
                    time.sleep(3)
    return pd.DataFrame()


def refresh_holding_prices(tickers: list, start: str = "20250101", end: str = "20261231") -> dict:
    """随时刷新持仓当前价到 recent_price。美股优先 Twelve Data → 兜底 akshare；港股 akshare。
    返回 {updated:[...], failed:[...]}。"""
    import akshare as ak
    updated, failed = [], []
    sdt, edt = pd.to_datetime(start), pd.to_datetime(end)
    for code in tickers:
        try:
            if code.endswith(".HK"):
                sym = code.replace(".HK", "")
                df = None
                for attempt in range(4):
                    try:
                        # sina 源（stock_hk_daily），不走 eastmoney，不限流
                        df = ak.stock_hk_daily(symbol=sym, adjust="")
                        break
                    except Exception:
                        if attempt < 3:
                            time.sleep(3)
                if df is None or df.empty:
                    failed.append(code); continue
                df = df.rename(columns={"date": "trade_date"})
                df["trade_date"] = pd.to_datetime(df["trade_date"])
                df = df[(df["trade_date"] >= sdt) & (df["trade_date"] <= edt)]
                df["ts_code"] = code
                cols = ["ts_code", "trade_date", "close"] + \
                       [c for c in ("high", "low") if c in df.columns]
                out = df[cols].copy()
            else:  # 美股
                try:
                    out = _fetch_twelvedata(code)
                except Exception:
                    out = _fetch_us_akshare(code, start, end)
                if out is None or out.empty:
                    failed.append(code); continue
            out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.date
            db.upsert_df(out, "recent_price", key_cols=["ts_code", "trade_date"])
            updated.append(code)
        except Exception:
            failed.append(code)
    return {"updated": updated, "failed": failed}


def load_recent_price(ts_code: str) -> pd.Series:
    """读单只持仓近期原始价序列（信号用）。"""
    df = db.read_sql("SELECT trade_date, close FROM recent_price WHERE ts_code=:c ORDER BY trade_date",
                     {"c": ts_code})
    if df.empty:
        return pd.Series(dtype=float)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df.set_index("trade_date")["close"].sort_index()


def load_recent_ohlc(ts_code: str) -> pd.DataFrame:
    """读单只持仓近期 OHLC（close/high/low），真 ATR/Chandelier 止损用。
    high/low 可能为 NULL（旧数据未回填），调用方需容错降级到收盘价代理。"""
    df = db.read_sql("SELECT trade_date, close, high, low FROM recent_price "
                     "WHERE ts_code=:c ORDER BY trade_date", {"c": ts_code})
    if df.empty:
        return pd.DataFrame(columns=["close", "high", "low"])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df.set_index("trade_date").sort_index()


def load_combined_series(ts_code: str) -> pd.Series:
    """历史(daily_price 复权价) + 当前(recent_price 原始价) 无缝拼接，延伸到最新。
    在衔接日按比例对齐 recent → daily 的尺度，避免断层。供持仓分析用。"""
    panel = load_price_panel([ts_code])
    daily = panel[ts_code].dropna() if (not panel.empty and ts_code in panel.columns) else pd.Series(dtype=float)
    recent = load_recent_price(ts_code)
    if daily.empty:
        return recent
    if recent.empty:
        return daily
    overlap = daily.index.intersection(recent.index)
    if len(overlap):
        j = overlap.max()
        ratio = daily.loc[j] / recent.loc[j] if recent.loc[j] else 1.0
    else:
        ratio = 1.0  # 无重叠（如智谱）尽力拼接
    cutoff = daily.index.max()
    tail = recent[recent.index > cutoff] * ratio
    return pd.concat([daily, tail]).sort_index()


def sync_global_benchmark(market: str, start: str = "20180101", end: str = "20251231"):
    """下载全球指数（IXIC/HSI）→ benchmark 表，供基准+择时用。"""
    pro = get_pro()
    code = config.MARKET_BENCHMARK[market]
    existing = db.read_sql("SELECT 1 FROM benchmark WHERE index_code=:c LIMIT 1", {"c": code})
    if not existing.empty:
        print(f"[data] 基准 {code} 已就绪，跳过")
        return
    df = pro.index_global(ts_code=code, start_date=start, end_date=end,
                          fields="ts_code,trade_date,close")
    if df is None or df.empty:
        print(f"[data] 基准 {code} 无数据")
        return
    df = df.rename(columns={"ts_code": "index_code"})[["index_code", "trade_date", "close"]]
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    db.upsert_df(df, "benchmark", key_cols=["index_code", "trade_date"])
    print(f"[data] 基准 {code} 下载完成（{len(df)} 行）")


def month_ends_for_codes(ts_codes: list) -> list:
    """从指定股票的行情里推每月最后一个交易日（港美股日历与 A 股不同）。"""
    if not ts_codes:
        return []
    ph = ",".join([f":c{i}" for i in range(len(ts_codes))])
    params = {f"c{i}": c for i, c in enumerate(ts_codes)}
    df = db.read_sql(f"SELECT DISTINCT trade_date FROM daily_price WHERE ts_code IN ({ph}) "
                     f"ORDER BY trade_date", params)
    if df.empty:
        return []
    s = pd.to_datetime(df["trade_date"])
    return list(s.groupby(s.dt.to_period("M")).max())


# ---------- 纯读库接口（web 回测用，不碰 Tushare）----------

def month_ends_from_db() -> list:
    """从 daily_price 已有交易日里推每月最后一个交易日（不调 Tushare）。"""
    df = db.read_sql("SELECT DISTINCT trade_date FROM daily_price ORDER BY trade_date")
    if df.empty:
        return []
    s = pd.to_datetime(df["trade_date"])
    return list(s.groupby(s.dt.to_period("M")).max())


def load_pb_panel() -> pd.DataFrame:
    """从库里读 PB，转宽表：index=trade_date, columns=ts_code, values=pb。"""
    df = db.read_sql("SELECT ts_code, trade_date, pb FROM daily_basic WHERE pb IS NOT NULL")
    if df.empty:
        return pd.DataFrame()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df.pivot(index="trade_date", columns="ts_code", values="pb").sort_index()


def load_roe_asof_panel(month_ends: list) -> pd.DataFrame:
    """
    构建 ROE 时点对齐面板：index=调仓日, columns=ts_code, values=ROE。
    每个调仓日只用【公告日 ann_date <= 调仓日】的最新一期财报，避免偷看未来。
    """
    df = db.read_sql("SELECT ts_code, ann_date, end_date, roe FROM fina_indicator "
                     "WHERE roe IS NOT NULL")
    if df.empty:
        return pd.DataFrame()
    # ann_date 缺失时用 end_date + 估计披露延迟（保守起见用 end_date 本身）
    df["ann_date"] = pd.to_datetime(df["ann_date"], errors="coerce")
    df["end_date"] = pd.to_datetime(df["end_date"], errors="coerce")
    df["ann_date"] = df["ann_date"].fillna(df["end_date"])
    df = df.dropna(subset=["ann_date"]).sort_values("ann_date")

    out = {}
    for me in month_ends:
        me = pd.to_datetime(me)
        sub = df[df["ann_date"] <= me]
        if sub.empty:
            continue
        out[me] = sub.groupby("ts_code")["roe"].last()  # 按公告日排序后取最新一期
    if not out:
        return pd.DataFrame()
    return pd.DataFrame(out).T.sort_index()


def load_members_from_db(index_code: str, month_ends: list) -> dict:
    """从 index_member 表读动态成分股（不调 Tushare）。"""
    rows = db.read_sql(
        "SELECT trade_date, con_code FROM index_member WHERE index_code=:ic",
        {"ic": index_code},
    )
    if rows.empty:
        return {}
    rows["trade_date"] = pd.to_datetime(rows["trade_date"])
    avail = sorted(rows["trade_date"].unique())
    members = {}
    for me in month_ends:
        me_dt = pd.to_datetime(me)
        usable = [d for d in avail if d <= me_dt]
        if not usable:
            continue
        pick = usable[-1]
        members[me_dt] = rows.loc[rows["trade_date"] == pick, "con_code"].tolist()
    return members


def load_benchmark_from_db(index_code: str) -> pd.Series:
    """从 benchmark 表读基准净值（不调 Tushare）。"""
    rows = db.read_sql(
        "SELECT trade_date, close FROM benchmark WHERE index_code=:ic ORDER BY trade_date",
        {"ic": index_code},
    )
    if rows.empty:
        return pd.Series(dtype=float)
    rows["trade_date"] = pd.to_datetime(rows["trade_date"])
    s = rows.set_index("trade_date")["close"].sort_index()
    return s / s.iloc[0]


def sync_benchmark(index_code: str, start: str, end: str) -> pd.Series:
    """下载/读取基准指数净值，存 benchmark 表，返回归一化 Series。"""
    rows = db.read_sql(
        "SELECT trade_date, close FROM benchmark WHERE index_code=:ic ORDER BY trade_date",
        {"ic": index_code},
    )
    if rows.empty:
        pro = get_pro()
        df = pro.index_daily(ts_code=index_code, start_date=start, end_date=end,
                             fields="ts_code,trade_date,close")
        df = df.rename(columns={"ts_code": "index_code"})[["index_code", "trade_date", "close"]]
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        db.upsert_df(df, "benchmark", key_cols=["index_code", "trade_date"])
        rows = df[["trade_date", "close"]].copy()
    rows["trade_date"] = pd.to_datetime(rows["trade_date"])
    s = rows.set_index("trade_date")["close"].sort_index()
    return s / s.iloc[0]
