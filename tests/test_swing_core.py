"""
短线动量核心（swing_scan / market_env / notify_swing / db 迁移 / data 新函数）单元测试。
零网络、零数据库：K 线用合成夹具，DB 用假 engine，外部取数全部 monkeypatch。
"""
import json
import re
import math
import os
import sys

import numpy as np
import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
import data  # noqa: E402
import db  # noqa: E402
import market_env as me  # noqa: E402
import notify_swing as ns  # noqa: E402
import swing_scan as ss  # noqa: E402


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def trend_bars(n=220, drift=0.004, half_range=0.025, p0=50.0, vol=3e6, start="2025-01-02"):
    """确定性上升趋势：收盘每天 +drift，高低点 ±half_range（ADR≈5%），量恒定。"""
    idx = pd.bdate_range(start, periods=n)
    close = p0 * (1 + drift) ** np.arange(n)
    return pd.DataFrame({"open": close / (1 + drift), "high": close * (1 + half_range),
                         "low": close * (1 - half_range), "close": close,
                         "volume": np.full(n, vol)}, index=idx)


def append_bar(df, o, h, lo, c, v=None):
    nxt = df.index[-1] + pd.offsets.BDay(1)
    row = pd.DataFrame({"open": [o], "high": [h], "low": [lo], "close": [c],
                        "volume": [df["volume"].iloc[-1] if v is None else v]}, index=[nxt])
    return pd.concat([df, row])


def ema9_next(df, close):
    """给定下一根收盘，算出下一根的 EMA9（adjust=False 递推）。"""
    prev = df["close"].ewm(span=9, adjust=False, min_periods=9).mean().iloc[-1]
    a = 2 / 10
    return a * close + (1 - a) * prev


def sim_frame(rows, start="2025-03-03"):
    """纸面状态机用：rows = [(open, high, low, close, ema9), ...]，第 0 行是信号日。"""
    idx = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "ema9"], index=idx)


class FakeConn:
    def __init__(self, log):
        self.log = log

    def execute(self, stmt, params=None):
        self.log.append(str(stmt))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeEngine:
    def __init__(self):
        self.log = []

    def begin(self):
        return FakeConn(self.log)

    def connect(self):
        return FakeConn(self.log)


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
class TestIndicators:
    def test_ema_matches_recursion_and_warmup(self):
        s = pd.Series(np.arange(1, 31, dtype=float))
        e = ss.ema(s, 9)
        assert e.iloc[:8].isna().all() and not math.isnan(e.iloc[8])
        a, v = 0.2, s.iloc[0]
        for x in s.iloc[1:]:
            v = a * x + (1 - a) * v
        assert e.iloc[-1] == pytest.approx(v)

    def test_ema_constant(self):
        assert ss.ema(pd.Series([7.0] * 40), 21).iloc[-1] == pytest.approx(7.0)

    def test_adr_and_dollar_volume(self):
        ind = ss.compute_indicators(trend_bars())
        assert ind["adr"].iloc[-1] == pytest.approx(1.025 / 0.975 - 1, rel=1e-6)
        last20 = (ind["close"] * ind["volume"]).tail(20).mean()
        assert ind["dvol"].iloc[-1] == pytest.approx(last20)
        assert ind["adr"].iloc[:19].isna().all()

    def test_tiers(self):
        up = ss.compute_indicators(trend_bars())
        dn = ss.compute_indicators(trend_bars(drift=-0.004, p0=200))
        assert up["tier"].iloc[-1] == "strong"
        assert dn["tier"].iloc[-1] == "weak"
        assert up["tier"].iloc[10] is None          # EMA150 未预热
        # 先涨后急跌到 EMA50 下方但 EMA50 仍在 EMA150 上 → 中档
        b = trend_bars(200)
        for _ in range(12):
            c = b["close"].iloc[-1] * 0.97
            b = append_bar(b, c, c * 1.01, c * 0.99, c)
        ind = ss.compute_indicators(b)
        r = ind.iloc[-1]
        assert r["close"] < r["ema50"] and r["ema50"] > r["ema150"]
        assert r["tier"] == "medium"

    def test_eligibility(self):
        ind = ss.compute_indicators(trend_bars())
        assert bool(ind["eligible"].iloc[-1]) and bool(ind["stack"].iloc[-1])
        cheap = ss.compute_indicators(trend_bars(p0=2.0))
        assert not cheap["eligible"].iloc[-1]
        assert any("价" in x for x in ss.eligibility_fails(cheap.iloc[-1]))
        thin = ss.compute_indicators(trend_bars(vol=1e5))
        assert any("成交额" in x for x in ss.eligibility_fails(thin.iloc[-1]))
        calm = ss.compute_indicators(trend_bars(half_range=0.01))
        assert any("ADR" in x for x in ss.eligibility_fails(calm.iloc[-1]))


# ---------------------------------------------------------------------------
# 形态识别
# ---------------------------------------------------------------------------
def pullback_bars(narrow=False):
    """上升趋势后一根回踩 9EMA 的 K 线。narrow=True 时振幅小（同时也满足突破 → 优先级测试）。"""
    b = trend_bars(220)
    e_prev = b["close"].ewm(span=9, adjust=False).mean().iloc[-1]
    c = e_prev * 1.01
    e9 = ema9_next(b, c)
    lo = e9 * 1.002
    h = c * (1.004 if narrow else 1.04)       # 宽幅：高点越过前一根高点、振幅 >0.6×ADR
    return append_bar(b, c * 1.002, h, lo, c)


def gainer_then_pullback():
    """t-1 日资格合格且大涨 4%（进涨幅榜），t 日缩量回落、窄幅收在 9EMA 附近。"""
    b = trend_bars(219)
    c1 = b["close"].iloc[-1] * 1.04
    b = append_bar(b, c1 * 0.985, c1 * 1.01, c1 * 0.975, c1)
    e_prev = b["close"].ewm(span=9, adjust=False).mean().iloc[-1]
    c = e_prev * 1.01
    e9 = ema9_next(b, c)
    return append_bar(b, c * 1.002, c * 1.004, e9 * 1.002, c)


