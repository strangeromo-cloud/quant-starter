"""
短线模块集成审查补的测试：core（swing_scan / market_env / data）与 web（webapp/app.py）之间的
接口、隐私边界与生产降级。零网络、零数据库（DB 全部 monkeypatch / 假 engine）。
"""
import json
import os
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
import data  # noqa: E402
import db  # noqa: E402
import market_env as me  # noqa: E402
import swing_scan as ss  # noqa: E402
from test_swing_core import (BR_OK, gainer_then_pullback, pool_df, pullback_bars,  # noqa: E402
                             trend_bars)

OWN_LOSING = {"consecutive_losses": 5, "recent_r": [-1.0, -1.0, -1.0, -1.0, -1.0], "risk_mult": 0.5,
              "pause": True, "note": "连亏 5 笔：暂停开新仓"}


# ---------------------------------------------------------------------------
# data：东财日K熔断 / 东财数据与未解决的拆股不写共享缓存 / 拆股后作废旧口径
# ---------------------------------------------------------------------------
class TestEmBreaker:
    def test_breaker_trips_on_unreachable_only(self, monkeypatch):
        calls = {"n": 0}

        def down(*a, **k):
            calls["n"] += 1
            raise OSError("RemoteDisconnected")
        monkeypatch.setattr(data._urlreq, "urlopen", down)
        monkeypatch.setattr(data.time, "sleep", lambda s: None)
        data.em_reset_breaker()
        assert data.fetch_ohlcv_em("A", 105, pause_s=0, retries=2).empty
        assert data.fetch_ohlcv_em("B", 105, pause_s=0, retries=2).empty
        n = calls["n"]
        assert data.fetch_ohlcv_em("C", 105, pause_s=0, retries=2).empty
        assert calls["n"] == n                                   # 熔断后不再发请求

        class R:
            def read(self):
                return json.dumps({"data": None}).encode()       # 连得上、只是没这只票
        data.em_reset_breaker()
        monkeypatch.setattr(data._urlreq, "urlopen", lambda *a, **k: R())
        for sym in ("X", "Y", "Z"):
            assert data.fetch_ohlcv_em(sym, None, pause_s=0).empty
        assert data._EM_STATE["fails"] == 0                      # "没数据"不算连不上
        data.em_reset_breaker()

    def test_unknown_market_retries_less(self, monkeypatch):
        calls = {"n": 0}

        def down(*a, **k):
            calls["n"] += 1
            raise OSError("down")
        monkeypatch.setattr(data._urlreq, "urlopen", down)
        monkeypatch.setattr(data.time, "sleep", lambda s: None)
        data.em_reset_breaker()
        data.fetch_ohlcv_em("A", None, pause_s=0, retries=4)
        assert calls["n"] == 3 * 2                               # 3 个市场号 × 2 次
        data.em_reset_breaker()


def _as_rows(bars, sym="X"):
    out = bars.reset_index().rename(columns={"index": "trade_date"})
    out["ts_code"] = sym
    return out


