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


# ==== 短线动量（swing）取数：OHLCV + 东财行情榜 ====
# 为什么另起函数而不改 _fetch_twelvedata：现有调用方（refresh_holding_prices / sector_rotation）
# upsert 的列集合不许变；EP 形态要 open/volume，只能由新函数产出、新代码写入。
# Twelve Data 免费版 8 次/分钟、800 次/天且与生产其他推送共用 → 统一走 td_fetch_paced 节流计数。

import json as _json
import urllib.parse as _urlparse
import urllib.request as _urlreq

_EM_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}
OHLCV_COLS = ["ts_code", "trade_date", "open", "high", "low", "close", "volume"]

# Twelve Data 调用状态（进程内共享：market_env 与 swing_scan 一起节流、一起计数）
_TD_STATE = {"last": 0.0, "calls": 0, "day_exhausted": False}


class TDQuotaError(RuntimeError):
    """Twelve Data 当日额度用尽 / 本次预算用尽：调用方应停止再调，改走兜底。"""


def td_credits_used() -> int:
    return int(_TD_STATE["calls"])


def td_reset_counter():
    _TD_STATE.update({"calls": 0, "day_exhausted": False})


def fetch_ohlcv_td(symbol: str, outputsize: int = 220) -> pd.DataFrame:
    """Twelve Data 单次日线 OHLCV（拆股已复权到当前价位，不含分红）。不节流、不重试——
    节流用 td_fetch_paced。错误抛 RuntimeError，消息带 TD 的错误码（"429 ..." 便于识别限流）。"""
    import os
    key = os.environ.get("TWELVEDATA_KEY", "demo")
    q = {"symbol": symbol, "interval": "1day", "outputsize": int(outputsize), "apikey": key}
    url = "https://api.twelvedata.com/time_series?" + _urlparse.urlencode(q)
    d = _json.loads(_urlreq.urlopen(url, timeout=25).read())
    if d.get("status") != "ok" or not d.get("values"):
        raise RuntimeError(f"{d.get('code', '')} {str(d.get('message', d))[:150]}".strip())
    rows = []
    for v in d["values"]:
        def _f(k):
            try:
                return float(v[k])
            except (KeyError, TypeError, ValueError):
                return float("nan")
        rows.append({"ts_code": symbol, "trade_date": pd.to_datetime(v["datetime"]).date(),
                     "open": _f("open"), "high": _f("high"), "low": _f("low"),
                     "close": _f("close"), "volume": _f("volume")})
    df = pd.DataFrame(rows, columns=OHLCV_COLS)
    return df.sort_values("trade_date").drop_duplicates("trade_date", keep="last").reset_index(drop=True)


def td_fetch_paced(symbol: str, outputsize: int = 220, pause_s: float = 9.0,
                   wait_429_s: float = 65.0, retries: int = 2, budget: int = None) -> pd.DataFrame:
    """带节流的 fetch_ohlcv_td：两次调用间隔 ≥ pause_s；分钟限流(429) 等 wait_429_s 重试；
    当日额度用尽或超出 budget（本进程累计调用上限）→ 抛 TDQuotaError，调用方改走兜底。"""
    last_err = None
    for attempt in range(retries + 1):
        if _TD_STATE["day_exhausted"]:
            raise TDQuotaError("Twelve Data 当日额度已用尽")
        if budget is not None and _TD_STATE["calls"] >= budget:
            raise TDQuotaError(f"本次 Twelve Data 预算 {budget} 次已用完")
        wait = pause_s - (time.time() - _TD_STATE["last"])
        if wait > 0:
            time.sleep(wait)
        _TD_STATE["last"] = time.time()
        _TD_STATE["calls"] += 1
        try:
            return fetch_ohlcv_td(symbol, outputsize)
        except Exception as e:
            msg = str(e)
            low = msg.lower()
            last_err = e
            if "for the day" in low or "daily" in low and "credit" in low:
                _TD_STATE["day_exhausted"] = True
                raise TDQuotaError(msg[:120])
            if ("429" in msg or ("credits" in low and "minute" in low)) and attempt < retries:
                print(f"[data] Twelve Data {symbol} 429 限流，等 {wait_429_s:.0f}s")
                time.sleep(wait_429_s)
                continue
            raise
    raise last_err if last_err else RuntimeError("Twelve Data 重试耗尽")


