"""
交易日志（个人隐私数据）：记录自己的真实短线交易，用 R 倍数记账，并给出"连亏熔断"状态。

为什么要有这个模块：
  Martin Luk 式短线胜率只有约 22%，全靠"亏小赚大"。这种打法下，**看胜率没有意义，
  只能看 R**（1R = 打到初始止损会亏的钱）：平均赚几 R、平均亏几 R、期望每笔几 R。
  另外视频里最重要的一条纪律是"三看"里的第一看——自己的单子：新仓接连止损 → 风险减半；
  连亏一串 → 停 1-2 天。人在连亏时最容易上头加码，所以把这条规则交给程序算，
  由 market_env 懒加载 streak_status() 放进交易环境（env['own']），网页和卡片都能看到。

记账口径：
  - stop_price 是**初始止损**（定义 1R）。之后把止损挪到保本不要改它，否则 R 会被算错。
  - 分批卖出时 exit_price 填加权平均卖价，fees 填总手续费（买+卖）。
  - R = ((卖价-买价)×股数 - 手续费) / ((买价-初始止损)×股数)
  - |R| ≤ 0.05 视为"保本"（挪到成本价被扫掉的单子），既不算赢也不算亏：
    不增加连亏计数，也不解除熔断（熔断要等一笔真正的盈利）。

安全：
  - 所有读写接口都要求请求头 X-Journal-Token（由 webapp 校验，见 check_token）。
  - 期望令牌 = 环境变量 JOURNAL_TOKEN，否则 app_settings['journal_token']；都没有 → 视为未配置。
  - 全部 SQL 参数化；输入严格校验（价格>0、止损<入场价、日期格式等），校验失败抛 ValueError(中文)。

命令行：
  python journal.py init        幂等建表
  python journal.py set-token   生成随机令牌写入 app_settings（只打印这一次，请存到浏览器里）
  python journal.py stats       打印统计与熔断状态（不打印令牌）
"""
from __future__ import annotations

import hmac
import math
import os
import re
import secrets
import sys
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import numpy as np
from sqlalchemy import text

import config
import db

# ---------------------------------------------------------------------------
# 规则常量
# ---------------------------------------------------------------------------
SCRATCH_R = 0.05      # |R| ≤ 0.05 视为保本（不算赢也不算亏）
REDUCE_AFTER = 3      # 连亏 ≥3 笔 → 风险减半，直到下一笔盈利
PAUSE_AFTER = 5       # 连亏 ≥5 笔 → 暂停开新仓
PAUSE_DAYS = 2        # 暂停的交易日数（最后一笔亏损平仓后的 2 个交易日）
RECENT_N = 10         # streak 里返回最近多少笔 R
MIN_TOKEN_LEN = 16    # 令牌最短长度（太短等于没设）

SETUPS = {"pullback": "回调", "breakout": "突破", "ep": "EP", "other": "其他"}
SIDES = ("long",)

_FIELDS = ["ts_code", "side", "setup", "entry_date", "entry_price", "stop_price",
           "shares", "exit_date", "exit_price", "fees", "note"]
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,15}$")
_MAX_PRICE = 1e7
_MAX_SHARES = 1e9
_MAX_FEES = 1e7
_MAX_NOTE = 500
_MIN_DATE = date(1990, 1, 1)

