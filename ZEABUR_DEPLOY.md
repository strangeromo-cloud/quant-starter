# 部署到 Zeabur（泰国节点 47.81.13.32）

从北京服务器迁到你的 Zeabur 专用集群。数据源已验证从泰国全部可达（Tushare/akshare/Twelve Data/Lark/FRED-API）。

标注 **【你做】** 的是需要你在 Zeabur 面板/域名商/云控制台操作的（我操作不了）；其余我可代劳。

---

## 0. 前置 【你做】
- **升配这台机内存到 4G+**（现 3.5G，k3s+7个环境已占 2.1G，MySQL+web 要留余量）。云控制台升配，可能重启。
- （可选）申请免费 **FRED API key**：https://fredaccount.stlouisfed.org → 换回真实高收益债利差 OAS。不申请则用 HYG/LQD 代理（已能用）。

## 1. 代码推到 GitHub 【你做，我给命令】
仓库已在本地 `~/Documents/quant_starter` 初始化好并提交。建一个空 GitHub 私有仓库后：
```bash
cd ~/Documents/quant_starter
git remote add origin git@github.com:<你的账号>/quant.git
git branch -M main
git push -u origin main
```

## 2. Zeabur 建 Web 服务 【你做】
1. 进目标项目 → **Add Service → Git** → 选 `quant` 仓库。Zeabur 自动识别 Dockerfile 构建。
2. 服务起来后，**Networking** 里它监听的端口 Zeabur 会自动注入 `PORT`（Dockerfile 已适配）。

## 3. 加 MySQL 服务 【你做】
1. **Add Service → Marketplace → MySQL**（8.x）。
2. 记下它给的连接信息（host/port/user/password/database），第 5 步填进 web 服务环境变量。

## 4. 迁移数据（北京 → Zeabur MySQL）
保留多年 A/美/港股历史 + 回测 + 已回填 OHLC。**我可代劳**（需北京 MySQL root 密码，在 `~/quant.env` 里）：
```bash
# 1) 从北京导出 quant 库
ssh ctj_xujz "docker exec mysql mysqldump -uroot -p'<ROOT_PW>' --single-transaction --quick --no-tablespaces quant" > /tmp/quant_dump.sql
# 2) 导入 Zeabur MySQL（用第3步的公网连接信息）
mysql -h <zeabur_mysql_host> -P <port> -u <user> -p'<pw>' <database> < /tmp/quant_dump.sql
```

## 5. Web 服务环境变量 【你做，值我给】
Zeabur web 服务 → Variables，填（对应原 `~/quant.env`）：
```
DB_HOST=<zeabur mysql host>     DB_PORT=<port>
DB_USER=<user>   DB_PASSWORD=<pw>   DB_NAME=<database>
TUSHARE_TOKEN=...   TWELVEDATA_KEY=...
LARK_WEBHOOK=...    LARK_SECRET=...
FRED_API_KEY=...            # 可选，填了用真实 OAS
BASE_URL=https://quant.xiuqilai.com
TZ=Asia/Shanghai
```

## 6. 定时推送（cron）服务 【你做】
Zeabur 里**再建一个服务**，同一 Git 仓库，类型选 **Cron Job**：
- 启动命令覆盖为：`python notify_lark.py`
- 环境变量同第 5 步（复用）
- **两条计划（Zeabur cron 走 UTC，已换算）**：
  - `0 0 * * *`  → 北京 **08:00**
  - `30 8 * * *` → 北京 **16:30**

## 7. 绑域名 + 切 DNS 【你做】
1. Zeabur web 服务 → Domains → 添加 `quant.xiuqilai.com`，按提示拿到 CNAME/A 目标。
2. 域名商后台把 `quant.xiuqilai.com` 解析改到 Zeabur 给的目标（Zeabur 自动签 TLS）。

## 8. 验证 → 退役北京
- 访问 https://quant.xiuqilai.com 正常、手动触发一次 `notify_lark.py` 能推 Lark。
- 确认无误后停北京：`ssh ctj_xujz "docker rm -f quant-web"` + 删北京 crontab 里两条 notify 行。

---

**我能立刻代劳的**：第 4 步数据迁移（拿到密码后）、生成/核对环境变量清单、部署后端到端验证、退役北京。
**只有你能做的**：0 升配、1 GitHub 建仓推送、2/3/5/6 Zeabur 面板、7 DNS。