def em_to_td_symbol(em_code: str):
    """东财代码 → Twelve Data/库内代码。BRK_B→BRK.B；_U/_W/_R 等单位/权证/配股权返回 None。"""
    em_code = str(em_code or "").strip().upper()
    if not em_code:
        return None
    if "_" in em_code:
        base, suf = em_code.split("_", 1)
        return f"{base}.{suf}" if suf in ("A", "B", "C") else None
    return em_code if em_code.replace(".", "").isalnum() else None


def td_to_em_code(symbol: str) -> str:
    """库内代码 → 东财代码（BRK.B→BRK_B）。"""
    return str(symbol).upper().replace(".", "_")


_EM_STATE = {"last": 0.0, "fails": 0}
# 熔断：连续这么多只股票"所有请求都连不上"（不是"没这只票"）→ 本进程不再试东财日K。
# 为什么：一只票最坏要 3 个市场号 × 多次退避 ≈ 2 分钟；Twelve Data 额度用完后剩下几十只全走兜底，
# 东财又不可达（本地实测整段掐断）时，一次扫描会拖到一两个小时、撞上美股开盘。
EM_BREAKER = 2


def em_reset_breaker():
    _EM_STATE["fails"] = 0


def fetch_ohlcv_em(symbol: str, market=None, days: int = 220, pause_s: float = 3.0,
                   retries: int = 4) -> pd.DataFrame:
    """东财日K兜底（fqt=0 原始价：前复权是加法复权、早年会出现负价，不能用）。
    东财连续请求会被掐断（RemoteDisconnected，冷却数分钟）→ 调用间隔 ≥ pause_s + 指数退避。
    market 用行情榜 f13（105 纳斯达克 / 106 纽交所 / 107 美交所）；不知道就三个都试（每个少试几次）。
    失败返回空 DataFrame（不抛）。连续 EM_BREAKER 只连不上 → 熔断，之后直接返回空。
    注意：原始价在拆股日会断层，只适合当短期兜底（get_ohlcv 不会把它写进 recent_price）。"""
    empty = pd.DataFrame(columns=OHLCV_COLS)
    if _EM_STATE["fails"] >= EM_BREAKER:
        return empty
    markets = [int(market)] if market not in (None, "", 0) else [105, 106, 107]
    if len(markets) > 1:
        retries = min(int(retries), 2)
    code = td_to_em_code(symbol)
    reached = False                 # 至少有一次请求拿到了响应（"没这只票"不算连不上）
    for mkt in markets:
        url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
               f"secid={mkt}.{code}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57"
               f"&klt=101&fqt=0&beg=0&end=20500101&lmt={int(days)}")
        kl = None
        for attempt in range(retries):
            wait = pause_s - (time.time() - _EM_STATE["last"])
            if wait > 0:
                time.sleep(wait)
            _EM_STATE["last"] = time.time()
            try:
                req = _urlreq.Request(url, headers=_EM_UA)
                d = _json.loads(_urlreq.urlopen(req, timeout=20).read().decode())
                reached = True
                kl = (d.get("data") or {}).get("klines") or []
                break
            except Exception as e:
                if attempt < retries - 1:
                    back = pause_s * (2 ** (attempt + 1))
                    print(f"[data] 东财日K {symbol} 失败 {repr(e)[:60]}，{back:.0f}s 后重试")
                    time.sleep(back)
        if not kl:
            continue
        rows = []
        for line in kl:
            p = line.split(",")
            try:
                rows.append({"ts_code": symbol, "trade_date": pd.to_datetime(p[0]).date(),
                             "open": float(p[1]), "close": float(p[2]), "high": float(p[3]),
                             "low": float(p[4]), "volume": float(p[5])})
            except (IndexError, ValueError):
                continue
        if rows:
            _EM_STATE["fails"] = 0
            df = pd.DataFrame(rows, columns=OHLCV_COLS)
            return df.sort_values("trade_date").drop_duplicates("trade_date", keep="last") \
                     .reset_index(drop=True)
    if not reached:
        _EM_STATE["fails"] += 1
        if _EM_STATE["fails"] >= EM_BREAKER:
            print(f"[data] 东财日K 连续 {_EM_STATE['fails']} 只连不上，本次不再用东财兜底")
    return empty


def _num(v):
    """东财字段 '-' / None → NaN。"""
    try:
        f = float(v)
        return f if f == f else float("nan")
    except (TypeError, ValueError):
        return float("nan")


