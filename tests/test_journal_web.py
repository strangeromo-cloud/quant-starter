"""
交易日志（journal.py）+ 网页「⚡ 短线」端点（webapp/app.py）测试。

不连 MySQL：纯函数直接测；SQL CRUD 用 SQLite 内存库替身（monkeypatch db.get_engine）；
FastAPI 端点用 TestClient + monkeypatch（journal 库函数换内存实现、swing_scan 换假模块）。
运行：pytest tests/test_journal_web.py -q
"""
import math
import os
import sys
import types
from datetime import date, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
import db  # noqa: E402
import journal  # noqa: E402

TOKEN = "unit-test-token-0123456789abcdef"


def _t(i, entry, stop, exit=None, exit_date=None, shares=100, fees=0.0,
       entry_date="2025-01-02", code="NVDA"):
    return {"id": i, "ts_code": code, "entry_date": entry_date, "entry_price": entry,
            "stop_price": stop, "shares": shares, "fees": fees,
            "exit_price": exit, "exit_date": exit_date}


def _losses(n, start=date(2025, 3, 3), r_exit=98.0):
    """n 笔各 -1R 的亏损（入场 100 / 止损 98 / 卖 98），平仓日逐日递增。"""
    return [_t(100 + k, 100, 98, r_exit, (start + timedelta(days=k)).isoformat()) for k in range(n)]


# ===========================================================================
# r_multiple
# ===========================================================================
class TestRMultiple:
    def test_basic_win(self):
        assert journal.r_multiple(100, 98, 106, 0, 10) == pytest.approx(3.0)

    def test_fees_reduce_r(self):
        # 净盈亏 60-2=58，风险 2×10=20 → 2.9R
        assert journal.r_multiple(100, 98, 106, 2, 10) == pytest.approx(2.9)

    def test_full_stop_is_minus_one(self):
        assert journal.r_multiple(50, 48.5, 48.5, 0, 200) == pytest.approx(-1.0)

    def test_gap_below_stop_exceeds_one_r(self):
        assert journal.r_multiple(100, 98, 95, 0, 10) == pytest.approx(-2.5)

    def test_without_shares_is_per_share_and_ignores_fees(self):
        assert journal.r_multiple(100, 98, 104, 5, None) == pytest.approx(2.0)
        assert journal.r_multiple(100, 98, 104, 5, 0) == pytest.approx(2.0)

    def test_string_numbers_ok(self):
        assert journal.r_multiple("100", "98", "101", "0", "10") == pytest.approx(0.5)

    @pytest.mark.parametrize("args", [
        (100, 100, 105, 0, 10),       # 止损 = 入场
        (100, 101, 105, 0, 10),       # 止损高于入场
        (0, -1, 5, 0, 10),            # 入场非正
        ("x", 98, 105, 0, 10),        # 非数字
        (float("nan"), 98, 105, 0, 10),
        (100, 98, None, 0, 10),       # 未平仓
        (True, 98, 105, 0, 10),       # bool 不当数字
    ])
    def test_invalid_returns_none(self, args):
        assert journal.r_multiple(*args) is None