class TestSetups:
    def test_gainer_then_pullback_fixture(self):
        ind = ss.compute_indicators(gainer_then_pullback())
        assert ind["chg"].iloc[-2] >= 0.03 and bool(ind["eligible"].iloc[-2])
        st = ss.make_setup("S01", "x", ind)
        assert st is not None and st["ok_stop"]

    def test_pullback_detected(self):
        ind = ss.compute_indicators(pullback_bars(narrow=False))
        found = [k for k, _ in ss.detect_setups(ind)]
        assert found == ["pullback"]
        st = ss.make_setup("TST", "测试", ind)
        assert st["setup"] == "pullback" and st["trigger"] == pytest.approx(ind["high"].iloc[-1], abs=0.01)
        assert st["stop"] == pytest.approx(ind["low"].iloc[-1], abs=0.01)
        assert st["ok_stop"] is False and "不做" in st["note"]          # 振幅 >3% → 止损过宽

    def test_narrow_pullback_prefers_breakout(self):
        ind = ss.compute_indicators(pullback_bars(narrow=True))
        found = [k for k, _ in ss.detect_setups(ind)]
        assert found[0] == "breakout" and "pullback" in found
        st = ss.make_setup("TST", "测试", ind)
        assert st["setup"] == "breakout" and st["ok_stop"] is True
        assert "同时满足回调" in st["note"]
        # 关掉突破（容差 0）→ 同一根 K 线只剩回调，且止损合格
        st2 = ss.make_setup("TST", "测试", ind, c={"bo_ema9_tol": 0.0})
        assert st2["setup"] == "pullback" and st2["ok_stop"] is True

    def test_breakout_inside_day(self):
        b = trend_bars(220)
        for _ in range(5):                          # 横盘 5 天，振幅收窄到 ±1%
            c = b["close"].iloc[-1] * 1.001
            b = append_bar(b, c, c * 1.01, c * 0.99, c)
        p = b.iloc[-1]
        b = append_bar(b, p["close"], p["high"] * 0.998, p["low"] * 1.002, p["close"] * 1.001)
        ind = ss.compute_indicators(b)
        found = ss.detect_setups(ind)
        assert found and found[0][0] == "breakout" and "inside day" in found[0][1]
        st = ss.make_setup("TST", "测试", ind)
        assert st["ok_stop"] is True and st["stop_pct"] < 0.03

    def test_ep(self):
        b = trend_bars(220, vol=2e6)
        pc = b["close"].iloc[-1]
        b2 = append_bar(b, pc * 1.12, pc * 1.15, pc * 1.11, pc * 1.14, v=8e6)
        found = [k for k, _ in ss.detect_setups(ss.compute_indicators(b2))]
        assert found[0] == "ep"
        b3 = append_bar(b, pc * 1.08, pc * 1.15, pc * 1.07, pc * 1.14, v=8e6)   # 跳空不够
        assert "ep" not in [k for k, _ in ss.detect_setups(ss.compute_indicators(b3))]
        b4 = append_bar(b, pc * 1.12, pc * 1.15, pc * 1.11, pc * 1.14, v=4e6)   # 量不够
        assert "ep" not in [k for k, _ in ss.detect_setups(ss.compute_indicators(b4))]

    def test_member_and_liquidity_gates(self):
        b = trend_bars(220, vol=2e6)
        pc = b["close"].iloc[-1]
        ep_ind = ss.compute_indicators(append_bar(b, pc * 1.12, pc * 1.15, pc * 1.11, pc * 1.14, v=8e6))
        assert [k for k, _ in ss.detect_setups(ep_ind, member=False)] == ["ep"]   # 名单外也判 EP
        pb = ss.compute_indicators(pullback_bars(narrow=True))
        assert ss.detect_setups(pb, member=False) == []                          # 名单外不判回调/突破
        thin = pullback_bars(narrow=True)
        thin["volume"] = 1e4                                                    # 流动性不合格
        assert ss.detect_setups(ss.compute_indicators(thin)) == []
        # EP 要求资格：ADR 太小（平静股）跳空也不算
        calm = trend_bars(220, half_range=0.005, vol=2e6)
        pc = calm["close"].iloc[-1]
        calm = append_bar(calm, pc * 1.12, pc * 1.125, pc * 1.118, pc * 1.12, v=8e6)
        assert "ep" not in [k for k, _ in ss.detect_setups(ss.compute_indicators(calm))]

    def test_no_false_positives(self):
        dn = ss.compute_indicators(trend_bars(drift=-0.004, p0=200))
        assert ss.detect_setups(dn) == []
        # 上升趋势里一根远离 9EMA 的宽幅阳线：既不是回调也不是突破
        b = trend_bars(220)
        c = b["close"].iloc[-1] * 1.06
        b = append_bar(b, c * 0.96, c * 1.01, c * 0.95, c)
        assert ss.detect_setups(ss.compute_indicators(b)) == []
        # 数据太短
        assert ss.detect_setups(ss.compute_indicators(trend_bars(3))) == []

    def test_no_lookahead(self):
        """在第 i 根判形态时，之后的 K 线怎么变都不影响结果。"""
        b = pullback_bars(narrow=True)
        base = ss.detect_setups(ss.compute_indicators(b))
        b2 = append_bar(b, 1, 1000, 0.5, 999)
        assert ss.detect_setups(ss.compute_indicators(b2), i=len(b) - 1) == base


# ---------------------------------------------------------------------------
# 止损过滤 + R 仓位 + 打分
# ---------------------------------------------------------------------------
class TestStopAndSize:
    def test_stop_filter(self):
        assert ss.stop_check(100, 97.5, 0.06)[0] is True
        ok, sp, cap = ss.stop_check(100, 96.5, 0.06)
        assert ok is False and sp == pytest.approx(0.035) and cap == pytest.approx(0.03)
        assert ss.stop_check(100, 97.5, 0.04)[0] is False         # ADR/2 = 2% 更严
        assert ss.stop_check(100, 96.0, 0.12, c={"max_stop_pct": 0.05})[0] is True
        assert ss.stop_check(100, 94.0, 0.20, c={"max_stop_pct": 0.08})[0] is False  # hard cap 5%
        assert ss.stop_check(100, 100, 0.06)[0] is False           # 退化 K 线（high==low）
        assert ss.stop_check(100, 99, float("nan"))[0] is False     # 没有 ADR 不做（与回测一致）

    def test_size_basic_and_cap(self):
        s = ss.size_position(100, 98, 10000, 0.005, 0.25)
        assert s["size_shares"] == 25 and s["size_value"] == 2500 and not s["capped"]
        assert s["risk_value"] == pytest.approx(50)
        s = ss.size_position(100, 99, 10000, 0.005, 0.25)
        assert s["size_shares"] == 25 and s["capped"] and s["risk_value"] == pytest.approx(25)
        assert "截断" in s["size_note"]
        assert ss.size_position(100, 98, 10000, 0.005, 0.25, risk_mult=0.5)["size_shares"] == 12

    def test_size_expensive(self):
        assert ss.size_position(900, 880, 10000, 0.005, 0.25)["size_shares"] == 2
        z = ss.size_position(3000, 2900, 10000, 0.005, 0.25)
        assert z["size_shares"] == 0 and "买不起" not in z["size_note"] and "1 股" in z["size_note"]
        assert ss.size_position(100, 101)["size_note"] == "止损无效"

    def test_quality_matches_backtest_formula(self):
        # bt_swing: q = 0.5×rs_rank + 0.3×(1-stop/allowed) + 0.2×clip((ema9/ema50-1)/0.2)
        q = ss.quality_score(0.01, 0.03, 0.8, 110, 100, True)
        assert q == pytest.approx(100 * (0.5 * 0.8 + 0.3 * (1 - 1 / 3) + 0.2 * 0.5), abs=0.05)
        tight = ss.quality_score(0.01, 0.03, 0.5, 105, 100, True)
        loose = ss.quality_score(0.028, 0.03, 0.5, 105, 100, True)
        bad = ss.quality_score(0.01, 0.03, 0.5, 105, 100, False)
        assert tight > loose and bad == pytest.approx(tight * 0.5, abs=0.1)
        assert ss.quality_score(0.01, 0.03, None, 105, 100, True) == tight      # rs 未知按 0.5
        assert 0 <= ss.quality_score(None, None, None, None, None, True) <= 100

    def test_rs_percentile(self):
        pool = list(range(11))                       # 0..10
        assert ss.rs_percentile(10, pool) == 1.0 and ss.rs_percentile(0, pool) == 0.0
        assert ss.rs_percentile(5, pool) == pytest.approx(0.5)
        assert ss.rs_percentile(99, pool) == 1.0 and ss.rs_percentile(float("nan"), pool) is None
        assert ss.rs_percentile(1, [1, 2]) is None