def us_liquid_list(pages: int = 6, sort: str = "f6", pause_s: float = 1.5,
                   min_dollar_vol: float = None, retries: int = 5) -> pd.DataFrame:
    """东财美股行情榜（push2.eastmoney.com clist）：按 sort 倒序翻页，一页最多 100 行。
    sort='f6' 成交额 / 'f3' 当日涨幅 / 'f24' 60 日涨幅。按成交额翻页时，末行成交额已低于
    min_dollar_vol 就提前停（后面只会更小）。实测字段（2026-10）：
      f12代码 f13市场 f14名称 f2最新价 f3涨跌幅% f5成交量 f6成交额$ f15高 f16低 f17开 f18昨收
      f20总市值$（ETF/基金为 '-'）f24 60日涨跌幅% f25 年初至今% f124 更新时间戳
    返回列：ts_code, em_code, market, name, price, pct_chg, dollar_vol, mcap, open, high, low,
           prev_close, volume, chg_60d, chg_ytd, quote_ts。全失败抛 RuntimeError。"""
    fields = "f12,f13,f14,f2,f3,f5,f6,f15,f16,f17,f18,f20,f24,f25,f124"
    rows, errs = [], []
    for pn in range(1, int(pages) + 1):
        url = ("https://push2.eastmoney.com/api/qt/clist/get?"
               f"pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid={sort}"
               f"&fs=m:105,m:106,m:107&fields={fields}")
        diff = None
        for attempt in range(retries):     # 东财偶发 502 / 掐断：指数退避（封顶 60 秒）
            try:
                req = _urlreq.Request(url, headers=_EM_UA)
                d = _json.loads(_urlreq.urlopen(req, timeout=20).read().decode())
                diff = (d.get("data") or {}).get("diff") or []
                break
            except Exception as e:
                errs.append(repr(e)[:80])
                if attempt < retries - 1:
                    time.sleep(min(60.0, pause_s * 2 * (2 ** attempt)))
        if diff is None:
            if not rows:
                raise RuntimeError(f"东财行情榜第{pn}页失败: {errs[-1] if errs else ''}")
            print(f"[data] 东财行情榜第{pn}页失败，用已取到的 {len(rows)} 行")
            break
        if isinstance(diff, dict):
            diff = list(diff.values())
        if not diff:
            break
        rows.extend(diff)
        if sort == "f6" and min_dollar_vol is not None:
            last = _num(diff[-1].get("f6"))
            if last == last and last < min_dollar_vol:
                break
        if pn < pages:
            time.sleep(pause_s)
    out = []
    for r in rows:
        sym = em_to_td_symbol(r.get("f12"))
        if not sym:
            continue
        mkt = r.get("f13")
        out.append({"ts_code": sym, "em_code": str(r.get("f12")),
                    "market": int(mkt) if isinstance(mkt, (int, float)) else None,
                    "name": str(r.get("f14") or sym), "price": _num(r.get("f2")),
                    "pct_chg": _num(r.get("f3")) / 100.0, "dollar_vol": _num(r.get("f6")),
                    "mcap": _num(r.get("f20")), "open": _num(r.get("f17")),
                    "high": _num(r.get("f15")), "low": _num(r.get("f16")),
                    "prev_close": _num(r.get("f18")), "volume": _num(r.get("f5")),
                    "chg_60d": _num(r.get("f24")) / 100.0, "chg_ytd": _num(r.get("f25")) / 100.0,
                    "quote_ts": _num(r.get("f124"))})
    df = pd.DataFrame(out)
    if df.empty:
        return df
    return df.drop_duplicates("ts_code").reset_index(drop=True)


# Nasdaq 名称后缀（"Agilent Technologies Inc. Common Stock" → "Agilent Technologies Inc."）
_NASDAQ_NAME_TAILS = (" Common Stock", " Ordinary Shares", " American Depositary Shares",
                      " Depositary Shares", " Class A", " Class B", " Class C")


