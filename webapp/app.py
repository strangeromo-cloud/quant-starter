"""
量化回测可视化平台 —— FastAPI 后端。

接口：
    GET  /api/health           健康检查 + 数据状态
    GET  /api/meta             可选股票池、默认参数
    POST /api/backtest         跑一次回测（参数在 body），存库，返回结果
    GET  /api/runs             已保存回测列表（用于多策略对比）
    GET  /api/runs/{id}        单条回测详情（含 nav）
    DELETE /api/runs/{id}      删除回测
    GET  /api/swing/config     短线参数（账户/风险/止损上限，供 R 计算器默认值；只透白名单数值）
    GET  /api/swing/snapshot   最近一次短线扫描（交易环境 + 今日形态 + 观察名单）
    GET  /api/swing/paper      短线纸面实盘统计
    GET/POST /api/journal      交易日志列表+统计+连亏状态 / 新增（需请求头 X-Journal-Token）
    PUT/DELETE /api/journal/{id}  修改(含平仓) / 删除（需 X-Journal-Token）
    GET  /                     仪表盘页面
"""
import math
import os
import re
import sys
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Optional

# 让 webapp 能 import 上级目录的 engine/db/config
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import db
import engine
import journal
import portfolio
import signals

app = FastAPI(title="Quant 回测平台")
from webapp.migration_access import migration_access
app.middleware("http")(migration_access)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class BacktestRequest(BaseModel):
    name: str = ""
    strategy: str = "momentum"
    market: str = "A"
    index_code: str = config.INDEX_CODE
    lookback: int = config.MOMENTUM_LOOKBACK
    top_n: int = config.HOLD_TOP_N
    timing_ma: int = config.TIMING_MA_DAYS
    value_weight: float = 0.5
    quality_weight: float = 0.5
    start: str = config.START_DATE
    end: str = config.END_DATE


@app.on_event("startup")
def _startup():
    try:
        db.init_tables()
    except Exception as e:
        print(f"[startup] init_tables 失败（可能 DB 未就绪）: {e}")
    try:
        journal.ensure_tables()
    except Exception as e:
        print(f"[startup] journal.ensure_tables 失败（可能 DB 未就绪）: {type(e).__name__}")


@app.get("/api/health")
def health():
    price_ready = db.table_has_data("daily_price")
    member_ready = db.table_has_data("index_member")
    return {"ok": True, "data_ready": price_ready and member_ready,
            "price_ready": price_ready, "member_ready": member_ready}


@app.get("/api/meta")
def meta():
    return {
        "defaults": {
            "strategy": "momentum", "index_code": config.INDEX_CODE,
            "lookback": config.MOMENTUM_LOOKBACK, "top_n": config.HOLD_TOP_N,
            "timing_ma": config.TIMING_MA_DAYS, "value_weight": 0.5, "quality_weight": 0.5,
            "start": config.START_DATE, "end": config.END_DATE,
        },
        "strategies": [
            {"key": "momentum", "label": "动量轮动"},
            {"key": "momentum_timing", "label": "动量轮动 + 风控择时"},
            {"key": "multifactor", "label": "多因子(动量+低估值)"},
            {"key": "multifactor_timing", "label": "多因子 + 风控择时"},
            {"key": "quality", "label": "动量+质量(高ROE)"},
            {"key": "quality_timing", "label": "动量+质量 + 风控择时"},
        ],
        "markets": [
            {"key": "A", "label": "A股(沪深300)", "pool": "沪深300成分",
             "strategies": ["momentum", "momentum_timing", "multifactor",
                            "multifactor_timing", "quality", "quality_timing"]},
            {"key": "US", "label": "美股(纳斯达克)", "pool": "纳斯达克40大票",
             "strategies": ["momentum", "momentum_timing"]},
            {"key": "HK", "label": "港股(恒生)", "pool": "恒生科技+蓝筹8只",
             "strategies": ["momentum", "momentum_timing"]},
        ],
    }


@app.post("/api/backtest")
def run_backtest(req: BacktestRequest):
    params = req.dict()
    try:
        result = engine.run_backtest_job(params)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    p = result["params"]
    name = req.name.strip() or f"{p['strategy']}-L{p['lookback']}-Top{p['top_n']}"
    # nav blob 同时存策略净值 + 基准，页面刷新后仍能显示对比基准
    nav_blob = {
        "strategy": result["nav"],
        "benchmark": result["benchmark"],
        "benchmark_metrics": result["benchmark_metrics"],
    }
    run_id = db.insert_run(name, p, result["metrics"], nav_blob)
    result["id"] = run_id
    result["name"] = name
    return result


class PortfolioRequest(BaseModel):
    tickers: list = []
    start: str = config.START_DATE
    end: str = config.END_DATE


