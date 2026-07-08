# 量化回测可视化平台（Tushare + 服务器 MySQL + Web 仪表盘）

多策略/多因子对比平台。数据存**服务器 MySQL `quant` 库**，回测引擎 + Web 界面跑在**服务器 Docker** 里，
通过浏览器访问。核心闭环：**数据 → 信号 → 回测 → 评估 → 可视化对比**。

## 架构

```
                          ┌─ 数据同步（需 token，偶尔跑）: deploy.sh ─┐
  Tushare API ───────────>│  下载→后复权→灌进 quant 库              │
                          └────────────────────────────────────────┘
                                          │
   浏览器 ──https──> nginx ──> quant-web 容器(FastAPI+ECharts)
   (quant.xiuqilai.com)         │  读 quant 库 → 跑回测 → 存 backtest_run → 返回净值
                                └── 多策略叠加对比 / 指标表 / 删除
```

- 量化项目部署在服务器 `~/quant/`（`/opt` 需 root，故放 home）。
- 两个角色，同一镜像 `quant-bt`：
  - **数据同步**（一次性容器，需 Tushare token）：`deploy.sh`
  - **Web 平台**（常驻容器 `quant-web`，端口 8003，只读库）：`deploy-web.sh`
- 容器在 `app-net` 上连 `mysql:3306` 的 `quant` 库；DB 凭证复用 `/opt/ctj/.env`。
- 基础镜像走 `docker.1ms.run`（服务器 Docker Hub 被墙）。

## 当前状态（已上线）

- ✅ `quant` 库已建，4 张表就绪，数据已同步（沪深300 / 2018-2025）
- ✅ `quant-web` 容器常驻运行（端口 8003）
- ✅ nginx 子域名已配（quant.xiuqilai.com → quant-web），**HTTP 公网可访问**
- 🌐 **访问地址： http://quant.xiuqilai.com**
- ⏳ **HTTPS**：证书需 sudo 签发，是一次性手动步骤（见「配置子域名 SSL」）

## 用法

### 1. 同步数据（首次必做，需 token）
```bash
cd ~/Documents/quant_starter
TUSHARE_TOKEN=你的token ./deploy.sh
```
下载沪深300成分+后复权价灌进库。几分钟，之后回测秒级。

### 2. 部署/更新 Web 平台（不需要 token）
```bash
./deploy-web.sh
```

### 3. 访问界面
浏览器开 **https://quant.xiuqilai.com**（HTTP/HTTPS 均已上线，无需隧道）。
> 当前 HTTPS 暂借 ctj-api 证书，浏览器有证书警告，点「高级 → 继续」可进。签独立证书后消除（见下）。

### 4. 消除 HTTPS 证书警告（一次性，需 sudo，你来做）
ACME 验证目录已铺好（quant HTTP 块的 acme 指向已挂载的 `/var/www/static`）。跑：
```bash
sudo certbot certonly --webroot -w /opt/ctj/static -d quant.xiuqilai.com
sudo cp /etc/letsencrypt/live/quant.xiuqilai.com/fullchain.pem /opt/ctj/ssl/quant-fullchain.pem
sudo cp /etc/letsencrypt/live/quant.xiuqilai.com/privkey.pem   /opt/ctj/ssl/quant-privkey.pem
sudo chown xujz:xujz /opt/ctj/ssl/quant-*.pem
docker restart nginx
```
完成后无警告。详见 `nginx-quant.conf` 注释（含自动续期 hook 提醒）。

> ⚠️ nginx 运维注意：服务器只挂载单个 `default.conf`（非整个 conf.d），改完必须【重启】
> nginx 而非 reload。CTJ 的 `make deploy-api` 若 rsync nginx 配置可能覆盖 quant 块，部署后留意。
> 备份：`default.conf.bak148`（纯 CTJ）、`default.conf.bak-http`（CTJ+quant HTTP）。

## 界面功能

- 左侧参数面板：策略 / 股票池 / 动量回看 / 持仓数 / 区间 / 命名 → 「跑回测」
- 净值对比图（ECharts）：勾选多条回测记录叠加，自动带沪深300基准虚线
- 回测记录表：年化 / 夏普 / 最大回撤 / 胜率，勾选对比、删除
- 所有回测结果存 `backtest_run` 表，刷新不丢

## 文件结构

| 文件 | 作用 |
|------|------|
| `config.py` | 区间、股票池、成本、策略默认参数 |
| `db.py` | MySQL 连接 + 4 张表 + 回测记录读写 |
| `data.py` | 下载+后复权+入库 / 纯读库接口（web 用） |
| `strategy.py` | 动量信号 → 目标权重（参数可调） |
| `backtest.py` | 回测引擎 + 绩效指标 |
| `engine.py` | 参数化回测封装（web 和 CLI 共用） |
| `run.py` | CLI 批处理（同步+回测+出图） |
| `webapp/app.py` | FastAPI 后端 |
| `webapp/static/index.html` | 仪表盘（单文件 + ECharts CDN） |
| `Dockerfile` / `deploy.sh` / `deploy-web.sh` / `nginx-quant.conf` | 容器化 + 部署 |

## quant 库数据表

| 表 | 内容 |
|----|------|
| `daily_price` | 后复权收盘价 |
| `index_member` | 指数每月成分股（动态股票池，避幸存者偏差） |
| `benchmark` | 基准指数净值 |
| `backtest_run` | 每次回测的参数+指标+净值（供对比） |

## 进阶路径

1. 调参对比：界面上跑不同 lookback/top_n，叠加看哪个夏普高（⚠️别过拟合）。
2. 加因子：`data.py` 加 `daily_basic`（估值）/`fina_indicator`（财务）入库，
   `strategy.py` 加低 PB / 高 ROE 策略，`/api/meta` 的 strategies 加选项。
3. 多因子打分合成。
4. 样本外验证：前段调参、后段验证。
5. 定时更新：把数据同步拆成增量脚本，crontab 每日收盘后跑。

## ⚠️ 提醒

回测漂亮 ≠ 实盘赚钱。先模拟盘跑 1-3 个月。本项目是学习脚手架，不构成投资建议。