# ===========================================================================
# compute_stats
# ===========================================================================
class TestComputeStats:
    def _sample(self):
        # 故意打乱输入顺序：统计必须按平仓日排序
        return [
            _t(3, 100, 98, 98, "2025-01-20"),                   # -1R
            _t(1, 100, 98, 110, "2025-01-10"),                  # +5R
            _t(2, 100, 98, 99, "2025-01-15"),                   # -0.5R
            _t(4, 100, 98, 100.05, "2025-01-25"),               # +0.025R → 保本
            _t(5, 100, 98, 104, "2025-01-30"),                  # +2R
            _t(6, 100, 98, None, None),                         # 进行中
            _t(7, 100, 98, None, None),                         # 进行中
        ]

    def test_counts_and_averages(self):
        st = journal.compute_stats(self._sample())
        assert st["n_closed"] == 5 and st["n_open"] == 2
        assert (st["n_win"], st["n_loss"], st["n_scratch"]) == (2, 2, 1)
        assert st["win_rate"] == pytest.approx(0.4)
        assert st["avg_win_r"] == pytest.approx(3.5)
        assert st["avg_loss_r"] == pytest.approx(-0.75)
        assert st["payoff"] == pytest.approx(round(3.5 / 0.75, 2))
        total = 5 - 0.5 - 1 + 0.025 + 2
        assert st["total_r"] == pytest.approx(total, abs=1e-3)
        assert st["expectancy_r"] == pytest.approx(total / 5, abs=1e-3)

    def test_equity_curve_sorted_by_exit_date(self):
        curve = journal.compute_stats(self._sample())["equity_curve_r"]
        assert [c["date"] for c in curve] == ["2025-01-10", "2025-01-15", "2025-01-20",
                                              "2025-01-25", "2025-01-30"]
        assert curve[0]["cum_r"] == pytest.approx(5.0)
        assert curve[-1]["cum_r"] == pytest.approx(5.525, abs=1e-3)

    def test_drawdown_and_streaks(self):
        st = journal.compute_stats(self._sample())
        assert st["max_drawdown_r"] == pytest.approx(1.5)        # 5 → 3.5
        assert st["max_consec_losses"] == 2                      # -0.5, -1 连着
        assert st["consecutive_losses"] == 0                     # 最近一笔 +2R

    def test_scratch_neither_breaks_nor_counts_loss_run(self):
        trades = [_t(1, 100, 98, 98, "2025-02-01"), _t(2, 100, 98, 100, "2025-02-02"),
                  _t(3, 100, 98, 98, "2025-02-03")]
        st = journal.compute_stats(trades)
        assert st["consecutive_losses"] == 2
        assert st["max_consec_losses"] == 2
        assert st["n_scratch"] == 1

    def test_empty(self):
        st = journal.compute_stats([])
        assert st["n_closed"] == 0 and st["n_open"] == 0
        assert st["win_rate"] is None and st["expectancy_r"] is None
        assert st["avg_win_r"] is None and st["avg_loss_r"] is None and st["payoff"] is None
        assert st["total_r"] == 0 and st["equity_curve_r"] == []
        assert st["consecutive_losses"] == 0

    def test_only_open(self):
        st = journal.compute_stats([_t(1, 100, 98)])
        assert st["n_closed"] == 0 and st["n_open"] == 1

    def test_total_pnl_includes_fees(self):
        st = journal.compute_stats([_t(1, 100, 98, 106, "2025-01-10", shares=10, fees=2)])
        assert st["total_pnl"] == pytest.approx(58.0)


# ===========================================================================
# streak_status_from
# ===========================================================================
class TestStreak:
    TODAY = date(2025, 3, 20)   # 周四

    def test_no_trades(self):
        s = journal.streak_status_from([], today=self.TODAY)
        assert s["consecutive_losses"] == 0 and s["risk_mult"] == 1.0 and s["pause"] is False
        assert s["level"] == "none" and s["recent_r"] == [] and s["note"]

    def test_brief_keys_present(self):
        s = journal.streak_status_from(_losses(1), today=self.TODAY)
        for k in ("consecutive_losses", "recent_r", "risk_mult", "pause", "note"):
            assert k in s
        assert isinstance(s["recent_r"], list) and isinstance(s["note"], str)

    @pytest.mark.parametrize("n", [1, 2])
    def test_one_or_two_losses_normal_risk(self, n):
        s = journal.streak_status_from(_losses(n), today=self.TODAY)
        assert s["consecutive_losses"] == n
        assert s["risk_mult"] == 1.0 and s["pause"] is False and s["level"] == "caution"

    @pytest.mark.parametrize("n", [3, 4])
    def test_three_losses_half_risk(self, n):
        s = journal.streak_status_from(_losses(n), today=self.TODAY)
        assert s["risk_mult"] == 0.5 and s["pause"] is False and s["level"] == "reduce"
        assert "减半" in s["note"] and "0.25%" in s["note"]

    def test_win_after_losses_restores(self):
        trades = _losses(4) + [_t(9, 100, 98, 103, "2025-03-15")]
        s = journal.streak_status_from(trades, today=self.TODAY)
        assert s["consecutive_losses"] == 0 and s["risk_mult"] == 1.0 and s["pause"] is False
        assert "盈利" in s["note"]

    def test_scratch_does_not_restore(self):
        trades = _losses(3) + [_t(9, 100, 98, 100, "2025-03-15")]
        s = journal.streak_status_from(trades, today=self.TODAY)
        assert s["consecutive_losses"] == 3 and s["risk_mult"] == 0.5

    def test_five_losses_recent_pauses(self):
        # 最后一笔亏损周一（3/17）平仓，周四（3/20）恢复；周二/周三都在暂停期
        trades = _losses(5, start=date(2025, 3, 13))     # 3/13..3/17
        for today, paused in [(date(2025, 3, 17), True), (date(2025, 3, 18), True),
                              (date(2025, 3, 19), True), (date(2025, 3, 20), False)]:
            s = journal.streak_status_from(trades, today=today)
            assert s["consecutive_losses"] == 5
            assert s["pause"] is paused, today
            assert s["risk_mult"] == 0.5
            assert s["resume_date"] == "2025-03-20"
        assert journal.streak_status_from(trades, today=date(2025, 3, 18))["level"] == "pause"

    def test_five_losses_old_not_paused_but_half_risk(self):
        s = journal.streak_status_from(_losses(6, start=date(2025, 1, 6)), today=self.TODAY)
        assert s["consecutive_losses"] == 6
        assert s["pause"] is False and s["risk_mult"] == 0.5 and s["level"] == "reduce"

    def test_recent_r_last_ten_chronological(self):
        trades = [_t(i, 100, 98, 100 + i * 0.2, f"2025-02-{i:02d}") for i in range(1, 13)]
        s = journal.streak_status_from(trades, today=self.TODAY)
        assert len(s["recent_r"]) == 10
        assert s["recent_r"][0] == pytest.approx(0.3)      # 第 3 笔 = 0.6/2
        assert s["recent_r"][-1] == pytest.approx(1.2)     # 第 12 笔 = 2.4/2