_DDL_MYSQL = [
    """
    CREATE TABLE IF NOT EXISTS trade_journal (
        id          INT AUTO_INCREMENT PRIMARY KEY,
        ts_code     VARCHAR(16)  NOT NULL,
        side        VARCHAR(8)   NOT NULL DEFAULT 'long',
        setup       VARCHAR(16)  NULL,
        entry_date  DATE         NOT NULL,
        entry_price DOUBLE       NOT NULL,
        stop_price  DOUBLE       NOT NULL,
        shares      DOUBLE       NOT NULL,
        exit_date   DATE         NULL,
        exit_price  DOUBLE       NULL,
        fees        DOUBLE       NOT NULL DEFAULT 0,
        note        TEXT         NULL,
        created_at  DATETIME     NOT NULL,
        updated_at  DATETIME     NOT NULL,
        KEY idx_entry_date (entry_date)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
    """
    CREATE TABLE IF NOT EXISTS app_settings (
        k VARCHAR(64) NOT NULL PRIMARY KEY,
        v TEXT        NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """,
]
# 仅供本地测试（SQLite 内存库）。列与 MySQL 版一一对应。
_DDL_SQLITE = [
    """
    CREATE TABLE IF NOT EXISTS trade_journal (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_code     VARCHAR(16)  NOT NULL,
        side        VARCHAR(8)   NOT NULL DEFAULT 'long',
        setup       VARCHAR(16)  NULL,
        entry_date  DATE         NOT NULL,
        entry_price DOUBLE       NOT NULL,
        stop_price  DOUBLE       NOT NULL,
        shares      DOUBLE       NOT NULL,
        exit_date   DATE         NULL,
        exit_price  DOUBLE       NULL,
        fees        DOUBLE       NOT NULL DEFAULT 0,
        note        TEXT         NULL,
        created_at  DATETIME     NOT NULL,
        updated_at  DATETIME     NOT NULL
    )
    """,
    "CREATE TABLE IF NOT EXISTS app_settings (k VARCHAR(64) NOT NULL PRIMARY KEY, v TEXT NULL)",
]


def _base_risk_pct() -> float:
    sw = getattr(config, "SWING", None) or {}
    try:
        v = float(sw.get("risk_pct", 0.005))
        return v if 0 < v < 1 else 0.005
    except Exception:
        return 0.005


# ===========================================================================
# 纯函数（无数据库，便于测试）
# ===========================================================================
def _f(v) -> Optional[float]:
    """转 float；None/空串/NaN/inf/bool → None。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _d(v) -> Optional[date]:
    """转 date；接受 date/datetime/'YYYY-MM-DD'（可带时间尾巴）。失败 → None。"""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        s = v.strip()[:10]
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def _ds(v) -> Optional[str]:
    d = _d(v)
    return d.isoformat() if d else None


def r_multiple(entry, stop, exit, fees=0.0, shares=None) -> Optional[float]:
    """
    R 倍数 = 净盈亏 / 初始风险。
      有股数：((exit-entry)×shares - fees) / ((entry-stop)×shares)
      无股数：(exit-entry)/(entry-stop)（每股口径，此时无法分摊手续费，忽略 fees）
    入参非法（止损不低于入场价、价格非正等）返回 None。
    """
    e, s, x = _f(entry), _f(stop), _f(exit)
    if e is None or s is None or x is None or e <= 0 or s < 0 or x < 0:
        return None
    risk_ps = e - s
    if risk_ps <= 0:
        return None
    n = _f(shares)
    if n is None or n <= 0:
        return (x - e) / risk_ps
    fee = _f(fees) or 0.0
    return ((x - e) * n - fee) / (risk_ps * n)


def _is_closed(t: dict) -> bool:
    return _f(t.get("exit_price")) is not None


def _trade_r(t: dict) -> Optional[float]:
    return r_multiple(t.get("entry_price"), t.get("stop_price"), t.get("exit_price"),
                      t.get("fees"), t.get("shares"))


def _classify(r: float) -> str:
    if r > SCRATCH_R:
        return "win"
    if r < -SCRATCH_R:
        return "loss"
    return "scratch"


def _closed_sorted(trades) -> list:
    """已平仓且 R 可算的交易，按 (平仓日, 入场日, id) 升序 → [(trade, r)]。"""
    out = []
    for t in trades or []:
        if not _is_closed(t):
            continue
        r = _trade_r(t)
        if r is None:
            continue
        out.append((t, r))
    out.sort(key=lambda p: (_ds(p[0].get("exit_date")) or "",
                            _ds(p[0].get("entry_date")) or "",
                            int(_f(p[0].get("id")) or 0)))
    return out


def _consecutive_losses(rs: list) -> int:
    """从最近一笔往回数连亏笔数：保本单跳过（不计数也不打断），遇到盈利停止。"""
    n = 0
    for r in reversed(rs):
        c = _classify(r)
        if c == "loss":
            n += 1
        elif c == "win":
            break
    return n


def _rnd(v, k=3):
    return None if v is None else round(float(v), k)


def compute_stats(trades) -> dict:
    """
    交易统计（R 口径）。trades 为 list_trades() 的行（或含同名字段的 dict）。
    win_rate 是 0~1 的小数；avg_loss_r 为负数；payoff = 平均盈利R / |平均亏损R|。
    equity_curve_r：按平仓先后的累计 R 曲线 [{date, ts_code, r, cum_r}]。
    """
    trades = list(trades or [])
    n_open = sum(1 for t in trades if not _is_closed(t))
    closed = _closed_sorted(trades)
    rs = [r for _, r in closed]
    wins = [r for r in rs if _classify(r) == "win"]
    losses = [r for r in rs if _classify(r) == "loss"]
    n = len(rs)

    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    payoff = (avg_win / abs(avg_loss)) if (avg_win is not None and avg_loss) else None

    curve, cum, peak, mdd = [], 0.0, 0.0, 0.0
    for t, r in closed:
        cum += r
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
        curve.append({"date": _ds(t.get("exit_date")), "ts_code": t.get("ts_code"),
                      "r": round(r, 3), "cum_r": round(cum, 3)})

    max_run, run = 0, 0
    for r in rs:
        c = _classify(r)
        if c == "loss":
            run += 1
            max_run = max(max_run, run)
        elif c == "win":
            run = 0

    pnl = 0.0
    for t, _ in closed:
        e, x, sh = _f(t.get("entry_price")), _f(t.get("exit_price")), _f(t.get("shares"))
        if e is not None and x is not None and sh:
            pnl += (x - e) * sh - (_f(t.get("fees")) or 0.0)

    return {
        "n_closed": n,
        "n_open": n_open,
        "n_win": len(wins),
        "n_loss": len(losses),
        "n_scratch": n - len(wins) - len(losses),
        "win_rate": round(len(wins) / n, 4) if n else None,
        "avg_win_r": _rnd(avg_win),
        "avg_loss_r": _rnd(avg_loss),
        "payoff": _rnd(payoff, 2),
        "expectancy_r": _rnd(sum(rs) / n) if n else None,
        "total_r": round(sum(rs), 3),
        "max_drawdown_r": round(mdd, 3),
        "max_consec_losses": max_run,
        "consecutive_losses": _consecutive_losses(rs),
        "total_pnl": round(pnl, 2),
        "scratch_band_r": SCRATCH_R,
        "equity_curve_r": curve,
    }


def _busdays_between(d0: date, d1: date) -> int:
    """d0 → d1 之间的工作日数（numpy 口径：[d0, d1) 内的周一~周五，不含美股假日）。"""
    return int(np.busday_count(np.datetime64(d0, "D"), np.datetime64(d1, "D")))


def streak_status_from(trades, today: Optional[date] = None,
                       risk_pct: Optional[float] = None) -> dict:
    """
    连亏熔断状态（BRIEF 约定结构 + 若干附加字段）：
      {consecutive_losses, recent_r, risk_mult, pause, note,
       level('none'|'normal'|'caution'|'reduce'|'pause'), n_closed, last_exit_date, resume_date}
    规则：连亏≥3 → risk_mult=0.5 直到下一笔盈利；连亏≥5 → 最后一笔亏损后暂停 2 个交易日
    （暂停期过后 pause=False，但仍半风险，等一笔盈利再恢复 1.0）。
    """
    today = _d(today) or date.today()
    base = risk_pct if (risk_pct and 0 < risk_pct < 1) else _base_risk_pct()
    half_txt = f"{base * 50:.2f}%".replace(".00%", "%")   # base×0.5×100
    closed = _closed_sorted(trades)
    rs = [r for _, r in closed]
    cl = _consecutive_losses(rs)

    out = {
        "consecutive_losses": cl,
        "recent_r": [round(r, 3) for r in rs[-RECENT_N:]],
        "risk_mult": 1.0,
        "pause": False,
        "note": "",
        "level": "normal",
        "n_closed": len(rs),
        "last_exit_date": _ds(closed[-1][0].get("exit_date")) if closed else None,
        "resume_date": None,
    }

    if not rs:
        out.update(level="none", note="暂无已平仓交易，按正常风险执行")
        return out

    if cl >= PAUSE_AFTER:
        last_loss = next((t for t, r in reversed(closed) if _classify(r) == "loss"), None)
        d0 = _d(last_loss.get("exit_date")) if last_loss else None
        out["risk_mult"] = 0.5
        if d0 is None:
            # 没有平仓日期就无法计时，保守起见视为仍在暂停期
            out.update(pause=True, level="pause",
                       note=f"连亏 {cl} 笔：暂停开新仓 1-2 个交易日，先复盘再以半风险（{half_txt}）恢复")
            return out
        resume = np.busday_offset(np.datetime64(d0, "D"), PAUSE_DAYS + 1, roll="forward")
        out["resume_date"] = str(resume)
        if _busdays_between(d0, today) <= PAUSE_DAYS:
            out.update(pause=True, level="pause",
                       note=f"连亏 {cl} 笔：暂停开新仓，{out['resume_date']} 起再以半风险（{half_txt}）恢复")
        else:
            out.update(level="reduce",
                       note=f"连亏 {cl} 笔：暂停期已过，恢复时每笔风险减半（{half_txt}），等一笔盈利再回到正常")
        return out

    if cl >= REDUCE_AFTER:
        out.update(risk_mult=0.5, level="reduce",
                   note=f"连亏 {cl} 笔：下一笔起风险减半（{half_txt}），直到出现一笔盈利")
        return out

    if cl > 0:
        out.update(level="caution",
                   note=f"连亏 {cl} 笔：正常风险；再亏 {REDUCE_AFTER - cl} 笔将风险减半")
        return out

    last_c = _classify(rs[-1])
    out["note"] = ("最近一笔盈利，正常风险" if last_c == "win"
                   else "最近一笔保本，正常风险")
    return out


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------
def _num(label: str, v, required: bool, *, min_excl: Optional[float] = 0.0,
         allow_zero: bool = False, max_val: float = _MAX_PRICE) -> Optional[float]:
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise ValueError(f"{label}不能为空")
        return None
    if isinstance(v, bool):
        raise ValueError(f"{label}必须是数字")
    x = _f(v)
    if x is None:
        raise ValueError(f"{label}必须是有效数字")
    if allow_zero:
        if x < 0:
            raise ValueError(f"{label}不能为负数")
    elif min_excl is not None and x <= min_excl:
        raise ValueError(f"{label}必须大于 0")
    if x > max_val:
        raise ValueError(f"{label}数值过大")
    return x


def _date_field(label: str, v, required: bool, today: date) -> Optional[date]:
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise ValueError(f"{label}不能为空")
        return None
    if isinstance(v, (date, datetime)):
        d = _d(v)
    elif isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v.strip()[:10]) \
            and (len(v.strip()) == 10 or v.strip()[10:11] in ("T", " ")):
        d = _d(v)
    else:
        d = None
    if d is None:
        raise ValueError(f"{label}格式应为 YYYY-MM-DD")
    if d < _MIN_DATE:
        raise ValueError(f"{label}早于 1990 年，请检查")
    if d > today + timedelta(days=1):   # 留 1 天时区余量
        raise ValueError(f"{label}不能晚于今天")
    return d


def validate_trade(payload: Any, existing: Optional[dict] = None,
                   today: Optional[date] = None) -> dict:
    """
    校验并清洗一笔交易，返回可直接写库的完整字段 dict（日期为 date，数字为 float）。
    existing 给定时为部分更新：payload 中出现的键覆盖 existing，其余沿用。
    失败抛 ValueError（中文提示）。
    """
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    today = _d(today) or date.today()
    base = {k: (existing or {}).get(k) for k in _FIELDS}
    for k in _FIELDS:
        if k in payload:
            base[k] = payload[k]

    # 代码
    code = base.get("ts_code")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("代码不能为空")
    code = code.strip().upper()
    if not _TICKER_RE.match(code):
        raise ValueError("代码格式不正确（例：NVDA、BRK.B、00700.HK）")

    side = base.get("side")
    side = (side.strip().lower() if isinstance(side, str) and side.strip() else "long")
    if side not in SIDES:
        raise ValueError("目前只支持做多（long）")

    setup = base.get("setup")
    if setup is None or (isinstance(setup, str) and not setup.strip()):
        setup = None
    elif not isinstance(setup, str) or setup.strip().lower() not in SETUPS:
        raise ValueError("形态只能是 pullback / breakout / ep / other")
    else:
        setup = setup.strip().lower()

    entry_date = _date_field("入场日期", base.get("entry_date"), True, today)
    entry = _num("入场价", base.get("entry_price"), True)
    stop = _num("初始止损价", base.get("stop_price"), True)
    if stop >= entry:
        raise ValueError("初始止损价必须低于入场价（做多）")
    shares = _num("股数", base.get("shares"), True, max_val=_MAX_SHARES)
    fees = _num("手续费", base.get("fees"), False, allow_zero=True, max_val=_MAX_FEES) or 0.0

    exit_date = _date_field("平仓日期", base.get("exit_date"), False, today)
    exit_price = _num("平仓价", base.get("exit_price"), False)
    if (exit_date is None) != (exit_price is None):
        raise ValueError("平仓需同时填写平仓日期和平仓价（撤销平仓则两者都清空）")
    if exit_date is not None and exit_date < entry_date:
        raise ValueError("平仓日期不能早于入场日期")

    note = base.get("note")
    if note is None:
        note = None
    elif not isinstance(note, str):
        raise ValueError("备注必须是文字")
    else:
        note = note.strip() or None
        if note and len(note) > _MAX_NOTE:
            raise ValueError(f"备注最多 {_MAX_NOTE} 字")

    return {"ts_code": code, "side": side, "setup": setup,
            "entry_date": entry_date, "entry_price": entry, "stop_price": stop,
            "shares": shares, "exit_date": exit_date, "exit_price": exit_price,
            "fees": fees, "note": note}


def _row_out(row: dict) -> dict:
    """数据库行 → 接口输出（日期转字符串 + 派生字段）。"""
    e, s = _f(row.get("entry_price")), _f(row.get("stop_price"))
    sh, x = _f(row.get("shares")), _f(row.get("exit_price"))
    fees = _f(row.get("fees")) or 0.0
    setup = row.get("setup")
    code = row.get("ts_code")
    out = {
        "id": int(row["id"]) if row.get("id") is not None else None,
        "ts_code": code,
        "name": config.TICKER_NAMES.get(code, "") if hasattr(config, "TICKER_NAMES") else "",
        "side": row.get("side") or "long",
        "setup": setup,
        "setup_cn": SETUPS.get(setup or "", ""),
        "entry_date": _ds(row.get("entry_date")),
        "entry_price": e,
        "stop_price": s,
        "shares": sh,
        "exit_date": _ds(row.get("exit_date")),
        "exit_price": x,
        "fees": fees,
        "note": row.get("note"),
        "created_at": str(row.get("created_at") or "")[:19] or None,
        "updated_at": str(row.get("updated_at") or "")[:19] or None,
        "status": "closed" if x is not None else "open",
        "stop_pct": None, "risk_value": None, "position_value": None,
        "r": None, "pnl": None, "hold_days": None,
    }
    if e and s is not None and e > s:
        out["stop_pct"] = round((e - s) / e, 4)
        if sh:
            out["risk_value"] = round((e - s) * sh, 2)
    if e and sh:
        out["position_value"] = round(e * sh, 2)
    if x is not None:
        r = r_multiple(e, s, x, fees, sh)
        out["r"] = _rnd(r)
        if e is not None and sh:
            out["pnl"] = round((x - e) * sh - fees, 2)
        d0, d1 = _d(row.get("entry_date")), _d(row.get("exit_date"))
        if d0 and d1:
            out["hold_days"] = (d1 - d0).days
    return out


# ===========================================================================
# 数据库读写
# ===========================================================================
def ensure_tables():
    """幂等建表（trade_journal + app_settings）。"""
    eng = db.get_engine()
    stmts = _DDL_SQLITE if eng.dialect.name == "sqlite" else _DDL_MYSQL
    with eng.begin() as conn:
        for s in stmts:
            conn.execute(text(s))


_SELECT = ("SELECT id, " + ", ".join(_FIELDS) + ", created_at, updated_at FROM trade_journal")


def list_trades(limit: int = 5000) -> List[dict]:
    """全部交易，入场日期倒序。"""
    lim = max(1, min(int(limit), 20000))
    sql = _SELECT + " ORDER BY entry_date DESC, id DESC LIMIT :lim"
    with db.get_engine().connect() as conn:
        rows = conn.execute(text(sql), {"lim": lim}).mappings().all()
    return [_row_out(dict(r)) for r in rows]


def _check_id(trade_id) -> int:
    if isinstance(trade_id, bool):
        raise ValueError("id 不合法")
    try:
        i = int(trade_id)
    except (TypeError, ValueError):
        raise ValueError("id 不合法")
    if i <= 0:
        raise ValueError("id 不合法")
    return i


def get_trade(trade_id) -> Optional[dict]:
    i = _check_id(trade_id)
    with db.get_engine().connect() as conn:
        row = conn.execute(text(_SELECT + " WHERE id = :id"), {"id": i}).mappings().first()
    return _row_out(dict(row)) if row else None


def create_trade(payload: dict) -> dict:
    clean = validate_trade(payload)
    now = datetime.now().replace(microsecond=0)
    params = dict(clean, created_at=now, updated_at=now)
    cols = _FIELDS + ["created_at", "updated_at"]
    sql = (f"INSERT INTO trade_journal ({', '.join(cols)}) "
           f"VALUES ({', '.join(':' + c for c in cols)})")
    with db.get_engine().begin() as conn:
        res = conn.execute(text(sql), params)
        new_id = res.lastrowid
    return get_trade(new_id)


def update_trade(trade_id, payload: dict) -> Optional[dict]:
    """部分更新（含平仓：传 exit_date + exit_price [+ fees]）。不存在返回 None。"""
    i = _check_id(trade_id)
    existing = get_trade(i)
    if existing is None:
        return None
    clean = validate_trade(payload, existing=existing)
    params = dict(clean, updated_at=datetime.now().replace(microsecond=0), id=i)
    sets = ", ".join(f"{c} = :{c}" for c in _FIELDS + ["updated_at"])
    with db.get_engine().begin() as conn:
        conn.execute(text(f"UPDATE trade_journal SET {sets} WHERE id = :id"), params)
    return get_trade(i)


def delete_trade(trade_id) -> bool:
    i = _check_id(trade_id)
    with db.get_engine().begin() as conn:
        res = conn.execute(text("DELETE FROM trade_journal WHERE id = :id"), {"id": i})
    return (res.rowcount or 0) > 0


def get_setting(k: str) -> Optional[str]:
    with db.get_engine().connect() as conn:
        row = conn.execute(text("SELECT v FROM app_settings WHERE k = :k"), {"k": k}).first()
    return None if row is None else row[0]


def set_setting(k: str, v: Optional[str]):
    if not isinstance(k, str) or not (0 < len(k) <= 64):
        raise ValueError("设置项名称不合法")
    eng = db.get_engine()
    if eng.dialect.name == "sqlite":
        sql = "INSERT OR REPLACE INTO app_settings (k, v) VALUES (:k, :v)"
    else:
        sql = "INSERT INTO app_settings (k, v) VALUES (:k, :v) ON DUPLICATE KEY UPDATE v = VALUES(v)"
    with eng.begin() as conn:
        conn.execute(text(sql), {"k": k, "v": v})


def streak_status(today: Optional[date] = None) -> Optional[dict]:
    """
    读库 → streak_status_from。被 market_env 懒加载调用（每日推送路径），**绝不抛异常**：
    表不存在 / 数据库不可用 / 日志为空 → None（表示"没有自己的交易数据"）。
    """
    try:
        trades = list_trades()
        if not trades:
            return None
        return streak_status_from(trades, today=today)
    except Exception as e:
        print(f"[journal] streak_status 读取失败（忽略）: {type(e).__name__}: {str(e)[:100]}")
        return None


# ---------------------------------------------------------------------------
# 访问令牌
# ---------------------------------------------------------------------------
def expected_token() -> Optional[str]:
    """环境变量 JOURNAL_TOKEN 优先，否则 app_settings['journal_token']；都没有 → None。"""
    env = (os.environ.get("JOURNAL_TOKEN") or "").strip()
    if env:
        return env
    try:
        v = get_setting("journal_token")
    except Exception as e:
        print(f"[journal] 读取 app_settings 失败: {type(e).__name__}")
        return None
    v = (v or "").strip()
    return v or None


def check_token(provided: Optional[str]) -> str:
    """返回 'ok' | 'unconfigured' | 'weak'（配置的令牌太短）| 'invalid'。"""
    exp = expected_token()
    if not exp:
        return "unconfigured"
    if len(exp) < MIN_TOKEN_LEN:
        return "weak"
    if not isinstance(provided, str) or not provided.strip():
        return "invalid"
    ok = hmac.compare_digest(provided.strip().encode("utf-8"), exp.encode("utf-8"))
    return "ok" if ok else "invalid"


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def _main(argv: list) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "init":
        ensure_tables()
        print("[journal] 已建表 trade_journal / app_settings（幂等）")
        return 0
    if cmd == "set-token":
        ensure_tables()
        tok = secrets.token_urlsafe(24)
        set_setting("journal_token", tok)
        print("[journal] 已生成新令牌并写入 app_settings['journal_token']（旧令牌立即失效）：")
        print(tok)
        print("[journal] 请在网页「⚡ 短线 → 交易日志」里输入它；本令牌不会再次显示。")
        if (os.environ.get("JOURNAL_TOKEN") or "").strip():
            print("[journal] ⚠ 当前进程设置了环境变量 JOURNAL_TOKEN，它优先于 app_settings；"
                  "若服务端也设了该变量，新令牌不会生效。")
        return 0
    if cmd == "stats":
        trades = list_trades()
        st = compute_stats(trades)
        st.pop("equity_curve_r", None)
        print("[journal] 统计:", st)
        print("[journal] 熔断:", streak_status_from(trades))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