# ---------------------------------------------------------------------------
# 纸面跟踪状态机
# ---------------------------------------------------------------------------
C = config.SWING["cost_pct"]


def r_of(entry, stop, exits):
    return sum(f * (p * (1 - C) - entry * (1 + C)) for p, f in exits) / (entry - stop)


class TestPaperStateMachine:
    def test_pending_without_next_bar(self):
        f = sim_frame([(100, 102, 99, 101, 100)])
        assert ss.simulate_signal(f, f.index[0], 102, 99)["status"] == "pending"

    def test_expired(self):
        f = sim_frame([(100, 102, 99, 101, 100), (101, 101.5, 100, 101, 100.5)])
        assert ss.simulate_signal(f, f.index[0], 102, 99)["status"] == "expired"

    def test_same_bar_conservative_stop(self):
        f = sim_frame([(100, 102, 99, 101, 100), (101, 103, 98.5, 102, 100.5)])
        r = ss.simulate_signal(f, f.index[0], 102, 99)
        assert r["status"] == "closed" and r["entry_price"] == 102 and r["exit_price"] == 99
        assert r["r_multiple"] == pytest.approx(r_of(102, 99, [(99, 1)]), abs=1e-3)
        assert r["r_multiple"] < -1

    def test_gap_up_fill_at_open_and_skip(self):
        f = sim_frame([(100, 102, 99, 101, 100), (102.5, 104, 102, 103.5, 101)])
        r = ss.simulate_signal(f, f.index[0], 102, 99)
        assert r["status"] == "open" and r["entry_price"] == 102.5
        f2 = sim_frame([(100, 102, 99, 101, 100), (104.5, 106, 104, 105, 101)])  # 离止损 5.3% > 4.5%
        assert ss.simulate_signal(f2, f2.index[0], 102, 99)["status"] == "skipped"

    def test_close_below_ema9_exits(self):
        f = sim_frame([(100, 102, 99, 101, 100), (101, 103, 101, 102.5, 101),
                       (102.5, 103, 100.2, 100.5, 101.2)])
        r = ss.simulate_signal(f, f.index[0], 102, 99)
        assert r["status"] == "closed" and r["exit_price"] == 100.5 and "9EMA" in r["note"]
        assert r["r_multiple"] == pytest.approx(r_of(102, 99, [(100.5, 1)]), abs=1e-3)

    def test_breakeven_after_three_days(self):
        """与 bt_swing 一致：成交日为第 0 天，(i - entry_i) >= 3 的那天收盘才上移，次日生效。"""
        rows = [(100, 102, 99, 101, 100),
                (101, 103, 101, 102.8, 101),      # 第 0 天（成交日）
                (103, 104, 102.5, 103.8, 101.8),  # 第 1 天
                (104, 105, 103.5, 104.8, 102.4),  # 第 2 天：EMA9 已 > 102，但还没满 3 天
                (104, 105.5, 103.6, 105.2, 103.0),  # 第 3 天收盘：止损上移到 102（次日生效）
                (104, 104.5, 101.5, 104.2, 103.5)]  # 盘中跌到 101.5 ≤ 102 → 保本出
        f = sim_frame(rows)
        r = ss.simulate_signal(f, f.index[0], 102, 99)
        assert r["status"] == "closed" and r["exit_price"] == 102 and r["note"] == "保本止损"
        assert -0.1 < r["r_multiple"] < 0      # 只亏成本
        r2 = ss.simulate_signal(f.iloc[:4], f.index[0], 102, 99)        # 到第 2 天：还没上移
        assert r2["status"] == "open" and r2["stop_now"] == 99
        r3 = ss.simulate_signal(f.iloc[:5], f.index[0], 102, 99)        # 第 3 天收盘：已上移
        assert r3["status"] == "open" and r3["stop_now"] == 102 and r3["note"].startswith("已保本")
        # 第 2 天就跌到 101.5：止损仍是 99，不出
        early = rows[:3] + [(104, 104.5, 101.5, 104.2, 102.4)]
        assert ss.simulate_signal(sim_frame(early), f.index[0], 102, 99)["status"] == "open"

    def test_breakeven_needs_ema9_above_entry(self):
        rows = [(100, 102, 99, 101, 100), (101, 103, 101, 102.8, 101),
                (103, 104, 102.5, 103.8, 101.5), (104, 105, 103.5, 104.8, 101.7),
                (104, 105, 103.5, 104.9, 101.9)]
        r = ss.simulate_signal(sim_frame(rows), pd.Timestamp("2025-03-03"), 102, 99)
        assert r["status"] == "open" and r["stop_now"] == 99     # EMA9 101.9 < 成交价 102

    def test_gap_down_below_stop(self):
        rows = [(100, 102, 99, 101, 100), (101, 103, 101, 102.8, 101),
                (97, 98, 96, 97.5, 100)]                           # 开盘 97 < 止损 99
        r = ss.simulate_signal(sim_frame(rows), pd.Timestamp("2025-03-03"), 102, 99)
        assert r["status"] == "closed" and r["exit_price"] == 97 and "跳空" in r["note"]
        assert r["r_multiple"] < -1.5

    def test_partial_then_trail(self):
        rows = [(100, 102, 99, 101, 100), (101, 103, 101, 102.8, 101),
                (110, 125, 109, 124, 103),        # 收盘 ≥ EMA9×1.2 → 卖 15%
                (124, 126, 118, 120, 104),        # 不再重复减仓
                (115, 116, 108, 108.5, 109)]      # 收盘跌破 EMA9 → 剩余全卖
        r = ss.simulate_signal(sim_frame(rows), pd.Timestamp("2025-03-03"), 102, 99)
        assert r["status"] == "closed" and r["partial_done"] is True
        assert [e["frac"] for e in r["exits"]] == [0.15, 0.85]
        assert r["r_multiple"] == pytest.approx(r_of(102, 99, [(124, 0.15), (108.5, 0.85)]), abs=1e-3)
        open_r = ss.simulate_signal(sim_frame(rows[:4]), pd.Timestamp("2025-03-03"), 102, 99)
        assert open_r["status"] == "open" and open_r["shares_frac"] == pytest.approx(0.85)

    def test_replay_is_idempotent(self):
        rows = [(100, 102, 99, 101, 100), (101, 103, 101, 102.8, 101), (103, 104, 102.5, 103.8, 101.8)]
        f = sim_frame(rows)
        assert ss.simulate_signal(f, f.index[0], 102, 99) == ss.simulate_signal(f, f.index[0], 102, 99)

    def test_summarize_paper(self):
        df = pd.DataFrame([
            {"id": 1, "signal_date": "2025-03-03", "ts_code": "A", "name": "A", "setup": "pullback",
             "status": "closed", "r_multiple": 2.0, "exit_date": "2025-03-10"},
            {"id": 2, "signal_date": "2025-03-04", "ts_code": "B", "name": "B", "setup": "breakout",
             "status": "closed", "r_multiple": -1.0, "exit_date": "2025-03-05"},
            {"id": 3, "signal_date": "2025-03-05", "ts_code": "C", "name": "C", "setup": "ep",
             "status": "closed", "r_multiple": -1.0, "exit_date": "2025-03-06"},
            {"id": 4, "signal_date": "2025-03-06", "ts_code": "D", "name": "D", "setup": "pullback",
             "status": "closed", "r_multiple": 5.0, "exit_date": "2025-03-20"},
            {"id": 5, "signal_date": "2025-03-21", "ts_code": "E", "name": "E", "setup": "pullback",
             "status": "open", "r_multiple": 0.4},
            {"id": 6, "signal_date": "2025-03-22", "ts_code": "F", "name": "F", "setup": "pullback",
             "status": "expired"},
        ])
        s = ss.summarize_paper(df)
        assert s["n_closed"] == 4 and s["win_rate"] == 0.5
        assert s["avg_win_r"] == 3.5 and s["avg_loss_r"] == -1.0
        assert s["expectancy_r"] == 1.25 and s["total_r"] == 5.0
        assert [o["ticker"] for o in s["open"]] == ["E"] and s["n_expired"] == 1
        assert s["recent"][0]["ticker"] == "D"
        empty = ss.summarize_paper(pd.DataFrame())
        assert empty["n_closed"] == 0 and empty["open"] == []