class TestStreakStatusDb:
    def test_exception_returns_none(self, monkeypatch):
        def boom():
            raise RuntimeError("no db")
        monkeypatch.setattr(journal, "list_trades", boom)
        assert journal.streak_status() is None

    def test_empty_journal_returns_none(self, monkeypatch):
        monkeypatch.setattr(journal, "list_trades", lambda: [])
        assert journal.streak_status() is None

    def test_with_trades(self, monkeypatch):
        monkeypatch.setattr(journal, "list_trades", lambda: _losses(3))
        s = journal.streak_status(today=date(2025, 3, 20))
        assert s["consecutive_losses"] == 3 and s["risk_mult"] == 0.5


# ===========================================================================
# 输入校验
# ===========================================================================
GOOD = {"ts_code": "nvda", "setup": "pullback", "entry_date": "2025-01-02",
        "entry_price": 100, "stop_price": 97.5, "shares": 40, "fees": 1.2, "note": " 测试 "}


class TestValidate:
    def test_good(self):
        c = journal.validate_trade(dict(GOOD), today=date(2025, 6, 1))
        assert c["ts_code"] == "NVDA" and c["side"] == "long" and c["setup"] == "pullback"
        assert c["entry_date"] == date(2025, 1, 2) and c["entry_price"] == 100.0
        assert c["exit_date"] is None and c["exit_price"] is None
        assert c["note"] == "测试" and c["fees"] == pytest.approx(1.2)

    def test_hk_and_class_tickers(self):
        for code in ("00700.HK", "BRK.B", "BF-B"):
            assert journal.validate_trade(dict(GOOD, ts_code=code), today=date(2025, 6, 1))["ts_code"] == code

    def test_numeric_strings_and_empty_optional(self):
        c = journal.validate_trade(dict(GOOD, entry_price="100", stop_price="97.5", fees="",
                                        setup="", note=""), today=date(2025, 6, 1))
        assert c["entry_price"] == 100.0 and c["fees"] == 0.0
        assert c["setup"] is None and c["note"] is None

    @pytest.mark.parametrize("patch,msg", [
        ({"ts_code": ""}, "代码"),
        ({"ts_code": "NV DA;--"}, "代码格式"),
        ({"ts_code": 123}, "代码"),
        ({"side": "short"}, "做多"),
        ({"setup": "cup"}, "形态"),
        ({"entry_price": 0}, "入场价"),
        ({"entry_price": -5}, "入场价"),
        ({"entry_price": "abc"}, "入场价"),
        ({"entry_price": True}, "入场价"),
        ({"entry_price": "nan"}, "入场价"),
        ({"stop_price": 100}, "低于入场价"),
        ({"stop_price": 101}, "低于入场价"),
        ({"stop_price": None}, "止损"),
        ({"shares": 0}, "股数"),
        ({"fees": -1}, "手续费"),
        ({"entry_date": "2025/01/02"}, "YYYY-MM-DD"),
        ({"entry_date": "2025-13-01"}, "YYYY-MM-DD"),
        ({"entry_date": "2099-01-01"}, "晚于今天"),
        ({"entry_date": "1980-01-01"}, "1990"),
        ({"exit_price": 105}, "同时"),
        ({"exit_date": "2025-01-05"}, "同时"),
        ({"exit_date": "2024-12-30", "exit_price": 105}, "早于入场"),
        ({"note": "x" * 501}, "500"),
        ({"note": 5}, "备注"),
    ])
    def test_rejects(self, patch, msg):
        with pytest.raises(ValueError) as ei:
            journal.validate_trade(dict(GOOD, **patch), today=date(2025, 6, 1))
        assert msg in str(ei.value)

    def test_not_a_dict(self):
        with pytest.raises(ValueError):
            journal.validate_trade(["x"])

    def test_partial_update_merges_existing(self):
        existing = journal._row_out(dict(id=1, created_at=None, updated_at=None,
                                         **journal.validate_trade(dict(GOOD), today=date(2025, 6, 1))))
        c = journal.validate_trade({"exit_date": "2025-01-09", "exit_price": 108},
                                   existing=existing, today=date(2025, 6, 1))
        assert c["ts_code"] == "NVDA" and c["stop_price"] == 97.5
        assert c["exit_date"] == date(2025, 1, 9) and c["exit_price"] == 108.0
        # 撤销平仓：两者都清空
        c2 = journal.validate_trade({"exit_date": None, "exit_price": None},
                                    existing=dict(existing, exit_date="2025-01-09", exit_price=108),
                                    today=date(2025, 6, 1))
        assert c2["exit_date"] is None and c2["exit_price"] is None

    def test_row_out_derived_fields(self):
        row = dict(id=3, created_at="2025-01-09 10:00:00", updated_at=None,
                   **journal.validate_trade(dict(GOOD, exit_date="2025-01-09", exit_price=105),
                                            today=date(2025, 6, 1)))
        o = journal._row_out(row)
        assert o["status"] == "closed" and o["hold_days"] == 7
        assert o["stop_pct"] == pytest.approx(0.025) and o["risk_value"] == pytest.approx(100.0)
        assert o["pnl"] == pytest.approx(5 * 40 - 1.2)
        assert o["r"] == pytest.approx((200 - 1.2) / 100, abs=1e-3)
        assert o["setup_cn"] == "回调" and o["entry_date"] == "2025-01-02"


