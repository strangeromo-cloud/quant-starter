"""
同步港美股数据（行情 + 基准指数）到 quant 库。
用法（在服务器，需 token + 连库）：
    docker run --rm --network app-net \
      -e TUSHARE_TOKEN=xxx -e DB_HOST=mysql -e DB_USER=root \
      -e DB_PASSWORD=xxx -e DB_NAME=quant \
      quant-bt python sync_global.py

港美股只支持动量/动量+风控（无估值/质量数据）。当前成分静态池，有幸存者偏差。
"""
import db
import data

print("=" * 50)
print("同步港美股数据 → quant 库")
print("=" * 50)
db.init_tables()

# 美股：Tushare us_daily（不限频）
print("\n--- US ---")
data.sync_global_benchmark("US")
data.sync_global_prices("US")

# 港股：基准 HSI 用 Tushare index_global，个股用 akshare（Tushare hk_daily 限频太狠）
print("\n--- HK ---")
data.sync_global_benchmark("HK")
data.sync_hk_prices_akshare()

print("\n✅ 港美股数据同步完成。可在平台切换市场回测。")
