"""
量化回测可视化平台 —— FastAPI 后端。

接口：
    GET  /api/health           健康检查 + 数据状态
    GET  /api/meta             可选股票池、默认参数
    POST /api/backtest         跑一次回测（参数在 body），存库，返回结果
    GET  /api/runs             已保存回测列表（用于多策略对比）
    GET  /api/runs/{id}        单条回测详情（含 nav）
    DELETE /api/runs/{id}      删除回测
    GET  /                     仪表盘页面
"""
import os
import sys

# 让 webapp 能 import 上级目录的 engine/db/config
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import db
import engine
import portfolio
import signals

app = FastAPI(title="Quant 回测平台")

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


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