# ===========================================================================
# 访问令牌
# ===========================================================================
class TestToken:
    def test_unconfigured(self, monkeypatch):
        monkeypatch.delenv("JOURNAL_TOKEN", raising=False)
        monkeypatch.setattr(journal, "get_setting", lambda k: None)
        assert journal.expected_token() is None
        assert journal.check_token(TOKEN) == "unconfigured"

    def test_db_error_is_unconfigured(self, monkeypatch):
        monkeypatch.delenv("JOURNAL_TOKEN", raising=False)

        def boom(k):
            raise RuntimeError("db down")
        monkeypatch.setattr(journal, "get_setting", boom)
        assert journal.check_token(TOKEN) == "unconfigured"

    def test_env_has_priority(self, monkeypatch):
        monkeypatch.setenv("JOURNAL_TOKEN", TOKEN)
        monkeypatch.setattr(journal, "get_setting", lambda k: "other-token-from-db-123456")
        assert journal.check_token(TOKEN) == "ok"
        assert journal.check_token("other-token-from-db-123456") == "invalid"

    def test_app_settings_fallback(self, monkeypatch):
        monkeypatch.delenv("JOURNAL_TOKEN", raising=False)
        monkeypatch.setattr(journal, "get_setting", lambda k: TOKEN if k == "journal_token" else None)
        assert journal.check_token(TOKEN) == "ok"
        assert journal.check_token(" " + TOKEN + " ") == "ok"
        assert journal.check_token("wrong") == "invalid"
        assert journal.check_token("") == "invalid"
        assert journal.check_token(None) == "invalid"
        assert journal.check_token("令牌令牌") == "invalid"    # 非 ASCII 不抛异常

    def test_weak(self, monkeypatch):
        monkeypatch.setenv("JOURNAL_TOKEN", "short")
        assert journal.check_token("short") == "weak"

    def test_uses_compare_digest(self):
        import inspect
        assert "hmac.compare_digest" in inspect.getsource(journal.check_token)


