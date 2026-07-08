"""
市场风险环境评估（ETF 代理版）。

本应直接用 FRED 高收益债利差 `BAMLH0A0HYM2`，但北京服务器访问 FRED 被墙，
故改用 Twelve Data 可达的 ETF 构造等价代理（方向一致）：
  - 信用利差代理 = HYG/LQD 比值。高收益债(HYG)相对投资级债(LQD)走弱 → 信用利差走扩 → 风险上升。
    两者都是债券，比值剔除利率因素、隔离信用风险，是 HY OAS 方向的干净代理。
  - 股市趋势 = SPY 相对 200日均线（跌破=风险）。

两信号各自 z-score(对齐为"越高越危险") → 等权合成 → 正态CDF 映射 0-100 风险分 → 分级。
（等权+z-score 是研究验证优于 PCA 的稳健做法。）仅技术/风险提示，非投资建议。
"""
import io
import json
import math
import os
import time
import urllib.request

import pandas as pd

import data


def _fred_oas() -> pd.Series:
    """FRED API 取真实高收益债利差 BAMLH0A0HYM2（需环境变量 FRED_API_KEY）。
    墙外节点(如 Zeabur 泰国)可用；无 key 或失败返回 None，调用方降级到 ETF 代理。"""
    key = os.environ.get("FRED_API_KEY")
    if not key:
        return None
    try:
        url = ("https://api.stlouisfed.org/fred/series/observations"
               f"?series_id=BAMLH0A0HYM2&api_key={key}&file_type=json"
               "&sort_order=desc&limit=300")
        req = urllib.request.Request(url, headers={"User-Agent": "quant-bt/1.0"})
        d = json.loads(urllib.request.urlopen(req, timeout=20).read())
        rows = [(o["date"], float(o["value"])) for o in d.get("observations", [])
                if o.get("value") not in (".", None, "")]
        if not rows:
            return None
        s = pd.Series({pd.to_datetime(dt): v for dt, v in rows}).sort_index()
        return s
    except Exception as e:
        print("[macro] FRED API 失败，降级 ETF 代理:", repr(e)[:100])
        return None


def _assess_from_oas(s: pd.Series) -> dict:
    """用真实 HY OAS 水平+变化分级。参考：<3.5 偏低 · ~4.5-5 中位 · >5 警戒 · >6 压力。"""
    latest = float(s.iloc[-1])
    prev_1m = float(s.iloc[-22]) if len(s) >= 22 else float(s.iloc[0])
    chg_1m = latest - prev_1m
    yr = s.tail(252)
    pctile = round(float((yr < latest).mean()) * 100) if len(yr) > 30 else None
    if chg_1m > 0.2:
        credit_trend = "利差走扩↑"
    elif chg_1m < -0.2:
        credit_trend = "利差收窄↓"
    else:
        credit_trend = "利差平稳→"
    if latest >= 6.0 or (latest >= 5.0 and chg_1m >= 0.5):
        status, emoji, note, score = "压力", "🔴", "信用紧张，市场风险高，避免加仓、留意减仓", 85
    elif latest >= 5.0 or chg_1m >= 0.75:
        status, emoji, note, score = "警戒", "🟠", "信用利差走扩，风险上升，谨慎加仓", 65
    elif latest < 3.5:
        status, emoji, note, score = "宽松", "🟢", "利差偏低、市场自满，别追高", 25
    else:
        status, emoji, note, score = "正常", "🟢", "信用环境正常", 45
    return {"status": status, "emoji": emoji, "note": note, "score": score,
            "credit_trend": credit_trend, "spy_above_ma": None,
            "source": f"FRED HY OAS {latest}%（近1年{pctile}分位）" if pctile is not None
            else f"FRED HY OAS {latest}%",
            "asof": s.index[-1].strftime("%Y-%m-%d")}