@app.get("/api/portfolio/defaults")
def portfolio_defaults():
    return {"tickers": config.PORTFOLIO_DEFAULT,
            "names": {t: config.TICKER_NAMES.get(t, t) for t in config.PORTFOLIO_DEFAULT}}


@app.post("/api/portfolio")
def analyze_portfolio(req: PortfolioRequest):
    tickers = [t.strip() for t in req.tickers if t.strip()]
    if not tickers:
        raise HTTPException(status_code=400, detail="请至少输入一个标的代码")
    try:
        return portfolio.analyze(tickers, req.start, req.end)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


class SignalRequest(BaseModel):
    tickers: list = []
    ma_days: int = 200


@app.post("/api/portfolio/refresh")
def portfolio_refresh(req: PortfolioRequest):
    import data as _data
    tickers = [t.strip() for t in req.tickers if t.strip()]
    if not tickers:
        raise HTTPException(status_code=400, detail="请至少输入一个标的代码")
    try:
        return _data.refresh_holding_prices(tickers)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/portfolio/signals")
def portfolio_signals(req: SignalRequest):
    tickers = [t.strip() for t in req.tickers if t.strip()]
    if not tickers:
        raise HTTPException(status_code=400, detail="请至少输入一个标的代码")
    return {"signals": signals.compute(tickers, req.ma_days)}


@app.get("/api/runs")
def list_runs():
    return {"runs": db.list_runs()}


@app.get("/api/runs/{run_id}")
def get_run(run_id: int):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="not found")
    return run


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: int):
    db.delete_run(run_id)
    return {"ok": True}


# ===================== ⚡ 短线（扫描结果 + 纸面实盘 + 交易日志） =====================
# swing_scan / market_env 由 core 模块提供，这里一律懒导入：模块缺失或库里没数据时
# 返回 {"data": null, "message": ...}，页面显示空态，绝不 500。

# 透给前端的短线参数白名单（只放数值 + 回测说明，绝不透出任何密钥）
_SWING_PUBLIC = {
    "account_size": 10000, "risk_pct": 0.005, "max_position_pct": 0.25,
    "max_stop_pct": 0.03, "hard_stop_cap": 0.05, "adr_min": 0.035,
    "dollar_vol_min": 5e7, "mcap_min": 1e9, "price_min": 5,
    "watch_days": 20, "watch_topk": 20, "card_max_setups": 8,
}


def _jsonable(o):
    """递归清洗：numpy 标量→Python、NaN/inf→None、日期→字符串（Starlette 不接受 NaN）。"""
    if o is None or isinstance(o, (str, bool)):
        return o
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        o = float(o)
    if hasattr(o, "isoformat") and callable(o.isoformat):     # pandas.Timestamp 等
        try:
            return o.isoformat()
        except Exception:
            return str(o)
    if hasattr(o, "item") and callable(o.item):               # numpy 标量
        try:
            o = o.item()
        except Exception:
            return str(o)
        if isinstance(o, (str, bool)) or o is None:
            return o
    if isinstance(o, int):
        return o
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    return str(o)


def _swing_module():
    try:
        import swing_scan  # noqa: 懒导入（core 并行开发，可能尚不存在）
        return swing_scan, None
    except Exception as e:
        print(f"[web] swing_scan 不可用: {type(e).__name__}: {str(e)[:100]}")
        return None, "短线扫描模块尚未部署"


_RISK_NOTE_RE = re.compile(r"；?风险系数×[0-9.]+（按纪律降风险）")


def _is_private_reason(r) -> bool:
    """market_env.decide() 由 env['own'] 生成的理由（"自己的单子：…" / "自己连亏 N 笔…"）。"""
    t = str(r)
    return "连亏" in t or t.startswith("自己") or "自己的单子" in t


def _redact_private(snap: dict) -> dict:
    """快照是公开接口：env['own'] 来自个人交易日志（连亏/近期R），必须剥离，
    改由需要令牌的 /api/journal 提供；由它生成的理由句同样去掉。"""
    env = snap.get("env")
    if isinstance(env, dict):
        env = dict(env)
        had_own = env.get("own") is not None
        env["own"] = None
        env["own_redacted"] = had_own
        pub = env.pop("public", None)
        if isinstance(pub, dict) and pub.get("verdict") in ("go", "selective", "stop"):
            # core 给了"不含个人交易日志"的公开结论：整体换上，连结论颜色都不受自己连亏影响
            for k in ("verdict", "emoji", "label"):
                if k in pub:
                    env[k] = pub[k]
            env["reasons"] = [r for r in (pub.get("reasons") or []) if not _is_private_reason(r)]
        elif isinstance(env.get("reasons"), list):
            kept = [r for r in env["reasons"] if not _is_private_reason(r)]
            if len(kept) < len(env["reasons"]):
                kept.append("另有来自个人交易日志的理由（输入令牌后在下方查看）")
            env["reasons"] = kept
        snap = dict(snap, env=env)
    if isinstance(snap.get("setups"), list):      # 旧快照：形态备注里的"风险系数×0.5"也来自个人日志
        snap = dict(snap, setups=[
            dict(s, note=_RISK_NOTE_RE.sub("", s["note"])) if isinstance(s, dict) and isinstance(s.get("note"), str)
            else s for s in snap["setups"]])
    return snap


