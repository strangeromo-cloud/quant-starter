"""
数据库层：连接服务器 MySQL 的 quant 库，建表 + 读写。

连接配置从环境变量读（在服务器 docker 容器内运行时，DB_HOST=mysql）：
    DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME(=quant)
"""
import os
import pandas as pd
from sqlalchemy import create_engine, text

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        host = os.environ.get("DB_HOST", "mysql")
        port = os.environ.get("DB_PORT", "3306")
        user = os.environ.get("DB_USER", "root")
        pw = os.environ.get("DB_PASSWORD", "")
        name = os.environ.get("DB_NAME", "quant")
        url = f"mysql+pymysql://{user}:{pw}@{host}:{port}/{name}?charset=utf8mb4"
        _engine = create_engine(url, pool_pre_ping=True, pool_recycle=3600)
    return _engine


def init_tables():
    """建表（幂等）。"""
    ddl = [
        """
        CREATE TABLE IF NOT EXISTS daily_price (
            ts_code    VARCHAR(12)  NOT NULL,
            trade_date DATE         NOT NULL,
            close_hfq  DOUBLE       NOT NULL,
            PRIMARY KEY (ts_code, trade_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """,
        """
        CREATE TABLE IF NOT EXISTS index_member (
            index_code VARCHAR(12) NOT NULL,
            trade_date DATE        NOT NULL,
            con_code   VARCHAR(12) NOT NULL,
            PRIMARY KEY (index_code, trade_date, con_code)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """,
        """
        CREATE TABLE IF NOT EXISTS benchmark (
            index_code VARCHAR(12) NOT NULL,
            trade_date DATE        NOT NULL,
            close      DOUBLE      NOT NULL,
            PRIMARY KEY (index_code, trade_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """,
        """
        CREATE TABLE IF NOT EXISTS daily_basic (
            ts_code    VARCHAR(12) NOT NULL,
            trade_date DATE        NOT NULL,
            pb         DOUBLE      NULL,
            total_mv   DOUBLE      NULL,
            PRIMARY KEY (ts_code, trade_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """,
        """
        CREATE TABLE IF NOT EXISTS fina_indicator (
            ts_code  VARCHAR(12) NOT NULL,
            end_date DATE        NOT NULL,
            ann_date DATE        NULL,
            roe      DOUBLE      NULL,
            PRIMARY KEY (ts_code, end_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """,
        """
        CREATE TABLE IF NOT EXISTS recent_price (
            ts_code    VARCHAR(12) NOT NULL,
            trade_date DATE        NOT NULL,
            close      DOUBLE      NOT NULL,
            PRIMARY KEY (ts_code, trade_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """,
        """
        CREATE TABLE IF NOT EXISTS backtest_run (
            id         INT AUTO_INCREMENT PRIMARY KEY,
            name       VARCHAR(120) NOT NULL,
            params     JSON         NOT NULL,
            metrics    JSON         NOT NULL,
            nav        JSON         NOT NULL,
            created_at DATETIME     NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """,
    ]
    with get_engine().begin() as conn:
        for stmt in ddl:
            conn.execute(text(stmt))
    _migrate_recent_price_ohlc()


def _migrate_recent_price_ohlc():
    """给 recent_price 补 high/low 列（幂等）。真 ATR/Chandelier 止损需要高低价。"""
    have = read_sql(
        "SELECT column_name AS col FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = 'recent_price'")
    have.columns = [c.lower() for c in have.columns]  # MySQL 可能返回大写列名
    cols = set(have["col"].str.lower()) if not have.empty else set()
    alters = []
    if "high" not in cols:
        alters.append("ADD COLUMN high DOUBLE NULL AFTER close")
    if "low" not in cols:
        alters.append("ADD COLUMN low DOUBLE NULL AFTER high")
    if alters:
        with get_engine().begin() as conn:
            conn.execute(text(f"ALTER TABLE recent_price {', '.join(alters)}"))


def upsert_df(df: pd.DataFrame, table: str, key_cols: list):
    """
    批量 upsert（INSERT ... ON DUPLICATE KEY UPDATE）。
    df 列名必须与表字段一致。
    """
    if df is None or df.empty:
        return
    cols = list(df.columns)
    placeholders = ", ".join([f":{c}" for c in cols])
    updates = ", ".join([f"{c}=VALUES({c})" for c in cols if c not in key_cols])
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
    if updates:
        sql += f" ON DUPLICATE KEY UPDATE {updates}"
    records = df.to_dict("records")
    # NaN/inf 转 None（MySQL 不接受 nan，需写成 NULL）
    for r in records:
        for k, v in r.items():
            if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
                r[k] = None
    with get_engine().begin() as conn:
        # 分批，避免单条 SQL 过大
        batch = 2000
        for i in range(0, len(records), batch):
            conn.execute(text(sql), records[i:i + batch])


def read_sql(sql: str, params: dict = None) -> pd.DataFrame:
    with get_engine().connect() as conn:
        return pd.read_sql(text(sql), conn, params=params or {})


def table_has_data(table: str) -> bool:
    try:
        df = read_sql(f"SELECT 1 FROM {table} LIMIT 1")
        return not df.empty
    except Exception:
        return False


# ---------- backtest_run 读写 ----------
import json
from datetime import datetime


def insert_run(name: str, params: dict, metrics: dict, nav: dict) -> int:
    sql = ("INSERT INTO backtest_run (name, params, metrics, nav, created_at) "
           "VALUES (:name, :params, :metrics, :nav, :created_at)")
    with get_engine().begin() as conn:
        res = conn.execute(text(sql), {
            "name": name,
            "params": json.dumps(params, ensure_ascii=False),
            "metrics": json.dumps(metrics, ensure_ascii=False),
            "nav": json.dumps(nav, ensure_ascii=False),
            "created_at": datetime.now(),
        })
        return res.lastrowid


def list_runs() -> list:
    """返回所有回测记录（不含 nav 大字段），按时间倒序。"""
    df = read_sql("SELECT id, name, params, metrics, created_at "
                  "FROM backtest_run ORDER BY id DESC")
    out = []
    for _, r in df.iterrows():
        out.append({
            "id": int(r["id"]),
            "name": r["name"],
            "params": _loads(r["params"]),
            "metrics": _loads(r["metrics"]),
            "created_at": str(r["created_at"]),
        })
    return out


def get_run(run_id: int) -> dict:
    df = read_sql("SELECT id, name, params, metrics, nav, created_at "
                  "FROM backtest_run WHERE id=:id", {"id": run_id})
    if df.empty:
        return None
    r = df.iloc[0]
    return {
        "id": int(r["id"]),
        "name": r["name"],
        "params": _loads(r["params"]),
        "metrics": _loads(r["metrics"]),
        "nav": _loads(r["nav"]),
        "created_at": str(r["created_at"]),
    }


def delete_run(run_id: int):
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM backtest_run WHERE id=:id"), {"id": run_id})


def _loads(v):
    """MySQL JSON 列经 pandas 取出可能已是 dict 或是 str，统一处理。"""
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return v