def us_list_nasdaq(timeout: int = 60) -> pd.DataFrame:
    """兜底：Nasdaq 官网 screener（一次请求全美股：价/涨跌/量/市值；成交额=价×量）。
    东财行情榜挂了（实测会整段 502）时用。没有 60 日涨幅和开盘价 → 调用方的 RS 补充、EP 跳空候选会停用。
    返回列与 us_liquid_list 相同（缺的字段为 NaN），按成交额倒序。失败抛异常。"""
    url = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&download=true"
    hdr = dict(_EM_UA, **{"Accept": "application/json, text/plain, */*",
                          "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"})
    d = _json.loads(_urlreq.urlopen(_urlreq.Request(url, headers=hdr), timeout=timeout).read().decode())
    out = []
    for r in (d.get("data") or {}).get("rows") or []:
        raw = str(r.get("symbol", "")).strip()
        if not raw or "^" in raw:                      # 优先股
            continue
        sym = em_to_td_symbol(raw.replace("/", "_"))
        if not sym:
            continue
        px = _num(str(r.get("lastsale", "")).replace("$", "").replace(",", ""))
        chg = _num(str(r.get("netchange", "")).replace(",", ""))
        pct = _num(str(r.get("pctchange", "")).replace("%", "").replace(",", ""))
        vol = _num(r.get("volume"))
        name = str(r.get("name") or sym)
        for t in _NASDAQ_NAME_TAILS:
            name = name.split(t)[0]
        out.append({"ts_code": sym, "em_code": td_to_em_code(sym), "market": None, "name": name.strip(),
                    "price": px, "pct_chg": pct / 100.0, "dollar_vol": px * vol, "mcap": _num(r.get("marketCap")),
                    "open": float("nan"), "high": float("nan"), "low": float("nan"),
                    "prev_close": px - chg if (px == px and chg == chg) else float("nan"), "volume": vol,
                    "chg_60d": float("nan"), "chg_ytd": float("nan"), "quote_ts": float("nan")})
    df = pd.DataFrame(out)
    if df.empty:
        raise RuntimeError("Nasdaq screener 无数据")
    return df.drop_duplicates("ts_code").sort_values("dollar_vol", ascending=False).reset_index(drop=True)


def load_recent_ohlcv(ts_code: str) -> pd.DataFrame:
    """读单只近期 OHLCV（open/high/low/close/volume，index=trade_date）。
    open/volume 可能为 NULL（由现有只写 close/high/low 的代码写入的行），调用方需容错。"""
    df = db.read_sql("SELECT trade_date, `open`, high, low, close, volume FROM recent_price "
                     "WHERE ts_code=:c ORDER BY trade_date", {"c": ts_code})
    if df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df.columns = [c.lower() for c in df.columns]
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.set_index("trade_date").sort_index()


def save_recent_ohlcv(df: pd.DataFrame):
    """把 OHLCV 写进 recent_price：只写自己的列集合（ts_code, trade_date, open, high, low, close, volume）。
    写之前自己先调 db.ensure_swing_schema()（open/volume 列的幂等迁移；同进程只真正执行一次）——
    不依赖调用方记得调，"代码先上线、列后加"那种崩溃从构造上排除。迁移失败就抛，由调用方降级。"""
    if df is None or df.empty:
        return
    db.ensure_swing_schema()
    out = df[OHLCV_COLS].copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.date
    out = out.dropna(subset=["close"])
    db.upsert_df(out, "recent_price", key_cols=["ts_code", "trade_date"])


def _et_now():
    """美东当前时间（naive）。容器里可能没有 tzdata → 退回 UTC-4 近似（夏令时口径，冬令时偏 1 小时，
    只用来判断"今天这根日线收盘了没"，偏差无害）。"""
    from datetime import datetime, timedelta, timezone
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
    except Exception:
        return (datetime.now(timezone.utc) - timedelta(hours=4)).replace(tzinfo=None)


def drop_partial_bar(df: pd.DataFrame) -> pd.DataFrame:
    """美股盘中运行时，数据源会给出"今天还没收完"的日线——去掉它（不能拿半根 K 线判形态/写缓存）。"""
    if df is None or df.empty:
        return df
    now = _et_now()
    if now.hour * 60 + now.minute >= 16 * 60 + 15:
        return df
    td = pd.to_datetime(df["trade_date"]).dt.date
    return df[td < now.date()].reset_index(drop=True)