@app.get("/api/swing/config")
def swing_config():
    sw = getattr(config, "SWING", None) or {}
    out = {}
    for k, dv in _SWING_PUBLIC.items():
        v = sw.get(k, dv) if isinstance(sw, dict) else dv
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            v = dv
        out[k] = v
    note = sw.get("backtest_note", "") if isinstance(sw, dict) else ""
    out["backtest_note"] = str(note)[:600] if note else ""
    return out


@app.get("/api/swing/snapshot")
def swing_snapshot():
    mod, err = _swing_module()
    if mod is None:
        return {"data": None, "message": err}
    try:
        snap = mod.latest_snapshot()
    except Exception as e:
        print(f"[web] latest_snapshot 失败: {type(e).__name__}: {str(e)[:120]}")
        return {"data": None, "message": "读取扫描结果失败，稍后再试"}
    if not snap:
        return {"data": None, "message": "还没有扫描结果（每日定时扫描后生成）"}
    if not isinstance(snap, dict):
        return {"data": None, "message": "扫描结果格式异常"}
    return {"data": _jsonable(_redact_private(snap)), "message": None}


@app.get("/api/swing/paper")
def swing_paper():
    mod, err = _swing_module()
    if mod is None:
        return {"data": None, "message": err}
    try:
        st = mod.paper_stats()
    except Exception as e:
        print(f"[web] paper_stats 失败: {type(e).__name__}: {str(e)[:120]}")
        return {"data": None, "message": "读取纸面实盘失败，稍后再试"}
    if not st:
        return {"data": None, "message": "纸面实盘还没有记录"}
    return {"data": _jsonable(st), "message": None}


def _journal_auth(token: Optional[str]):
    """令牌校验：未配置 → 503；配置的令牌太短 → 503；不匹配 → 401。"""
    status = journal.check_token(token)
    if status == "unconfigured":
        raise HTTPException(status_code=503, detail=(
            "交易日志未配置访问令牌：请在服务器运行 python journal.py set-token，"
            "或设置环境变量 JOURNAL_TOKEN（至少 16 位随机字符）"))
    if status == "weak":
        raise HTTPException(status_code=503, detail=(
            f"交易日志访问令牌太短（至少 {journal.MIN_TOKEN_LEN} 位），请重新生成"))
    if status != "ok":
        raise HTTPException(status_code=401, detail="访问令牌错误")


def _db_fail(action: str, e: Exception):
    print(f"[web] journal {action} 失败: {type(e).__name__}: {str(e)[:120]}")
    raise HTTPException(status_code=500, detail=f"交易日志{action}失败（数据库错误）")


@app.get("/api/journal")
def journal_list(x_journal_token: Optional[str] = Header(None)):
    _journal_auth(x_journal_token)
    try:
        trades = journal.list_trades()
    except Exception as e:
        _db_fail("读取", e)
    return _jsonable({"trades": trades,
                      "stats": journal.compute_stats(trades),
                      "streak": journal.streak_status_from(trades)})


@app.post("/api/journal")
def journal_create(payload: Dict[str, Any] = Body(...),
                   x_journal_token: Optional[str] = Header(None)):
    _journal_auth(x_journal_token)
    try:
        t = journal.create_trade(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _db_fail("保存", e)
    return _jsonable({"trade": t})


@app.put("/api/journal/{trade_id}")
def journal_update(trade_id: int, payload: Dict[str, Any] = Body(...),
                   x_journal_token: Optional[str] = Header(None)):
    _journal_auth(x_journal_token)
    try:
        t = journal.update_trade(trade_id, payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _db_fail("修改", e)
    if t is None:
        raise HTTPException(status_code=404, detail="交易不存在")
    return _jsonable({"trade": t})


@app.delete("/api/journal/{trade_id}")
def journal_delete(trade_id: int, x_journal_token: Optional[str] = Header(None)):
    _journal_auth(x_journal_token)
    try:
        ok = journal.delete_trade(trade_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _db_fail("删除", e)
    if not ok:
        raise HTTPException(status_code=404, detail="交易不存在")
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