def _phi(x: float) -> float:
    """标准正态 CDF（不依赖 scipy）。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _close_series(symbol: str, retries: int = 2) -> pd.Series:
    """用 Twelve Data 取 ETF 日收盘序列（复用 data 层，服务器可达）。
    免费版 8 次/分钟，撞 429 时等 65s 让窗口重置后重试（自愈）。"""
    for attempt in range(retries + 1):
        try:
            df = data._fetch_twelvedata(symbol)
            break
        except Exception as e:
            if "429" in str(e) and attempt < retries:
                print(f"[macro] {symbol} 429 限流，等 65s 重试")
                time.sleep(65)
                continue
            raise
    df = df.sort_values("trade_date")
    s = pd.Series(df["close"].values,
                  index=pd.to_datetime(df["trade_date"]), dtype=float)
    return s[~s.index.duplicated(keep="last")].sort_index()


def _z_latest(s: pd.Series, window: int = 252):
    """最新值相对近 window 个观测的 z 分数 + 均值。数据不足返回 (None, None)。"""
    s = s.dropna()
    if len(s) < 30:
        return None, None
    w = s.tail(window)
    mu, sd = float(w.mean()), float(w.std())
    if sd == 0:
        return 0.0, mu
    return (float(s.iloc[-1]) - mu) / sd, mu


def assess_market_risk() -> dict:
    """评估市场风险环境。任何取数/计算失败返回 None（调用方跳过展示，不影响推送）。
    优先真实 FRED HY OAS（需 FRED_API_KEY，墙外节点）；否则降级 Twelve Data ETF 代理。"""
    oas = _fred_oas()
    if oas is not None and len(oas) > 30:
        return _assess_from_oas(oas)
    try:
        hyg = _close_series("HYG")
        time.sleep(8)   # 免费版 8次/分钟，调用间隔避免自我撞限流
        lqd = _close_series("LQD")
        time.sleep(8)
        spy = _close_series("SPY")
    except Exception as e:
        print("[macro] 取数失败:", repr(e)[:120])
        return None
    if hyg.empty or lqd.empty or spy.empty:
        return None

    # 信用利差代理：HYG/LQD 比值（越低=信用利差越宽=风险越高 → 取负 z）
    ratio = (hyg / lqd.reindex(hyg.index).ffill()).dropna()
    z_ratio, _ = _z_latest(ratio)
    # 1 个月比值变化方向（判断利差走扩/收窄）
    if len(ratio) >= 22:
        chg_1m = float(ratio.iloc[-1] / ratio.iloc[-22] - 1)
    else:
        chg_1m = 0.0
    if chg_1m < -0.01:
        credit_trend = "利差走扩↑"
    elif chg_1m > 0.01:
        credit_trend = "利差收窄↓"
    else:
        credit_trend = "利差平稳→"

    # 股市趋势：SPY 相对 200 日均线
    ma200 = spy.rolling(200).mean()
    spy_ext = (spy / ma200 - 1.0).dropna()
    z_ext, _ = _z_latest(spy_ext)
    above = bool(spy.iloc[-1] > ma200.iloc[-1]) if ma200.notna().any() else None

    if z_ratio is None or z_ext is None:
        return None

    # 合成（对齐为"越高越危险"）：信用风险 = -z(比值)，趋势风险 = -z(距年线)
    risk_credit = -z_ratio
    risk_trend = -z_ext
    composite = (risk_credit + risk_trend) / 2.0
    score = round(100 * _phi(composite))

    if score >= 75:
        status, emoji, note = "压力", "🔴", "信用+趋势双警，市场风险高，避免加仓、留意减仓"
    elif score >= 58:
        status, emoji, note = "警戒", "🟠", "风险抬升，谨慎加仓"
    elif score >= 35:
        status, emoji, note = "正常", "🟢", "风险环境中性偏稳"
    else:
        status, emoji, note = "宽松", "🟢", "风险偏好高，环境宽松"

    asof = spy.index[-1].strftime("%Y-%m-%d")
    return {"status": status, "emoji": emoji, "note": note, "score": score,
            "credit_trend": credit_trend, "spy_above_ma": above,
            "z_credit": round(risk_credit, 2), "z_trend": round(risk_trend, 2),
            "asof": asof}