# ===========================================================================
# SQL CRUD（SQLite 内存库替身，验证参数化 SQL 与读写往返）
# ===========================================================================
@pytest.fixture
def sqlite_db(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(db, "get_engine", lambda: eng)
    journal.ensure_tables()
    journal.ensure_tables()   # 幂等
    return eng


class TestCrudSqlite:
    def test_roundtrip(self, sqlite_db):
        a = journal.create_trade(dict(GOOD, entry_date="2025-01-02"))
        b = journal.create_trade(dict(GOOD, ts_code="AMD", entry_date="2025-01-06", setup="ep"))
        assert a["id"] and b["id"] and a["id"] != b["id"]
        assert a["status"] == "open" and a["ts_code"] == "NVDA" and a["entry_date"] == "2025-01-02"
        rows = journal.list_trades()
        assert [r["ts_code"] for r in rows] == ["AMD", "NVDA"]       # 入场日期倒序

        closed = journal.update_trade(a["id"], {"exit_date": "2025-01-09", "exit_price": 105})
        assert closed["status"] == "closed" and closed["exit_date"] == "2025-01-09"
        assert closed["r"] == pytest.approx((200 - 1.2) / 100, abs=1e-3)
        assert closed["ts_code"] == "NVDA" and closed["stop_price"] == 97.5

        reopened = journal.update_trade(a["id"], {"exit_date": None, "exit_price": None})
        assert reopened["status"] == "open"

        assert journal.update_trade(99999, {"note": "x"}) is None
        with pytest.raises(ValueError):
            journal.update_trade(a["id"], {"stop_price": 200})
        assert journal.get_trade(a["id"])["stop_price"] == 97.5      # 校验失败不落库

        assert journal.delete_trade(b["id"]) is True
        assert journal.delete_trade(b["id"]) is False
        assert [r["id"] for r in journal.list_trades()] == [a["id"]]

    def test_sql_injection_text_is_data(self, sqlite_db):
        t = journal.create_trade(dict(GOOD, note="'); DROP TABLE trade_journal; --"))
        assert journal.get_trade(t["id"])["note"].startswith("');")
        assert len(journal.list_trades()) == 1

    def test_bad_ids(self, sqlite_db):
        for bad in (0, -1, "abc", None, True):
            with pytest.raises(ValueError):
                journal.get_trade(bad)

    def test_settings_upsert(self, sqlite_db):
        assert journal.get_setting("journal_token") is None
        journal.set_setting("journal_token", "a" * 20)
        journal.set_setting("journal_token", "b" * 20)
        assert journal.get_setting("journal_token") == "b" * 20

    def test_streak_status_reads_db(self, sqlite_db):
        assert journal.streak_status() is None       # 空表
        for k in range(3):
            journal.create_trade(dict(GOOD, entry_date=f"2025-01-0{k + 2}",
                                      exit_date=f"2025-01-0{k + 3}", exit_price=97.5))
        s = journal.streak_status(today=date(2025, 1, 20))
        assert s["consecutive_losses"] == 3 and s["risk_mult"] == 0.5


def test_ensure_tables_failure_is_visible_not_silent(monkeypatch):
    """ensure_tables 本身允许抛（启动钩子负责兜底）；streak_status 绝不抛。"""
    def boom():
        raise RuntimeError("no db")
    monkeypatch.setattr(db, "get_engine", boom)
    with pytest.raises(RuntimeError):
        journal.ensure_tables()
    assert journal.streak_status() is None


# ===========================================================================
# FastAPI 端点
# ===========================================================================
from fastapi.testclient import TestClient  # noqa: E402

import webapp.app as webapp_app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(webapp_app.app)


@pytest.fixture
def token_on(monkeypatch):
    monkeypatch.setenv("JOURNAL_TOKEN", TOKEN)
    return {"X-Journal-Token": TOKEN}


@pytest.fixture
def mem_journal(monkeypatch):
    """把 journal 的库函数换成内存实现（复用真实的校验与派生字段逻辑）。"""
    store = {}
    seq = {"n": 0}

    def list_trades(limit=5000):
        rows = [journal._row_out(dict(r)) for r in store.values()]
        return sorted(rows, key=lambda r: (r["entry_date"], r["id"]), reverse=True)

    def create_trade(payload):
        clean = journal.validate_trade(payload)
        seq["n"] += 1
        store[seq["n"]] = dict(clean, id=seq["n"], created_at="2025-01-01 00:00:00", updated_at=None)
        return journal._row_out(store[seq["n"]])

    def update_trade(trade_id, payload):
        if trade_id not in store:
            return None
        existing = journal._row_out(store[trade_id])
        clean = journal.validate_trade(payload, existing=existing)
        store[trade_id].update(clean)
        return journal._row_out(store[trade_id])

    def delete_trade(trade_id):
        return store.pop(trade_id, None) is not None

    monkeypatch.setattr(journal, "list_trades", list_trades)
    monkeypatch.setattr(journal, "create_trade", create_trade)
    monkeypatch.setattr(journal, "update_trade", update_trade)
    monkeypatch.setattr(journal, "delete_trade", delete_trade)
    return store


class TestJournalAuth:
    def test_unconfigured_503(self, client, monkeypatch):
        monkeypatch.delenv("JOURNAL_TOKEN", raising=False)
        monkeypatch.setattr(journal, "get_setting", lambda k: None)
        for method, path in [("get", "/api/journal"), ("post", "/api/journal"),
                             ("put", "/api/journal/1"), ("delete", "/api/journal/1")]:
            kw = {"json": {}} if method in ("post", "put") else {}
            r = getattr(client, method)(path, headers={"X-Journal-Token": TOKEN}, **kw)
            assert r.status_code == 503, (method, path)
            assert "未配置" in r.json()["detail"]

    def test_weak_token_503(self, client, monkeypatch):
        monkeypatch.setenv("JOURNAL_TOKEN", "123")
        r = client.get("/api/journal", headers={"X-Journal-Token": "123"})
        assert r.status_code == 503 and "太短" in r.json()["detail"]

    def test_missing_or_wrong_token_401(self, client, token_on, mem_journal):
        assert client.get("/api/journal").status_code == 401
        bad = {"X-Journal-Token": "wrong-token-wrong-token"}
        assert client.get("/api/journal", headers=bad).status_code == 401
        assert client.post("/api/journal", json=GOOD, headers=bad).status_code == 401
        assert client.put("/api/journal/1", json={}, headers=bad).status_code == 401
        assert client.delete("/api/journal/1", headers=bad).status_code == 401
        assert mem_journal == {}          # 未授权什么也没写进去

    def test_token_from_app_settings(self, client, monkeypatch, mem_journal):
        monkeypatch.delenv("JOURNAL_TOKEN", raising=False)
        monkeypatch.setattr(journal, "get_setting", lambda k: TOKEN)
        assert client.get("/api/journal", headers={"X-Journal-Token": TOKEN}).status_code == 200


class TestJournalCrud:
    def test_full_flow(self, client, token_on, mem_journal):
        r = client.get("/api/journal", headers=token_on)
        assert r.status_code == 200
        body = r.json()
        assert body["trades"] == [] and body["stats"]["n_closed"] == 0
        assert body["streak"]["level"] == "none"

        r = client.post("/api/journal", json=dict(GOOD, entry_date="2025-01-02"), headers=token_on)
        assert r.status_code == 200, r.text
        tid = r.json()["trade"]["id"]
        assert r.json()["trade"]["status"] == "open"

        r = client.put(f"/api/journal/{tid}", json={"exit_date": "2025-01-03", "exit_price": 97.5},
                       headers=token_on)
        assert r.status_code == 200, r.text
        assert r.json()["trade"]["r"] == pytest.approx(-1.0 - 1.2 / 100, abs=1e-3)

        body = client.get("/api/journal", headers=token_on).json()
        assert body["stats"]["n_closed"] == 1 and body["stats"]["consecutive_losses"] == 1
        assert body["streak"]["consecutive_losses"] == 1

        assert client.delete(f"/api/journal/{tid}", headers=token_on).json() == {"ok": True}
        assert client.delete(f"/api/journal/{tid}", headers=token_on).status_code == 404

    def test_validation_errors_are_400_chinese(self, client, token_on, mem_journal):
        r = client.post("/api/journal", json=dict(GOOD, stop_price=120), headers=token_on)
        assert r.status_code == 400 and "低于入场价" in r.json()["detail"]
        r = client.post("/api/journal", json=dict(GOOD, entry_date="01/02/2025"), headers=token_on)
        assert r.status_code == 400 and "YYYY-MM-DD" in r.json()["detail"]
        tid = client.post("/api/journal", json=GOOD, headers=token_on).json()["trade"]["id"]
        r = client.put(f"/api/journal/{tid}", json={"exit_price": 105}, headers=token_on)
        assert r.status_code == 400 and "同时" in r.json()["detail"]

    def test_update_missing_404(self, client, token_on, mem_journal):
        r = client.put("/api/journal/12345", json={"note": "x"}, headers=token_on)
        assert r.status_code == 404

    def test_non_object_body_rejected(self, client, token_on, mem_journal):
        r = client.post("/api/journal", json=[1, 2], headers=token_on)
        assert r.status_code in (400, 422)
        assert mem_journal == {}

    def test_db_error_is_500_without_leak(self, client, token_on, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("mysql://root:secret@host failed")
        monkeypatch.setattr(journal, "list_trades", boom)
        r = client.get("/api/journal", headers=token_on)
        assert r.status_code == 500 and "secret" not in r.text

    def test_end_to_end_sqlite(self, client, token_on, sqlite_db):
        """真实 CRUD 函数 + SQLite 替身走一遍端点。"""
        tid = client.post("/api/journal", json=GOOD, headers=token_on).json()["trade"]["id"]
        r = client.put(f"/api/journal/{tid}", json={"exit_date": "2025-01-10", "exit_price": 110},
                       headers=token_on)
        assert r.status_code == 200 and r.json()["trade"]["r"] > 3.9
        body = client.get("/api/journal", headers=token_on).json()
        assert body["stats"]["n_win"] == 1 and len(body["stats"]["equity_curve_r"]) == 1


# ---------- swing 端点 ----------
def _fake_swing(snapshot=None, paper=None, raise_snap=False):
    m = types.ModuleType("swing_scan")

    def latest_snapshot():
        if raise_snap:
            raise RuntimeError("boom")
        return snapshot

    m.latest_snapshot = latest_snapshot
    m.paper_stats = lambda: paper
    return m


class TestSwingEndpoints:
    def test_module_missing(self, client, monkeypatch):
        monkeypatch.setitem(sys.modules, "swing_scan", None)    # 让 import swing_scan 抛 ImportError
        for path in ("/api/swing/snapshot", "/api/swing/paper"):
            r = client.get(path)
            assert r.status_code == 200
            assert r.json()["data"] is None and r.json()["message"]

    def test_no_data(self, client, monkeypatch):
        monkeypatch.setitem(sys.modules, "swing_scan", _fake_swing(None, None))
        for path in ("/api/swing/snapshot", "/api/swing/paper"):
            r = client.get(path)
            assert r.status_code == 200 and r.json()["data"] is None and r.json()["message"]

    def test_snapshot_raises(self, client, monkeypatch):
        monkeypatch.setitem(sys.modules, "swing_scan", _fake_swing(raise_snap=True))
        r = client.get("/api/swing/snapshot")
        assert r.status_code == 200 and r.json()["data"] is None

    def test_snapshot_sanitized_and_private_redacted(self, client, monkeypatch):
        np = pytest.importorskip("numpy")
        snap = {
            "asof": date(2025, 3, 20),
            "env": {"verdict": "selective", "label": "精挑细选", "emoji": "🟡",
                    "reasons": ["自己的单子：连亏 5 笔：暂停开新仓，先停 1-2 天",
                                "自己连亏 3 笔，降风险、只挑最好的", "QQQ 站上 21EMA"],
                    "index": {"QQQ": {"close": np.float64(500.5), "ema9": float("nan"),
                                      "above9": np.bool_(True), "days_above21": np.int64(7)}},
                    "own": {"consecutive_losses": 3, "recent_r": [-1, -1, -1]}},
            "setups": [{"ticker": "NVDA", "stop_pct": np.float64(0.021), "size_shares": np.int64(40),
                        "quality": float("inf")}],
            "watchlist": [], "paper": None,
        }
        monkeypatch.setitem(sys.modules, "swing_scan", _fake_swing(snap))
        r = client.get("/api/swing/snapshot")
        assert r.status_code == 200, r.text
        d = r.json()["data"]
        assert d["asof"] == "2025-03-20"
        assert d["env"]["own"] is None and d["env"]["own_redacted"] is True
        assert d["env"]["reasons"][0] == "QQQ 站上 21EMA"
        assert len(d["env"]["reasons"]) == 2 and "连亏" not in r.text and "自己" not in r.text
        q = d["env"]["index"]["QQQ"]
        assert q["close"] == 500.5 and q["ema9"] is None and q["above9"] is True and q["days_above21"] == 7
        assert d["setups"][0]["size_shares"] == 40 and d["setups"][0]["quality"] is None
        assert "recent_r" not in r.text

    def test_paper_ok(self, client, monkeypatch):
        paper = {"n_closed": 10, "win_rate": 0.3, "avg_win_r": 3.1, "avg_loss_r": -0.9,
                 "expectancy_r": 0.3, "total_r": 3.0, "open": [], "recent": []}
        monkeypatch.setitem(sys.modules, "swing_scan", _fake_swing(None, paper))
        r = client.get("/api/swing/paper")
        assert r.status_code == 200 and r.json()["data"]["n_closed"] == 10

    def test_swing_config_whitelist(self, client, monkeypatch):
        monkeypatch.setattr(config, "SWING", {"account_size": 25000, "risk_pct": 0.004,
                                              "max_stop_pct": "bad", "TWELVEDATA_KEY": "sekret",
                                              "backtest_note": "回测进行中"}, raising=False)
        d = client.get("/api/swing/config").json()
        assert d["account_size"] == 25000 and d["risk_pct"] == 0.004
        assert d["max_stop_pct"] == 0.03                     # 非数字回落默认
        assert d["backtest_note"] == "回测进行中"
        assert "sekret" not in str(d) and "TWELVEDATA_KEY" not in d

    def test_swing_config_missing(self, client, monkeypatch):
        monkeypatch.delattr(config, "SWING", raising=False)
        d = client.get("/api/swing/config").json()
        assert d["account_size"] == 10000 and d["risk_pct"] == 0.005 and d["hard_stop_cap"] == 0.05


# ---------- 现有端点未被破坏 ----------
class TestExistingEndpoints:
    def test_health(self, client, monkeypatch):
        monkeypatch.setattr(db, "table_has_data", lambda t: True)
        assert client.get("/api/health").json() == {"ok": True, "data_ready": True,
                                                     "price_ready": True, "member_ready": True}
        monkeypatch.setattr(db, "table_has_data", lambda t: False)
        assert client.get("/api/health").json()["data_ready"] is False

    def test_meta(self, client):
        d = client.get("/api/meta").json()
        assert set(d) >= {"defaults", "strategies", "markets"}
        assert [m["key"] for m in d["markets"]] == ["A", "US", "HK"]

    def test_index_page(self, client):
        r = client.get("/")
        assert r.status_code == 200
        html = r.text
        for marker in ('id="tabBT"', 'id="tabPF"', 'id="tabSW"', 'id="viewBacktest"',
                       'id="viewPortfolio"', 'id="viewSwing"', "switchTab('sw')", "X-Journal-Token"):
            assert marker in html, marker

    def test_portfolio_defaults(self, client):
        d = client.get("/api/portfolio/defaults").json()
        assert d["tickers"] == config.PORTFOLIO_DEFAULT

    def test_startup_survives_db_failure(self, monkeypatch):
        def boom():
            raise RuntimeError("db not ready")
        monkeypatch.setattr(db, "init_tables", boom)
        monkeypatch.setattr(journal, "ensure_tables", boom)
        with TestClient(webapp_app.app) as c:       # 触发 startup 钩子
            monkeypatch.setattr(db, "table_has_data", lambda t: False)
            assert c.get("/api/health").status_code == 200

    def test_startup_calls_ensure_tables(self, monkeypatch):
        called = []
        monkeypatch.setattr(db, "init_tables", lambda: called.append("db"))
        monkeypatch.setattr(journal, "ensure_tables", lambda: called.append("journal"))
        with TestClient(webapp_app.app):
            pass
        assert called == ["db", "journal"]


def test_snapshot_without_private_reasons_unchanged(client, monkeypatch):
    snap = {"asof": "2025-03-20", "env": {"verdict": "go", "reasons": ["QQQ、IWM 都站上向上的 9EMA 和 21EMA"],
                                          "own": None}, "setups": [], "watchlist": []}
    monkeypatch.setitem(sys.modules, "swing_scan", _fake_swing(snap))
    d = client.get("/api/swing/snapshot").json()["data"]
    assert d["env"]["reasons"] == ["QQQ、IWM 都站上向上的 9EMA 和 21EMA"]
    assert d["env"]["own_redacted"] is False


# ===========================================================================
# 与 core 模块（market_env / swing_scan）的接口契约：core 缺失时跳过
# ===========================================================================
def _idx(in_window=True, above21=True):
    return {"close": 100.0, "ema9": 99.0, "ema21": 97.0, "above9": in_window, "above21": above21,
            "rising9": in_window, "rising21": in_window, "in_window": in_window, "extended": False,
            "ext_pct": 0.03, "days_above21": 5}


class TestCoreContract:
    def test_market_env_consumes_streak_status(self):
        me = pytest.importorskip("market_env")
        index = {"QQQ": _idx(), "IWM": _idx()}
        paused = journal.streak_status_from(_losses(5, start=date(2025, 3, 13)), today=date(2025, 3, 18))
        verdict, reasons, _ = me.decide(index, None, paused)
        assert verdict == "stop" and "连亏" in reasons[0]
        reduce = journal.streak_status_from(_losses(3), today=date(2025, 3, 20))
        assert me.decide(index, None, reduce)[0] == "selective"
        ok = journal.streak_status_from([_t(1, 100, 98, 104, "2025-03-03")], today=date(2025, 3, 20))
        assert me.decide(index, None, ok)[0] == "go"

    def test_market_env_lazy_loads_journal(self, monkeypatch):
        me = pytest.importorskip("market_env")
        monkeypatch.setattr(journal, "streak_status", lambda: {"consecutive_losses": 4})
        assert me._own_status() == {"consecutive_losses": 4}
        monkeypatch.undo()                       # 换回真实 streak_status，让它读库失败

        def boom():
            raise RuntimeError("no db")
        monkeypatch.setattr(journal, "list_trades", boom)
        assert me._own_status() is None          # 读库失败 → None，不抛

    def test_real_swing_scan_snapshot_via_endpoint(self, client, monkeypatch):
        ss = pytest.importorskip("swing_scan")
        import json as _json
        import pandas as pd
        payload = {"asof": "2025-10-16",
                   "env": {"verdict": "selective", "reasons": ["自己连亏 3 笔，降风险、只挑最好的", "IWM跌破21EMA"],
                           "own": {"consecutive_losses": 3, "recent_r": [-1, -1, -1]}},
                   "setups": [{"ticker": "APP", "trigger": 618.0, "stop": 601.2}],
                   "watchlist": [{"ticker": "APP", "source": "gainer", "added_date": "2025-10-02", "days_in": 3}]}
        monkeypatch.setitem(sys.modules, "swing_scan", ss)
        monkeypatch.setattr(db, "read_sql", lambda sql, params=None: pd.DataFrame(
            [{"asof": "2025-10-16", "payload": _json.dumps(payload)}]))
        d = client.get("/api/swing/snapshot").json()["data"]
        assert d["asof"] == "2025-10-16" and d["setups"][0]["ticker"] == "APP"
        assert d["env"]["own"] is None and "recent_r" not in _json.dumps(d)
        assert d["env"]["reasons"][0] == "IWM跌破21EMA"

    def test_real_swing_scan_db_failure_is_null(self, client, monkeypatch):
        ss = pytest.importorskip("swing_scan")
        monkeypatch.setitem(sys.modules, "swing_scan", ss)

        def boom(sql, params=None):
            raise RuntimeError("no mysql")
        monkeypatch.setattr(db, "read_sql", boom)
        for path in ("/api/swing/snapshot", "/api/swing/paper"):
            r = client.get(path)
            assert r.status_code == 200 and r.json()["data"] is None