def get_ohlcv(symbol: str, market=None, use_db: bool = True, cfg: dict = None) -> tuple:
    """取单只近期 OHLCV（index=trade_date，列 open/high/low/close/volume），返回 (df, source)。
    source ∈ td_full / td_incr / em / cache / none。
      - use_db=True：先读 recent_price 缓存；缓存够用（≥160 根完整 OHLCV、最新一根不超过 10 天）
        就只增量取 hist_incr 根，否则取 hist_full 根；新数据写回 recent_price（只写 OHLCV 列）。
        增量与缓存重叠日的收盘对不上（>2%，多半是拆股复权），重取全量并以新数据为准。
      - use_db=False（dry-run）：不读不写库，直接取 hist_full 根。
      - Twelve Data 失败/额度用尽 → 东财日K兜底（只在内存里用，不写库）→ 再不行用缓存
        （可能过期，由调用方判断新鲜度）。
    调用方负责 use_db=True 时先 db.ensure_swing_schema()。"""
    c = dict(config.SWING)
    if cfg:
        c.update(cfg)
    full, incr = int(c.get("hist_full", 220)), int(c.get("hist_incr", 15))
    keep = int(c.get("hist_keep", 260))
    td_kw = {"pause_s": float(c.get("td_pause_s", 9)), "wait_429_s": float(c.get("td_429_wait_s", 65)),
             "budget": c.get("td_budget")}
    cached = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    if use_db:
        try:
            cached = load_recent_ohlcv(symbol)
        except Exception as e:
            print(f"[data] 读缓存 {symbol} 失败: {repr(e)[:80]}")
    good = cached.dropna(subset=["open", "high", "low", "close", "volume"]) if not cached.empty else cached
    fresh_cache = (len(good) >= 160 and
                   (pd.Timestamp(_et_now().date()) - good.index.max()).days <= 10)
    want = incr if fresh_cache else full

    def _td(n):
        return drop_partial_bar(td_fetch_paced(symbol, n, **td_kw))

    new, source = None, "none"
    try:
        new, source = _td(want), ("td_incr" if fresh_cache else "td_full")
    except Exception as e:
        print(f"[data] Twelve Data {symbol} 失败，退东财日K: {repr(e)[:100]}")
        new = drop_partial_bar(fetch_ohlcv_em(symbol, market, days=max(want, 30),
                                              pause_s=float(c.get("em_kline_pause_s", 3))))
        source = "em" if new is not None and not new.empty else "none"

    split_unresolved = False
    if new is not None and not new.empty and fresh_cache:
        nd = new.set_index(pd.to_datetime(new["trade_date"]))["close"]
        ov = nd.index.intersection(good.index)
        if len(ov):
            dev = float((nd.loc[ov] / good["close"].loc[ov] - 1).abs().median())
            if dev > 0.02:
                print(f"[data] {symbol} 新旧收盘偏差 {dev:.1%}（疑似拆股），重取全量")
                try:
                    new, source = _td(full), "td_full"
                    if use_db:
                        _forget_old_scale(symbol, pd.to_datetime(new["trade_date"]).min())
                except Exception:
                    # 重取失败：手里只有一小段新口径数据。不能写库——否则库里变成"旧口径历史 + 新口径近 15 天"，
                    # 下次增量与库重叠段对得上、偏差检测失效，混合序列就永久留下了。
                    split_unresolved = True
                cut = pd.to_datetime(new["trade_date"]).min()
                cached, good = cached[cached.index >= cut], good[good.index >= cut]

    if new is None or new.empty:
        if cached.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"]), "none"
        return cached.tail(keep), "cache"
    # 只缓存 Twelve Data（拆股已复权，与现有 refresh_holding_prices / sector_rotation 同口径）。
    # 东财兜底是 fqt=0 原始价：拆股前的价位与 TD 不同口径，写进共享的 recent_price 会污染
    # levels.py / sector_rotation 读的 close/high/low；成交量单位也未实测核对。只在内存里用。
    if use_db and source in ("td_full", "td_incr") and not split_unresolved:
        try:
            save_recent_ohlcv(new)
        except Exception as e:
            print(f"[data] 写缓存 {symbol} 失败: {repr(e)[:80]}")
    nd = new.copy()
    nd["trade_date"] = pd.to_datetime(nd["trade_date"])
    nd = nd.set_index("trade_date")[["open", "high", "low", "close", "volume"]]
    # 只拼接完整 OHLCV 的缓存行（= 本模块自己写的 TD 数据）。现有模块只写 close/high/low 的行
    # （更早的历史、或拆股后被 _forget_old_scale 作废的旧口径行）不拼进来，免得混口径。
    if not good.empty:
        old = good[~good.index.isin(nd.index)][["open", "high", "low", "close", "volume"]]
        nd = pd.concat([old, nd]).sort_index()
    return nd.tail(keep), source


def _forget_old_scale(symbol: str, before):
    """拆股后重取了全量：库里早于新数据起点的行是旧口径（拆股前价位）。只把本模块自己的列
    open/volume 置空——它们从此不算"完整 OHLCV"，不会再被拼进短线序列；现有模块读的 close/high/low
    一个字都不动（那是它们自己的数据，口径由它们自己负责）。失败只打印。"""
    try:
        from sqlalchemy import text
        with db.get_engine().begin() as conn:
            conn.execute(text("UPDATE recent_price SET `open`=NULL, volume=NULL "
                              "WHERE ts_code=:c AND trade_date < :d"),
                         {"c": symbol, "d": pd.Timestamp(before).date()})
    except Exception as e:
        print(f"[data] {symbol} 作废旧口径缓存失败: {repr(e)[:80]}")