class TestOhlcvCacheWrites:
    def test_em_fallback_is_not_written(self, monkeypatch):
        monkeypatch.setattr(data, "load_recent_ohlcv", lambda s: pd.DataFrame(
            columns=["open", "high", "low", "close", "volume"]))
        monkeypatch.setattr(data, "save_recent_ohlcv", lambda df: (_ for _ in ()).throw(
            AssertionError("东财原始价不许写进 recent_price")))
        monkeypatch.setattr(data, "td_fetch_paced", lambda *a, **k: (_ for _ in ()).throw(
            data.TDQuotaError("budget")))
        monkeypatch.setattr(data, "fetch_ohlcv_em", lambda *a, **k: _as_rows(trend_bars(60)))
        monkeypatch.setattr(data, "drop_partial_bar", lambda df: df)
        df, src = data.get_ohlcv("X", use_db=True)
        assert src == "em" and len(df) == 60

    def _split_setup(self, monkeypatch, refetch_ok=True):
        cache = trend_bars(200, start="2026-01-02")              # 拆股前口径（2 倍价）
        cache[["open", "high", "low", "close"]] *= 2
        monkeypatch.setattr(data, "load_recent_ohlcv", lambda s: cache.copy())
        monkeypatch.setattr(data, "_et_now", lambda: cache.index[-1].to_pydatetime() + pd.Timedelta(days=2))
        monkeypatch.setattr(data, "drop_partial_bar", lambda df: df)
        new_all = trend_bars(205, start="2026-01-02")             # TD 已复权口径；增量与缓存重叠 10 天
        incr, full = _as_rows(new_all.iloc[-15:]), _as_rows(new_all.iloc[-150:])

        def td(sym, n, **k):
            if n == config.SWING["hist_incr"]:
                return incr.copy()
            if not refetch_ok:
                raise data.TDQuotaError("budget")
            return full.copy()
        monkeypatch.setattr(data, "td_fetch_paced", td)
        log = {"saved": None, "forgot": None}
        monkeypatch.setattr(data, "save_recent_ohlcv", lambda df: log.update(saved=len(df)))
        monkeypatch.setattr(data, "_forget_old_scale", lambda s, before: log.update(forgot=pd.Timestamp(before)))
        return new_all, log

    def test_split_refetch_drops_old_scale_rows(self, monkeypatch):
        new_all, log = self._split_setup(monkeypatch, refetch_ok=True)
        df, src = data.get_ohlcv("X", use_db=True)
        assert src == "td_full" and log["saved"] == 150
        assert log["forgot"] == new_all.index[-150]
        assert len(df) == 150 and df.index.min() == new_all.index[-150]   # 旧口径行没拼进来
        assert (df["close"] / new_all["close"].iloc[-150:] - 1).abs().max() < 1e-9

    def test_split_refetch_failure_is_not_written(self, monkeypatch):
        _, log = self._split_setup(monkeypatch, refetch_ok=False)
        df, src = data.get_ohlcv("X", use_db=True)
        assert log["saved"] is None and log["forgot"] is None             # 混口径不落库，下次再重取
        assert len(df) == 15

    def test_merge_skips_rows_without_full_ohlcv(self, monkeypatch):
        cache = trend_bars(230, start="2025-12-01")
        cache.loc[cache.index[:30], ["open", "volume"]] = float("nan")   # 现有模块只写 close/high/low 的行
        monkeypatch.setattr(data, "load_recent_ohlcv", lambda s: cache.copy())
        monkeypatch.setattr(data, "_et_now", lambda: cache.index[-1].to_pydatetime() + pd.Timedelta(days=1))
        monkeypatch.setattr(data, "drop_partial_bar", lambda df: df)
        monkeypatch.setattr(data, "save_recent_ohlcv", lambda df: None)
        monkeypatch.setattr(data, "td_fetch_paced", lambda sym, n, **k: _as_rows(cache.iloc[-15:]))
        df, src = data.get_ohlcv("X", use_db=True)
        assert src == "td_incr" and len(df) == 200 and df["volume"].notna().all()


# ---------------------------------------------------------------------------
# swing_scan：EP 候选上限 / 库不可用时降级
# ---------------------------------------------------------------------------
def test_ep_gap_candidates_capped():
    pool = pool_df(40)
    pool["pct_chg"] = 0.12
    pool["prev_close"] = 40.0
    pool["open"] = 45.0                                        # 全部跳空 12.5%
    cands = ss.select_candidates(pool, {}, [], dict(config.SWING, watch_topk=5), None)
    ep_only = [v for v in cands if v["ep_check"] and not v["source"]]
    assert len(ep_only) == config.SWING["ep_cand_max"]
    assert all(v["ep_check"] for v in cands if v["source"] == "gainer")  # 名单股跳空只打标记、不占名额