# ---------------------------------------------------------------------------
# 观察名单
# ---------------------------------------------------------------------------
def pool_df(n=30):
    rows = []
    for i in range(n):
        rows.append({"ts_code": f"S{i:02d}", "em_code": f"S{i:02d}", "market": 105, "name": f"股票{i}",
                     "price": 50.0, "pct_chg": 0.10 - i * 0.004, "dollar_vol": 2e8, "mcap": 5e9,
                     "chg_60d": 0.5 - i * 0.01})
    return pd.DataFrame(rows)


class TestWatchlist:
    def test_prelim_pool_filters(self):
        df = pool_df(5)
        extra = pd.DataFrame([
            {"ts_code": "ETF1", "market": 107, "name": "某某ETF", "price": 50, "pct_chg": 0.2,
             "dollar_vol": 1e9, "mcap": 5e9, "chg_60d": 0.1},
            {"ts_code": "PENY", "market": 105, "name": "便宜股", "price": 3, "pct_chg": 0.2,
             "dollar_vol": 1e9, "mcap": 5e9, "chg_60d": 0.1},
            {"ts_code": "TINY", "market": 105, "name": "小盘", "price": 30, "pct_chg": 0.2,
             "dollar_vol": 1e9, "mcap": 5e8, "chg_60d": 0.1},
            {"ts_code": "QQQ", "market": 105, "name": "纳指", "price": 500, "pct_chg": 0.01,
             "dollar_vol": 1e10, "mcap": 5e11, "chg_60d": 0.1}])
        p = ss.prelim_pool(pd.concat([df, extra], ignore_index=True))
        assert set(p["ts_code"]) == set(df["ts_code"])

    def test_select_candidates_order_cap_limit(self):
        pool = pool_df(30)       # 涨幅 ≥3% 的有 18 只（10% → 3.2%）
        prev = {"OLD1": {"ts_code": "OLD1", "name": "老1", "market": 106, "added_date": "2025-03-01"},
                "S00": {"ts_code": "S00", "name": "股票0", "market": 105, "added_date": "2025-02-20"}}
        cands = ss.select_candidates(pool, prev, ["POS1"], {"watch_cap": 10})
        assert cands[0]["ts_code"] == "POS1" and cands[0]["position"]
        srcs = [c["source"] for c in cands if c["source"]]
        assert len(srcs) == 10 and srcs.count("carry") == 0      # 10 个名额全被涨幅榜占满
        s00 = next(c for c in cands if c["ts_code"] == "S00")
        assert s00["source"] == "gainer" and s00["added_date"] == "2025-02-20"
        # 冷启动 + limit：一半涨幅榜一半 60 日强势
        cold = ss.select_candidates(pool, {}, [], limit=6)
        assert [c["source"] for c in cold] == ["gainer"] * 3 + ["rs"] * 3
        no_rs = pool.assign(chg_60d=float("nan"))                  # Nasdaq 兜底源：没有 60 日涨幅
        assert [c["source"] for c in ss.select_candidates(no_rs, {}, [], limit=6)] == ["gainer"] * 6

    def test_gainer_verified_by_bars_and_stale_list(self):
        b = trend_bars(220)
        c0 = b["close"].iloc[-1] * 1.05                                 # t-1：大涨 5%
        b = append_bar(b, c0 * 0.99, c0 * 1.02, c0 * 0.97, c0)
        b = append_bar(b, c0, c0 * 1.02, c0 * 0.98, c0 * 1.001)         # t：几乎没动
        ind = ss.compute_indicators(b)
        asof = ind.index[-1].strftime("%Y-%m-%d")
        active, removed = ss.finalize_watchlist([{"ts_code": "STALE", "source": "gainer"}], {"STALE": ind}, asof)
        assert active and active[0]["added_date"] == ind.index[-2].strftime("%Y-%m-%d")
        assert active[0]["days_in"] == 2                                # 按回测口径 t-1 已入选
        flat = ss.compute_indicators(trend_bars(220))                   # 每天只涨 0.4%：不算涨幅榜
        active, removed = ss.finalize_watchlist([{"ts_code": "FLAT", "source": "gainer"}], {"FLAT": flat},
                                                flat.index[-1].strftime("%Y-%m-%d"))
        assert not active and "涨幅不足" in removed[0]["why"]

    def test_ep_gap_candidates(self):
        pool = pool_df(30)
        pool["prev_close"] = 50.0
        pool["open"] = 50.5
        pool.loc[pool["ts_code"] == "S25", "open"] = 56.0          # 跳空 12%，但涨幅不在前列
        cands = ss.select_candidates(pool, {}, [])
        s25 = next(c for c in cands if c["ts_code"] == "S25")
        assert s25["ep_check"] and s25["source"] is None
        srcs = [c["source"] or ("ep" if c["ep_check"] else None) for c in cands]
        assert srcs.index("ep") > max(i for i, x in enumerate(srcs) if x == "gainer")
        active, _ = ss.finalize_watchlist(cands, {}, "2025-03-03")
        assert "S25" not in {a["ticker"] for a in active}            # 只判 EP，不进名单

    def test_finalize_rules(self):
        up = ss.compute_indicators(trend_bars(220))
        asof = up.index[-1].strftime("%Y-%m-%d")
        old_added = up.index[-25].strftime("%Y-%m-%d")       # 已入选 25 个交易日
        recent_added = up.index[-5].strftime("%Y-%m-%d")
        dn_raw = trend_bars(200)
        for _ in range(15):
            c = dn_raw["close"].iloc[-1] * 0.97
            dn_raw = append_bar(dn_raw, c, c * 1.03, c * 0.97, c)
        dn_raw.index = up.index[-len(dn_raw):]
        dn = ss.compute_indicators(dn_raw)
        cands = [
            {"ts_code": "NEW", "name": "新", "source": "gainer", "added_date": None},
            {"ts_code": "EXP", "name": "满期", "source": "carry", "added_date": old_added},
            {"ts_code": "KEEP", "name": "留", "source": "carry", "added_date": recent_added},
            {"ts_code": "BRK", "name": "破50", "source": "carry", "added_date": recent_added},
            {"ts_code": "BAD", "name": "不合格", "source": "gainer", "added_date": None},
            {"ts_code": "NODATA", "name": "无数据", "source": "carry", "added_date": recent_added},
        ]
        nb = trend_bars(219)
        cg = nb["close"].iloc[-1] * 1.04
        nb = append_bar(nb, cg * 0.99, cg * 1.02, cg * 0.97, cg)
        nb.index = up.index[-len(nb):]
        frames = {"NEW": ss.compute_indicators(nb), "EXP": up, "KEEP": up, "BRK": dn,
                  "BAD": ss.compute_indicators(trend_bars(220, half_range=0.01)), "NODATA": None}
        active, removed = ss.finalize_watchlist(cands, frames, asof)
        a = {x["ticker"]: x for x in active}
        rm = {x["ticker"]: x["why"] for x in removed}
        assert a["NEW"]["added_date"] == asof and a["NEW"]["days_in"] == 1
        assert a["KEEP"]["days_in"] == 5 and "NODATA" in a
        assert "满" in rm["EXP"] and "EMA50" in rm["BRK"] and "资格不符" in rm["BAD"]


# ---------------------------------------------------------------------------
# 交易环境
# ---------------------------------------------------------------------------
def idx(above9=True, above21=True, r9=True, r21=True, ext=False, days=5):
    return {"close": 100, "ema9": 99, "ema21": 97, "above9": above9, "above21": above21,
            "rising9": r9, "rising21": r21, "ext_pct": 0.03, "days_above21": days,
            "ext_pctl": 0.05, "in_window": above9 and above21 and r9 and r21,
            "extended": ext, "ext_a": ext, "ext_b": False, "asof": "2025-03-03"}


BR_OK = {"strong": 12, "medium": 5, "weak": 3, "total": 20, "strong_pct": 0.6,
         "strong_pct_prev": 0.62, "source": "观察名单 20只"}


class TestMarketEnv:
    def test_index_state_window(self):
        up = me.index_state(trend_bars(260, drift=0.002))
        assert up["in_window"] and up["above21"] and up["days_above21"] > 100
        dn = me.index_state(trend_bars(260, drift=-0.002, p0=300))
        assert not dn["above21"] and not dn["in_window"]
        assert me.index_state(trend_bars(10)) is None

    def test_extension_detected(self):
        b = trend_bars(230, drift=0.0003)
        rng = np.random.default_rng(0)
        b["close"] = b["close"] * (1 + rng.normal(0, 0.01, len(b)))
        for _ in range(30):                          # 最后连涨 30 天、加速
            c = b["close"].iloc[-1] * 1.012
            b = append_bar(b, c, c * 1.005, c * 0.995, c)
        st = me.index_state(b)
        assert st["extended"] and st["days_above21"] >= 15

    def test_breadth_from_frames(self):
        frames = {"U1": trend_bars(220), "U2": trend_bars(220, p0=30), "D1": trend_bars(220, drift=-0.004, p0=200),
                  "SHORT": trend_bars(40)}
        br = me.breadth_from_frames(frames, source="测试")
        assert br["strong"] == 2 and br["weak"] == 1 and br["total"] == 3
        assert br["strong_pct"] == pytest.approx(2 / 3, abs=1e-3) and br["strong_pct_prev"] == pytest.approx(2 / 3, abs=1e-3)
        assert me.breadth_from_frames({"U1": trend_bars(220)}, source="x") is None

    def test_decide_branches(self):
        good = {"QQQ": idx(), "IWM": idx()}
        assert me.decide(good, BR_OK, None)[0] == "go"
        assert me.decide({"QQQ": idx(above9=False, above21=False), "IWM": idx(above9=False, above21=False)},
                         BR_OK, None)[0] == "stop"
        assert me.decide(good, BR_OK, {"pause": True, "consecutive_losses": 5, "note": "连亏5"})[0] == "stop"
        assert me.decide(good, {**BR_OK, "strong_pct": 0.15}, None)[0] == "stop"
        v, reasons, _ = me.decide({"QQQ": idx(), "IWM": idx(above9=False)}, BR_OK, None)
        assert v == "selective" and any("只有 QQQ" in r for r in reasons)
        v, reasons, _ = me.decide({"QQQ": idx(r21=False), "IWM": idx(above9=False)}, BR_OK, None)
        assert v == "selective" and any("都不在" in r for r in reasons)
        v, _, ext = me.decide({"QQQ": idx(ext=True, days=20), "IWM": idx()}, BR_OK, None)
        assert v == "selective" and ext
        assert me.decide(good, {**BR_OK, "strong_pct": 0.5, "strong_pct_prev": 0.62}, None)[0] == "selective"
        assert me.decide(good, BR_OK, {"pause": False, "consecutive_losses": 3})[0] == "selective"
        assert me.decide(good, None, None)[0] == "go"                    # 宽度缺失不参与

    def test_assess_contract(self):
        frames = {f"W{i}": trend_bars(220, p0=20 + i) for i in range(12)}
        env = me.assess(index_frames={"QQQ": trend_bars(260, drift=0.002), "IWM": trend_bars(260, drift=0.002)},
                        watch_frames=frames, own=None, use_db=False)
        for k in ("verdict", "emoji", "label", "reasons", "index", "extended", "breadth", "own", "asof"):
            assert k in env
        for k in ("close", "ema9", "ema21", "above9", "above21", "rising9", "rising21", "ext_pct", "days_above21"):
            assert k in env["index"]["QQQ"]
        for k in ("strong", "medium", "weak", "total", "strong_pct", "strong_pct_prev", "source"):
            assert k in env["breadth"]
        assert env["verdict"] in ("go", "selective") and env["own"] is None
        assert me.assess(index_frames={"QQQ": None, "IWM": None}, own=None, use_db=False) is None

    def test_assess_fetches_indices_itself(self, monkeypatch):
        seen = []

        def fake_get(sym, market=None, use_db=True, cfg=None):
            seen.append((sym, market, use_db))
            return trend_bars(260, drift=-0.003, p0=300), "td_full"
        monkeypatch.setattr(data, "get_ohlcv", fake_get)
        env = me.assess(own={"pause": False, "consecutive_losses": 0}, use_db=False)
        assert [x[0] for x in seen] == ["QQQ", "IWM"] and seen[0][1] == 105 and seen[1][1] == 107
        assert env["verdict"] == "stop" and "21EMA 下方" in env["reasons"][0]
        monkeypatch.setattr(data, "get_ohlcv", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        assert me.assess(own=None, use_db=False) is None               # 取数异常 → None，不抛

    def test_own_lazy_import_missing(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "journal", None)       # import journal → ImportError
        assert me._own_status() is None


# ---------------------------------------------------------------------------
# 卡片
# ---------------------------------------------------------------------------
def fake_result(env=True, setups=True, paper=None):
    st = {"ticker": "MU", "name": "美光科技", "setup": "pullback", "setup_cn": "回调", "close": 100.0,
          "trigger": 101.5, "stop": 99.0, "stop_pct": 0.0246, "adr_pct": 0.055, "dollar_vol_m": 900.0,
          "tier": "strong", "ema9": 99.5, "ema21": 96, "ema50": 90, "asof": "2025-03-03",
          "size_shares": 20, "size_value": 2030.0, "size_pct": 0.203, "risk_value": 50.0,
          "ok_stop": True, "quality": 71.2, "note": "低点回踩9EMA"}
    wide = {**st, "ticker": "TSLA", "name": "特斯拉", "ok_stop": False, "quality": 30.0}
    e = {"verdict": "go", "emoji": "🟢", "label": "适合出手", "reasons": ["QQQ、IWM 都站上向上的 9EMA 和 21EMA"],
         "index": {"QQQ": idx(), "IWM": idx()}, "extended": False, "breadth": BR_OK,
         "own": {"consecutive_losses": 1, "risk_mult": 1.0, "pause": False, "note": ""}, "asof": "2025-03-03"}
    return {"asof": "2025-03-03", "env": e if env else None, "setups": [st, wide] if setups else [],
            "watchlist": [{"ticker": "MU", "source": "carry"}, {"ticker": "NVDA", "source": "gainer"}],
            "paper": paper, "universe_size": 300, "credits_used": 14, "errors": []}


def card_text(card):
    out = []
    for el in card["elements"]:
        if el["tag"] == "div":
            out.append(el["text"]["content"])
        elif el["tag"] == "note":
            out.append(el["elements"][0]["content"])
    return "\n".join(out)


class TestCard:
    def test_full_card(self):
        card = ns.build_card(fake_result())
        assert card["header"]["template"] == "green"
        assert card["header"]["title"]["content"] == "📡 短线观察雷达 · 2025-03-03"
        t = card_text(card)
        assert "交易环境" in t and "三看" in t and "适合出手" in t and "QQQ" in t and "宽度" in t and "连亏 1 笔" in t
        assert "美光科技 MU" in t and "触发 >101.50" in t and "止损 99.00(-2.5%)" in t and "质量" in t
        # 雷达模式（回测无优势）：绝不给建议股数 / 仓位 / 风险金额
        assert not re.search(r"建议\s*\d+\s*股", t) and "风险 $" not in t and " 股 ≈ " not in t
        assert "特斯拉" not in t and "止损过宽" in t
        assert "不是买入信号" in t and "刚开始" in t and "不讨论实盘" in t and "非投资建议" in t
        assert config.SWING["backtest_note"] in t
        json.dumps(card, ensure_ascii=False)

    def test_header_colors(self):
        for v, color in (("selective", "orange"), ("stop", "red")):
            r = fake_result()
            r["env"]["verdict"] = v
            assert ns.build_card(r)["header"]["template"] == color
        assert "休息观望" in card_text(ns.build_card({**fake_result(), "env": {**fake_result()["env"],
                                                                              "verdict": "stop", "label": "休息观望"}}))

    def test_degrade_env_missing_or_broken(self):
        card = ns.build_card(fake_result(env=False))
        assert card["header"]["template"] == "blue" and "交易环境" not in card_text(card)
        broken = fake_result()
        broken["env"] = {"verdict": "go"}                   # 缺字段 → 环境块跳过，不崩
        card = ns.build_card(broken)
        assert "交易环境" not in card_text(card) and "美光科技" in card_text(card)

    def test_no_setups_and_paper(self):
        paper = {"n_closed": 4, "win_rate": 0.25, "avg_win_r": 6.0, "avg_loss_r": -1.0,
                 "expectancy_r": 0.75, "total_r": 3.0, "open": [{"ticker": "A"}], "recent": []}
        t = card_text(ns.build_card(fake_result(setups=False, paper=paper)))
        assert "没有止损合格的形态" in t and "已平 4 笔" in t and "胜率 25%" in t and "+0.75R" in t
        assert "不讨论实盘" in t          # 实盘门槛：满 100 笔且期望 R 显著 >0

    def test_fail_card_and_main_degrade(self, monkeypatch, tmp_path):
        fc = ns.build_fail_card("boom", "2025-03-03")
        assert "今日扫描失败" in card_text(fc) and fc["header"]["template"] == "grey"

        def boom(**kw):
            raise RuntimeError("东财挂了")
        monkeypatch.setattr(ns.swing_scan, "run", boom)
        out = tmp_path / "card.json"
        res = ns.main(["--dry-run", "--out", str(out)])
        saved = json.loads(out.read_text())
        assert "今日扫描失败" in card_text(saved["card"]) and res["result"] is None


    def test_main_sends_when_not_dry_run(self, monkeypatch):
        import notify_lark
        monkeypatch.setattr(ns.swing_scan, "run", lambda **kw: fake_result())
        sent = []
        monkeypatch.setattr(notify_lark, "send_lark_card", lambda card: sent.append(card) or '{"code":0}')
        out = ns.main([])
        assert len(sent) == 1 and sent[0]["header"]["template"] == "green" and out["result"]["asof"]
        monkeypatch.setattr(notify_lark, "send_lark_card", lambda card: (_ for _ in ()).throw(OSError("net")))
        ns.main([])                                                     # 发送失败只打印，不抛


# ---------------------------------------------------------------------------
# 端到端 dry-run（证明完全不碰数据库、不碰网络）
# ---------------------------------------------------------------------------
class TestRunDryRun:
    def test_dry_run_no_db(self, monkeypatch):
        def no_db(*a, **k):
            raise AssertionError("dry-run 不许碰数据库")
        for fn in ("read_sql", "get_engine", "upsert_df", "ensure_swing_schema"):
            monkeypatch.setattr(db, fn, no_db)
        univ = pool_df(8)
        monkeypatch.setattr(data, "us_liquid_list", lambda **kw: univ)
        idx_bars = trend_bars(260, drift=0.002)
        stock = {f"S{i:02d}": trend_bars(220, p0=20 + i) for i in range(8)}
        stock["S01"] = gainer_then_pullback()
        calls = []

        def fake_get(sym, market=None, use_db=True, cfg=None):
            assert use_db is False
            calls.append(sym)
            if sym in ("QQQ", "IWM"):
                b = idx_bars.copy()
            else:
                b = stock[sym].copy()
            b.index = pd.bdate_range(end=stock["S01"].index[-1], periods=len(b))
            return b, "td_full"
        monkeypatch.setattr(data, "get_ohlcv", fake_get)
        res = ss.run(dry_run=True, limit=6)
        assert len([c for c in calls if c not in ("QQQ", "IWM")]) == 6
        for k in ("asof", "env", "setups", "watchlist", "paper", "universe_size", "credits_used"):
            assert k in res
        assert res["paper"] is None and res["env"]["own"] is None
        assert any(s["ticker"] == "S01" and s["ok_stop"] for s in res["setups"])
        s01 = next(s for s in res["setups"] if s["ticker"] == "S01")
        for k in ("ticker", "name", "setup", "setup_cn", "close", "trigger", "stop", "stop_pct", "adr_pct",
                  "dollar_vol_m", "tier", "ema9", "ema21", "ema50", "asof", "size_shares", "size_value",
                  "size_pct", "risk_value", "ok_stop", "quality", "note"):
            assert k in s01
        json.dumps(res, ensure_ascii=False)
        card = ns.build_card(res)
        assert "S01" in card_text(card)


    def test_production_path_with_fake_db(self, monkeypatch):
        """use_db=True：名单/信号/快照都走（假的）写库函数；在途信号被推进；持仓股不重复记新信号。"""
        univ = pool_df(6)
        monkeypatch.setattr(data, "us_liquid_list", lambda **kw: univ)
        monkeypatch.setattr(db, "ensure_swing_schema", lambda **k: True)
        end = pd.Timestamp("2026-03-02")
        stock = {f"S{i:02d}": trend_bars(220, p0=20 + i) for i in range(6)}
        stock["S01"] = gainer_then_pullback()
        stock["S02"] = pullback_bars(narrow=True)
        for k in stock:
            stock[k].index = pd.bdate_range(end=end, periods=len(stock[k]))
        idx_bars = trend_bars(260, drift=0.002)
        idx_bars.index = pd.bdate_range(end=end, periods=260)
        monkeypatch.setattr(data, "get_ohlcv", lambda sym, market=None, use_db=True, cfg=None:
                            ((idx_bars if sym in ("QQQ", "IWM") else stock[sym]).copy(), "td_incr"))
        s3 = stock["S03"]
        sig_day = s3.index[-3]
        monkeypatch.setattr(ss, "_load_watchlist", lambda: {
            "S02": {"ts_code": "S02", "name": "股票2", "market": 105, "added_date": "2026-02-20", "source": "carry"}})
        monkeypatch.setattr(ss, "_load_open_signals", lambda: [
            {"id": 7, "signal_date": sig_day.date(), "ts_code": "S03", "trigger": float(s3["high"].iloc[-3]),
             "stop": float(s3["low"].iloc[-3]) * 0.9},
            {"id": 8, "signal_date": stock["S02"].index[-4].date(), "ts_code": "S02",
             "trigger": float(stock["S02"]["high"].iloc[-4]), "stop": float(stock["S02"]["low"].iloc[-4]) * 0.9},
            {"id": 9, "signal_date": end.date(), "ts_code": "S01", "trigger": 1.0, "stop": 0.5}])  # 同日重跑
        log = {"upd": [], "ins": [], "wl": None, "snap": None}
        monkeypatch.setattr(ss, "_update_signal", lambda i, r: log["upd"].append((i, r["status"])))
        monkeypatch.setattr(ss, "_insert_signals", lambda rows, v: log["ins"].extend(r["ticker"] for r in rows))
        monkeypatch.setattr(ss, "_save_watchlist", lambda a, r, p, asof: log.update(wl=[x["ticker"] for x in a]))
        monkeypatch.setattr(ss, "_save_snapshot", lambda p: log.update(snap=p))
        monkeypatch.setattr(ss, "paper_stats", lambda: ss.summarize_paper(pd.DataFrame()))
        monkeypatch.setattr(me, "breadth_from_etfs", lambda loader=None: None)   # 名单 <10：ETF 退路也不连库
        monkeypatch.setattr(me, "_own_status", lambda: {"consecutive_losses": 3, "recent_r": [-1, -1, -1],
                                                          "risk_mult": 0.5, "pause": False, "note": "连亏3"})
        res = ss.run(dry_run=False)
        upd = dict(log["upd"])
        assert upd[7] == "open" and upd[8] == "open" and upd[9] == "pending"
        assert "S01" in log["ins"] and "S02" not in log["ins"]          # S02 有在途持仓，不重复记
        notes = {x["ticker"]: x["note"] for x in res["setups"]}
        assert "已有纸面持仓" in notes["S02"] and "已有纸面持仓" not in notes["S01"]   # 同日重跑不误标
        assert "S02" in log["wl"] and log["snap"]["asof"] == "2026-03-02"
        assert res["env"]["verdict"] == "selective" and res["env"]["own"]["risk_mult"] == 0.5
        s01 = next(x for x in res["setups"] if x["ticker"] == "S01")
        budget = config.SWING["account_size"] * config.SWING["risk_pct"] * 0.5
        assert 0 < s01["risk_value"] <= budget + 1e-6 and "风险系数×0.5" in s01["note"]


# ---------------------------------------------------------------------------
# DB 迁移 + data 新函数
# ---------------------------------------------------------------------------
class TestDbMigration:
    def _cols(self, names):
        return pd.DataFrame({"COL": names})          # MySQL 可能返回大写列名

    def test_ov_migration_idempotent(self, monkeypatch):
        eng = FakeEngine()
        monkeypatch.setattr(db, "get_engine", lambda: eng)
        monkeypatch.setattr(db, "read_sql", lambda *a, **k: self._cols(["ts_code", "trade_date", "close",
                                                                         "high", "low"]))
        alters = db._migrate_recent_price_ov()
        assert len(alters) == 2 and len(eng.log) == 1
        assert "ADD COLUMN `open`" in eng.log[0] and "ADD COLUMN volume" in eng.log[0]
        eng.log.clear()
        monkeypatch.setattr(db, "read_sql", lambda *a, **k: self._cols(
            ["ts_code", "trade_date", "close", "high", "low", "open", "volume"]))
        assert db._migrate_recent_price_ov() == [] and eng.log == []

    def test_ensure_swing_schema(self, monkeypatch):
        eng = FakeEngine()
        monkeypatch.setattr(db, "get_engine", lambda: eng)
        monkeypatch.setattr(db, "_SWING_SCHEMA_OK", False)
        monkeypatch.setattr(db, "read_sql", lambda *a, **k: self._cols(
            ["ts_code", "trade_date", "close", "high", "low", "open", "volume"]))
        assert db.ensure_swing_schema() is True
        sql = "\n".join(eng.log)
        for t in ("swing_watchlist", "swing_signals", "swing_snapshot"):
            assert f"CREATE TABLE IF NOT EXISTS {t}" in sql
        assert "`trigger`" in sql and "uq_swing_sig (signal_date, ts_code, setup)" in sql
        assert "ALTER" not in sql
        n = len(eng.log)
        db.ensure_swing_schema()                       # 同进程第二次：不再执行
        assert len(eng.log) == n
        db.ensure_swing_schema(force=True)             # 强制再跑：仍只有 CREATE IF NOT EXISTS
        assert "ALTER" not in "\n".join(eng.log)

    def test_init_tables_survives_swing_failure(self, monkeypatch):
        eng = FakeEngine()
        monkeypatch.setattr(db, "get_engine", lambda: eng)
        monkeypatch.setattr(db, "read_sql", lambda *a, **k: self._cols(["high", "low"]))

        def fail(**k):
            raise RuntimeError("no JSON support")
        monkeypatch.setattr(db, "ensure_swing_schema", fail)
        db.init_tables()                               # 不抛


class TestSnapshotRead:
    def test_latest_snapshot_and_paper_stats(self, monkeypatch):
        payload = ss.jsonable({"asof": pd.Timestamp("2026-03-02"), "env": None,
                               "setups": [{"q": np.float64(1.5), "n": np.int64(3), "x": float("nan"),
                                           "ok": np.bool_(True)}]})
        assert payload["asof"] == "2026-03-02" and payload["setups"][0] == {"q": 1.5, "n": 3, "x": None, "ok": True}
        monkeypatch.setattr(db, "read_sql", lambda sql, params=None: pd.DataFrame(
            [{"asof": "2026-03-02", "payload": json.dumps(payload)}]))
        snap = ss.latest_snapshot()
        assert snap["asof"] == "2026-03-02" and snap["watchlist"] == [] and snap["paper"] is None
        monkeypatch.setattr(db, "read_sql", lambda sql, params=None: pd.DataFrame())
        assert ss.latest_snapshot() is None
        assert ss.paper_stats()["n_closed"] == 0

        def boom(*a, **k):
            raise RuntimeError("table doesn't exist")
        monkeypatch.setattr(db, "read_sql", boom)
        assert ss.latest_snapshot() is None and ss.paper_stats() is None


class TestDataLayer:
    def test_save_recent_ohlcv_column_set(self, monkeypatch):
        got = {}
        order = []
        monkeypatch.setattr(db, "ensure_swing_schema", lambda **k: order.append("migrate") or True)
        monkeypatch.setattr(db, "upsert_df", lambda df, table, key_cols: order.append("write") or got.update(
            cols=list(df.columns), table=table, keys=key_cols))
        df = pd.DataFrame({"ts_code": ["X"], "trade_date": ["2025-03-03"], "open": [1.0], "high": [2.0],
                           "low": [0.5], "close": [1.5], "volume": [100.0], "extra": [9]})
        data.save_recent_ohlcv(df)
        assert got["table"] == "recent_price" and got["keys"] == ["ts_code", "trade_date"]
        assert got["cols"] == ["ts_code", "trade_date", "open", "high", "low", "close", "volume"]
        assert order == ["migrate", "write"]                  # 先迁移（加 open/volume 列）再写

    def test_nasdaq_fallback_parsing(self, monkeypatch):
        payload = {"data": {"rows": [
            {"symbol": "AAA", "name": "Alpha Inc. Common Stock", "lastsale": "$20.00", "netchange": "1.00",
             "pctchange": "5.263%", "volume": "5000000", "marketCap": "2000000000.00"},
            {"symbol": "BRK/B", "name": "Berkshire Class B", "lastsale": "$500.00", "netchange": "-1",
             "pctchange": "-0.2%", "volume": "100000", "marketCap": "1000000000000"},
            {"symbol": "XYZ^A", "name": "pref", "lastsale": "$25", "netchange": "0", "pctchange": "0%",
             "volume": "1", "marketCap": "1"}]}}

        class R:
            def read(self):
                return json.dumps(payload).encode()
        monkeypatch.setattr(data._urlreq, "urlopen", lambda *a, **k: R())
        df = data.us_list_nasdaq()
        assert list(df["ts_code"]) == ["AAA", "BRK.B"]                 # 按成交额倒序；优先股剔除
        a = df.set_index("ts_code").loc["AAA"]
        assert a["name"] == "Alpha Inc." and a["pct_chg"] == pytest.approx(0.05263)
        assert a["dollar_vol"] == pytest.approx(1e8) and a["prev_close"] == pytest.approx(19.0)
        assert math.isnan(a["chg_60d"]) and a["market"] is None
        assert len(ss.prelim_pool(df)) == 2                            # 没有市场号也不被剔除

    def test_em_kline_parsing_and_backoff(self, monkeypatch):
        payload = {"data": {"klines": ["2026-03-02,10.0,10.5,10.8,9.9,123456,1.3e6",
                                       "2026-03-03,10.5,11.0,11.2,10.4,223456,2.4e6"]}}
        tries = {"n": 0}

        class R:
            def read(self):
                return json.dumps(payload).encode()

        def urlopen(req, timeout=20):
            tries["n"] += 1
            if tries["n"] == 1:
                raise ConnectionResetError("RemoteDisconnected")
            assert "fqt=0" in req.full_url and "secid=105.BRK_B" in req.full_url
            return R()
        monkeypatch.setattr(data._urlreq, "urlopen", urlopen)
        monkeypatch.setattr(data.time, "sleep", lambda s: None)
        df = data.fetch_ohlcv_em("BRK.B", 105, days=2, pause_s=0)
        assert tries["n"] == 2 and list(df.columns) == data.OHLCV_COLS
        r = df.iloc[-1]
        assert (r["open"], r["close"], r["high"], r["low"], r["volume"]) == (10.5, 11.0, 11.2, 10.4, 223456)
        monkeypatch.setattr(data._urlreq, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
        assert data.fetch_ohlcv_em("X", None, days=2, pause_s=0, retries=2).empty     # 失败返回空，不抛

    def test_symbol_mapping(self):
        assert data.em_to_td_symbol("BRK_B") == "BRK.B"
        assert data.em_to_td_symbol("ABCD_U") is None and data.em_to_td_symbol("NVDA") == "NVDA"
        assert data.td_to_em_code("BRK.B") == "BRK_B"

    def test_td_paced_429_and_budget(self, monkeypatch):
        data.td_reset_counter()
        seq = [RuntimeError("429 You have run out of API credits for the current minute"),
               pd.DataFrame({"x": [1]})]

        def fake(symbol, n):
            v = seq.pop(0)
            if isinstance(v, Exception):
                raise v
            return v
        monkeypatch.setattr(data, "fetch_ohlcv_td", fake)
        monkeypatch.setattr(data.time, "sleep", lambda s: None)
        assert not data.td_fetch_paced("A", 10, pause_s=0, wait_429_s=0).empty
        assert data.td_credits_used() == 2
        with pytest.raises(data.TDQuotaError):
            data.td_fetch_paced("A", 10, pause_s=0, budget=2)
        monkeypatch.setattr(data, "fetch_ohlcv_td", lambda s, n: (_ for _ in ()).throw(
            RuntimeError("429 You have run out of API credits for the day")))
        data.td_reset_counter()
        with pytest.raises(data.TDQuotaError):
            data.td_fetch_paced("A", 10, pause_s=0)
        with pytest.raises(data.TDQuotaError):          # 当日额度用尽后不再调
            data.td_fetch_paced("B", 10, pause_s=0)
        data.td_reset_counter()

    def test_get_ohlcv_dry_run_and_fallback(self, monkeypatch):
        monkeypatch.setattr(data, "load_recent_ohlcv", lambda s: (_ for _ in ()).throw(AssertionError("读库")))
        monkeypatch.setattr(data, "save_recent_ohlcv", lambda df: (_ for _ in ()).throw(AssertionError("写库")))
        bars = trend_bars(30).reset_index().rename(columns={"index": "trade_date"})
        bars["ts_code"] = "X"
        monkeypatch.setattr(data, "td_fetch_paced", lambda *a, **k: bars.copy())
        monkeypatch.setattr(data, "drop_partial_bar", lambda df: df)
        df, src = data.get_ohlcv("X", use_db=False)
        assert src == "td_full" and len(df) == 30 and list(df.columns) == ["open", "high", "low", "close", "volume"]

        def td_fail(*a, **k):
            raise RuntimeError("down")
        monkeypatch.setattr(data, "td_fetch_paced", td_fail)
        monkeypatch.setattr(data, "fetch_ohlcv_em", lambda *a, **k: bars.copy())
        assert data.get_ohlcv("X", use_db=False)[1] == "em"
        monkeypatch.setattr(data, "fetch_ohlcv_em", lambda *a, **k: pd.DataFrame(columns=data.OHLCV_COLS))
        df, src = data.get_ohlcv("X", use_db=False)
        assert src == "none" and df.empty

    def test_get_ohlcv_incremental_merge(self, monkeypatch):
        cache = trend_bars(200, start="2026-01-02")
        monkeypatch.setattr(data, "load_recent_ohlcv", lambda s: cache.copy())
        saved = {}
        monkeypatch.setattr(data, "save_recent_ohlcv", lambda df: saved.update(n=len(df)))
        monkeypatch.setattr(data, "_et_now", lambda: cache.index[-1].to_pydatetime() + pd.Timedelta(days=2))
        tail = trend_bars(215, start="2026-01-02").iloc[-15:].reset_index().rename(columns={"index": "trade_date"})
        tail["ts_code"] = "X"
        asked = {}

        def td(sym, n, **k):
            asked["n"] = n
            return tail.copy()
        monkeypatch.setattr(data, "td_fetch_paced", td)
        monkeypatch.setattr(data, "drop_partial_bar", lambda df: df)
        df, src = data.get_ohlcv("X", use_db=True)
        assert src == "td_incr" and asked["n"] == config.SWING["hist_incr"] and saved["n"] == 15
        assert len(df) == 215 and df.index.is_monotonic_increasing