def test_run_degrades_when_schema_migration_fails(monkeypatch):
    def boom(**k):
        raise RuntimeError("Access denied for user 'quant'@'10.0.0.5' to ALTER")
    monkeypatch.setattr(db, "ensure_swing_schema", boom)
    for fn in ("read_sql", "get_engine", "upsert_df"):
        monkeypatch.setattr(db, fn, lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该再碰库")))
    monkeypatch.setattr(data, "us_liquid_list", lambda **kw: pool_df(4))
    end = pd.Timestamp("2026-03-02")

    def fake_get(sym, market=None, use_db=True, cfg=None):
        assert use_db is False
        b = trend_bars(260, drift=0.002) if sym in ("QQQ", "IWM") else gainer_then_pullback()
        b.index = pd.bdate_range(end=end, periods=len(b))
        return b, "td_full"
    monkeypatch.setattr(data, "get_ohlcv", fake_get)
    res = ss.run(dry_run=False)
    assert res["asof"] == "2026-03-02" and res["paper"] is None
    assert any("数据库不可用" in e for e in res["errors"])
    assert "Access denied" not in json.dumps(res, ensure_ascii=False)   # 异常原文只进日志


# ---------------------------------------------------------------------------
# market_env：公开结论 / 指数行情过期
# ---------------------------------------------------------------------------
def _frames_up(end="2026-03-02"):
    b = trend_bars(260, drift=0.002)
    b.index = pd.bdate_range(end=end, periods=len(b))
    return b


class TestMarketEnvPublic:
    def test_public_verdict_excludes_own(self):
        f = _frames_up()
        watch = {f"W{i}": trend_bars(220, p0=20 + i) for i in range(12)}
        env = me.assess(index_frames={"QQQ": f, "IWM": f}, watch_frames=watch, own=OWN_LOSING,
                        use_db=False, asof="2026-03-02")
        assert env["verdict"] == "stop" and me.is_own_reason(env["reasons"][0])
        pub = env["public"]
        assert pub["verdict"] in ("go", "selective") and not any(me.is_own_reason(r) for r in pub["reasons"])
        assert me.assess(index_frames={"QQQ": f, "IWM": f}, own=None, use_db=False)["public"] is None

    def test_stale_index_is_flagged(self):
        fresh = _frames_up("2026-03-02")
        stale = _frames_up("2026-02-26")
        env = me.assess(index_frames={"QQQ": stale, "IWM": fresh}, watch_frames=None, own=None,
                        use_db=False, asof="2026-03-02")
        assert env["verdict"] != "go"
        assert any("QQQ 行情停在 2026-02-26" in r for r in env["reasons"])
        ok = me.assess(index_frames={"QQQ": fresh, "IWM": fresh}, own=None, use_db=False, asof="2026-03-02")
        assert not any("行情停在" in r for r in ok["reasons"])

    def test_own_reason_prefixes_match_decide(self):
        good = {"QQQ": {"in_window": True, "above9": True, "above21": True, "rising9": True,
                        "rising21": True, "extended": False},
                "IWM": {"in_window": True, "above9": True, "above21": True, "rising9": True,
                        "rising21": True, "extended": False}}
        for own in (OWN_LOSING, {"pause": False, "consecutive_losses": 3}):
            _, reasons, _ = me.decide(good, BR_OK, own)
            assert me.is_own_reason(reasons[0])


# ---------------------------------------------------------------------------
# 公开快照：core 写进库的东西 → web 公开端点，整条链路不泄露个人交易日志
# ---------------------------------------------------------------------------
def _run_production(monkeypatch, own):
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
    monkeypatch.setattr(ss, "_load_watchlist", lambda: {
        "S02": {"ts_code": "S02", "name": "股票2", "market": 105, "added_date": "2026-02-20", "source": "carry"}})
    monkeypatch.setattr(ss, "_load_open_signals", lambda: [])
    log = {"ins": [], "verdict": None, "snap": None}

    def ins(rows, v):
        log["ins"].extend(rows)
        log["verdict"] = v
    monkeypatch.setattr(ss, "_insert_signals", ins)
    monkeypatch.setattr(ss, "_update_signal", lambda i, r: None)
    monkeypatch.setattr(ss, "_save_watchlist", lambda a, r, p, asof: None)
    monkeypatch.setattr(ss, "_save_snapshot", lambda p: log.update(snap=json.loads(json.dumps(p))))
    monkeypatch.setattr(ss, "paper_stats", lambda: ss.summarize_paper(pd.DataFrame()))
    monkeypatch.setattr(me, "breadth_from_etfs", lambda loader=None: None)
    monkeypatch.setattr(me, "_own_status", lambda: own)
    return ss.run(dry_run=False), log


def test_public_snapshot_strips_personal_journal(monkeypatch):
    monkeypatch.setitem(config.SWING, "max_position_pct", 1.0)    # 不让单仓上限截断，股数差异才看得出来
    res, log = _run_production(monkeypatch, OWN_LOSING)
    # 私有结果（给 Lark 卡片）：按纪律降风险 + 自己连亏的理由
    assert res["env"]["verdict"] == "stop" and res["env"]["own"]["risk_mult"] == 0.5
    priv = {s["ticker"]: s for s in res["setups"]}
    assert "风险系数×0.5" in priv["S01"]["note"]
    # 公开快照：结论/理由/股数都不受个人日志影响
    snap = log["snap"]
    text = json.dumps(snap, ensure_ascii=False)
    assert snap["env"]["own"] is None and "public" not in snap["env"]
    assert snap["env"]["verdict"] == res["env"]["public"]["verdict"] != "stop"
    for bad in ("连亏", "自己", "风险系数", "recent_r"):
        assert bad not in text, bad
    pub = {s["ticker"]: s for s in snap["setups"]}
    assert set(pub) == set(priv)
    for t in pub:                     # 同一形态、同一触发/止损；股数按标准风险（risk_mult=1）重算
        assert pub[t]["trigger"] == priv[t]["trigger"] and pub[t]["stop"] == priv[t]["stop"]
        assert pub[t]["size_shares"] >= priv[t]["size_shares"]
    t = "S01"
    std = ss.size_position(priv[t]["trigger"], priv[t]["stop"], risk_mult=1.0)["size_shares"]
    half = ss.size_position(priv[t]["trigger"], priv[t]["stop"], risk_mult=0.5)["size_shares"]
    assert abs(pub[t]["size_shares"] - std) <= 1 and abs(priv[t]["size_shares"] - half) <= 1
    assert pub[t]["risk_value"] > 1.8 * priv[t]["risk_value"]
    # 纸面信号（公开的 /api/swing/paper 会显示 note）：用公开结论、不带风险系数
    assert log["ins"] and log["verdict"] == res["env"]["public"]["verdict"]
    assert all("风险系数" not in r["note"] and "连亏" not in r["note"] for r in log["ins"])

    # web 公开端点读到的正是这份快照
    tc = pytest.importorskip("fastapi.testclient")
    import webapp.app as webapp_app
    monkeypatch.setattr(db, "read_sql", lambda sql, params=None: pd.DataFrame(
        [{"asof": snap["asof"], "payload": json.dumps(snap, ensure_ascii=False)}]))
    client = tc.TestClient(webapp_app.app)
    d = client.get("/api/swing/snapshot").json()["data"]
    assert d["asof"] == "2026-03-02" and d["env"]["verdict"] == snap["env"]["verdict"]
    assert {s["ticker"] for s in d["setups"]} == set(pub)
    for k in ("ticker", "setup", "setup_cn", "trigger", "stop", "stop_pct", "size_shares", "ok_stop", "quality"):
        assert k in d["setups"][0]
    for k in ("ticker", "source", "added_date", "days_in", "tier"):
        assert k in d["watchlist"][0]


def test_no_journal_means_identical_public_and_private(monkeypatch):
    res, log = _run_production(monkeypatch, None)
    assert res["env"]["public"] is None
    assert log["snap"]["env"]["verdict"] == res["env"]["verdict"]
    assert [s["size_shares"] for s in log["snap"]["setups"]] == [s["size_shares"] for s in res["setups"]]


def test_web_uses_core_public_verdict_when_present(monkeypatch):
    tc = pytest.importorskip("fastapi.testclient")
    import webapp.app as webapp_app
    snap = {"asof": "2026-03-02",
            "env": {"verdict": "stop", "emoji": "🔴", "label": "休息观望",
                    "reasons": ["自己的单子：连亏 5 笔，先停 1-2 天", "QQQ、IWM 都站上向上的 9EMA 和 21EMA"],
                    "own": OWN_LOSING,
                    "public": {"verdict": "go", "emoji": "🟢", "label": "适合出手",
                               "reasons": ["QQQ、IWM 都站上向上的 9EMA 和 21EMA"]}},
            "setups": [{"ticker": "NVDA", "note": "低点回踩9EMA；风险系数×0.5（按纪律降风险）"}],
            "watchlist": []}
    monkeypatch.setattr(db, "read_sql", lambda sql, params=None: pd.DataFrame(
        [{"asof": "2026-03-02", "payload": json.dumps(snap, ensure_ascii=False)}]))
    monkeypatch.setitem(sys.modules, "swing_scan", ss)
    r = tc.TestClient(webapp_app.app).get("/api/swing/snapshot")
    d = r.json()["data"]
    assert d["env"]["verdict"] == "go" and d["env"]["label"] == "适合出手"
    assert d["env"]["reasons"] == ["QQQ、IWM 都站上向上的 9EMA 和 21EMA"] and "public" not in d["env"]
    assert d["setups"][0]["note"] == "低点回踩9EMA"
    for bad in ("连亏", "风险系数", "recent_r"):
        assert bad not in r.text


def test_card_never_shows_sizing_in_radar_mode():
    """回测无优势（docs/SWING_RESEARCH.md）→ 卡片只列形态，任何情况下都不给建议股数/仓位/风险金额。"""
    import notify_swing as ns
    from test_swing_core import fake_result
    res = fake_result()
    for rm in (1.0, 0.5):
        res["env"]["own"]["risk_mult"] = rm
        txt = ns.setups_block(res, res["env"])["text"]["content"]
        assert "仅观察，不是买入信号" in txt
        assert "建议" not in txt and "每笔风险" not in txt and "风险 $" not in txt and "账户 $" not in txt


def test_card_says_env_unassessed_when_missing():
    import notify_swing as ns
    from test_swing_core import card_text, fake_result
    t = card_text(ns.build_card(fake_result(env=False)))
    assert "环境未评估" in t and "美光科技" in t


def test_public_payload_edge_cases():
    assert ss.public_payload({"asof": "2026-03-02", "env": None, "setups": []})["env"] is None
    p = ss.public_payload({"asof": "2026-03-02", "env": {"verdict": "selective", "own": OWN_LOSING,
                                                         "reasons": ["自己连亏 5 笔，降风险", "IWM跌破21EMA"]},
                           "setups": [{"ticker": "A"}]})
    assert p["env"]["own"] is None and p["env"]["reasons"] == ["IWM跌破21EMA"]   # 没有 public 也去掉私有理由
