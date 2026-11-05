# site_group_monitor（站群百度收录监控工具）

本地优先的站群监控工具：批量监控 **500+ 站点** 的

1. **百度首页收录状态**（模拟 `site:域名` 检索，三态判定）；
2. **TDK**（Title / Description / Keywords，含 GBK 编码兜底）；
3. **站点健康**（HTTP 状态码 / 响应时间 / SSL 证书 / 重定向链 / 混合内容 / DNS）；

定时跑批，产出**每日 diff 报告**（哪些被收录、哪些掉收录）、本地仪表盘、邮件推送与 HTML/Excel 导出。

> 形态：Python 单体本地工具 = **Click CLI** + **原生桌面窗口（pywebview + WebView2 封装的 ECharts 仪表盘）** + **内置 FastAPI 服务（仅绑 127.0.0.1）** + **SQLite**。
> 不依赖 Docker / Redis / Celery / 任何云服务，数据全部留在本机。
> **双击 `站群监控-v版本号.exe`（无参数）即进入图形界面**；命令行仍可用。

---

## 1. 快速开始

```bash
# ① 建虚拟环境（建议，避免污染系统 Python）
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate

# ② 安装依赖（可编辑安装，产出 sgm 命令）
pip install -r requirements.txt
pip install -e .

# ③ 看帮助
sgm --help
# 或（未安装 console script 时）
python -m site_group_monitor --help

# ④ 导入站点并跑一轮
sgm import sites.txt --format text          # 一行一个域名
sgm import sites.csv --format csv           # 表头：domain,group,tags,name,root_url,notes,enabled
sgm add example.com --group 主站 --tags "seo|核心"

# ⑤ 跑检测（首次会建立基线，不产生任何 diff 事件）
sgm check --full
sgm check --full --with-report             # 检测 + diff + 报告 +（可选）邮件

# ⑥ 出报告 / 起仪表盘
sgm report --date today --format all
sgm serve                                  # http://127.0.0.1:8080
```

### 图形界面（exe 双击即用）

打包产物如 `dist/站群监控-v0.1.4.exe`，是一个**原生桌面窗口程序**：双击即弹出窗口，内嵌 ECharts 仪表盘
（总览卡片 / 收录率趋势 / 掉收录图 / 站点表格 / TDK 详情抽屉），窗口顶部有
「开始检测」「下载 HTML / Excel」「刷新」与分组/状态/关键词筛选。

```bash
站群监控.exe            # 双击：无参数直接进入图形界面（推荐）
站群监控.exe gui        # 同上，显式进入图形界面
站群监控.exe --help     # 查看命令行用法（exe 同时保留全部 CLI 能力）
```

> 图形界面基于 **WebView2**（Windows 10/11 自带）。若系统未安装 WebView2 运行库，
> 程序会自动降级为「用默认浏览器打开仪表盘」并弹窗提示，不影响功能。
> 数据仍落在 exe 同级 `data/` 目录。

常用代理命令：

```bash
sgm proxy import proxies.txt               # 每行 protocol://user:pass@host:port
sgm proxy add --host 1.2.3.4 --port 8080 --username u --password p
sgm proxy test                             # 连通性测试（最多访问一次百度首页/代理）
sgm proxy health                           # 代理池健康度
```

---

## 2. 目录结构

```
site_group_monitor/
├─ config/                     配置模板（config.example.yaml / logging.yaml）
├─ data/                       运行期产物（.gitignore）
│  ├─ monitor.db               SQLite 主库（WAL）
│  ├─ config.yaml              用户级配置（首次运行自动从模板复制）
│  ├─ reports/                 HTML / Excel 报告
│  ├─ evidence/                百度结果页证据（gzip，30 天滚动）
│  └─ backups/                 每日 VACUUM INTO 备份（留 7 份）
├─ logs/                       滚动日志（sgm.log / sgm.error.log）
├─ site_group_monitor/
│  ├─ config/settings.py       配置加载（代码默认 < data/config.yaml < SGM_* 环境变量）
│  ├─ models/                  枚举与 Pydantic 实体
│  ├─ utils/                   域名归一化 / 编码探测 / 时间 / 导入 / 进度条 / 日志 / 异常
│  ├─ storage/                 schema.sql + database.py + repository.py（唯一 SQL 出口）
│  ├─ core/                    HTTP / UA 池 / 限速器 / 代理池 / 百度检测 / TDK / 状态 / Worker
│  ├─ services/                站点 / 审计 / 编排 / diff / 报告 / 告警（CLI 与 Web 共用）
│  ├─ scheduler/scheduler.py   APScheduler 每日流水线
│  ├─ report/                  Jinja2 HTML 报告 + openpyxl Excel 导出
│  ├─ notify/                  SMTP 邮件模板与发送
│  ├─ web/                     FastAPI + 原生 HTML/JS/CSS + 本地 vendor/echarts.min.js
│  └─ cli/                     Click 子命令（site / run / proxy 三组）
└─ tests/                      离线 fixture 驱动的单测（不联网）
```

---

## 3. 配置

优先级：**代码内 `DEFAULT_CONFIG` < `data/config.yaml` < 环境变量 `SGM_*`**（嵌套用双下划线）。

```bash
# 常用覆盖示例
export SGM_SMTP__PASSWORD=your-app-password
export SGM_BAIDU__MIN_INTERVAL_SEC=5          # 同一代理两次百度请求最小间隔
export SGM_CONCURRENCY__GLOBAL_WORKERS=4
export SGM_STORAGE__DB_PATH=D:/data/monitor.db
```

关键默认值（全部可配，默认值即可安全运行，不需要先拍板）：

| 项 | 默认值 | 说明 |
|---|---|---|
| `concurrency.global_workers` | 6 | 全局并发 |
| `concurrency.per_proxy_workers` | 1 | 单代理并发（防 IP 特征聚集） |
| `concurrency.batch_size` / `batch_pause_sec` | 50 / 20–40s | 批次错峰 |
| `engine.baidu.min_interval_sec` / `jitter` | 3.0s / 0.6–1.4× | 同代理最小间隔 |
| `engine.baidu.max_requests_per_proxy` / `rest_after_threshold_sec` | 25 / 45s | 单代理阈值与冷静期 |
| `engine.baidu.max_retry` | 2 | 暂态错误换代理重试 |
| `scheduler.daily_time` / `jitter_minutes` | 09:00 / 30 | 每日任务 + 随机抖动 |
| `smtp.enabled` | **false** | 邮件默认禁用（填凭据后启用） |
| `alert.index_rate_threshold` / `ssl_warn_days` / `unknown_persistent_days` | 0.70 / 15 / 3 | 告警阈值 |
| `alert.quiet_hours` / `dedup_hours` | 23:00–07:30 / 12 | 静默时段与去重冷却 |
| `retention.days` / `evidence_days` | 180 / 30 | 滚动保留 |
| `web.host` / `port` | 127.0.0.1 / 8080 | **安全默认值，拒绝 0.0.0.0** |

---

## 4. 收录判定（最重要的一条规则）

**三态**：`indexed`（已收录）/ `not_indexed`（未收录）/ `unknown_need_manual_check`（待人工确认）。

- 只有出现**明确空结果文案**（如「没有找到与…相关的网页」）才判 `not_indexed`；
- 只有结果 host 与目标**注册根域**匹配才判 `indexed`；
- **网络异常 / 验证码 / 结构不可识别 / 结果泛化 → 一律降级 `unknown`，绝不误判为未收录**；
- `unknown` **不计入收录率的分子分母**，也**不产生 diff 事件**（连续 ≥3 天才生成 `unknown_persistent` 告警）；
- 每次百度请求都会落证 `data/evidence/{domain}/{ts}.html.gz`，可用 `sgm analyze-snapshot` 离线重跑解析。

---

## 5. 报告与通知

- 每日流水线：`check → 快照 → diff → 报告/导出 → 告警 → 邮件 → 清理/备份`；
- HTML 报告为单文件离线可打开（趋势曲线用内联 SVG，无需联网）；
- Excel 含 4+ 个 Sheet：总览 / 明细 / 变化 / 待确认（有历史时追加趋势）；
- SMTP 未启用时只落本地报告，不影响任何功能；启用后支持 SSL(465) 与 STARTTLS(587)，失败按 30/90/180s 退避重试 3 次并写审计日志。

---

## 6. 合规与使用边界

- 本项目仅用于**自有/授权站点**的健康巡检，以**低频只读**方式检索（默认 < 3 请求/分钟/代理）；
- 请遵守目标站点 `robots.txt` 与服务条款，不要用于大规模爬取或规避风控；
- 建议配置 5–10 个可用代理：既能显著提速（500 站默认约 40 分钟），也能降低单 IP 封禁概率；
- 代理凭据仅本机明文存放，日志/报告/证据文件中**一律脱敏**（`***`），凭据不会随报告外发。

---

## 7. 测试与自验

```bash
pip install -r requirements-dev.txt
pytest -q                       # 全部离线，不发起真实网络请求
```

覆盖：百度解析器四类 fixture（命中 / 明确空结果 / 验证码 / 结构异常）、TDK 编码探测、
diff 与基线规则、Repository 幂等与聚合、FastAPI 冒烟。

---

## 8. 已知限制

1. **只实现百度引擎**（`SearchEngineChecker` 已抽象，搜狗/360 后续加子类即可）。
2. **验证码不接打码平台**：默认降级 `unknown` + 落证据；`playwright` 人工确认属 P2 可选（`requirements-optional.txt`）。
3. **TDK 默认纯静态解析**：JS 渲染站点可能抽不到；仅当开启 `tdk.render_fallback.enabled` 且域名在 `render_whitelist` 中才用 playwright 旁路，**绝不进主链路**。
4. **Windows 托盘 / 开机自启（R-022）本期不做**，需要常驻请用 `sgm serve`（内嵌 APScheduler）。
5. 仪表盘**无鉴权**（仅环回监听、单用户），共享部署需自行加反向代理与鉴权。
6. `web/static/vendor/echarts.min.js` 已本地内置（约 1MB，ECharts 5.5）；若缺失，页面会提示且表格数据仍可用。
7. 单轮 500 站耗时取决于代理数量与质量：无代理直连时约 70 分钟（并发降为 3、间隔 ×1.8）。
8. **百度风控会拦截无代理直连**：实测首次真实检索即触发 `百度安全验证`，此时系统按设计降级为
   `unknown_need_manual_check`（**不会误判为未收录**），并落证据到 `data/evidence/{domain}/`。
   生产使用务必配置代理池，否则报告里大部分站点会是「待人工确认」。

---

## 9. 交付验收记录（实测）

本节记录交付前的真实运行结果，便于复核。

| 项目 | 结果 |
|---|---|
| 测试套件 | **69 passed**（百度解析 15 / TDK 6 / diff 7 / Repository 10 / API 冒烟 11 / CLI 冒烟 5 / QA 回归 13，含 SSL/certifi、trust_env、TDK 动态导入、spec excludes），全部离线不联网 |
| 全模块导入 | 66 个 .py 文件，**0 ImportError** |
| CLI | `sgm --help` / `add` / `remove` / `list` / `import`（CSV 与纯文本）/ `check` / `report` / `serve` / `system-status` 全部实跑通过 |
| 域名归一化 | `https://WWW.Test-Site.COM/path?x=1` → `test-site.com`，三种写法重复添加仅保留 1 条 |
| 建表 | 7 张表幂等创建（`sites`/`check_results`/`daily_snapshots`/`proxies`/`change_events`/`audit_logs`/`runs`） |
| 导入性能 | **500 站 0.38s / 1000 站 0.64s**（PRD R-001 要求 500 条 < 10s，达标） |
| 真实检测 | 跑通完整链路；百度返回验证码 → 正确降级 `unknown`/`captcha`，同时正常取到 `http_status=200`、`response_ms=178`、`ssl_days_left=88` |
| 基线 | 首次检测写入 `is_baseline=1` 快照，**change_events = 0**（首次零误报）|
| diff 幂等 | 同一天重复跑 diff，第 2/3 次均返回 0 条，事件表只有 1 条 |
| 报告 | HTML + Excel（5 个 Sheet）同日落盘 |
| 仪表盘 | 页面 200，ECharts canvas 已渲染，总览卡片/趋势/表格/详情抽屉/筛选全部实跑；控制台 **0 JS 错误**；无横向溢出 |

### 交付前修复的关键缺陷（均已完成回归）

**第一轮（工程师代码从未运行过，问题集中爆发）**

| # | 问题 | 影响 |
|---|---|---|
| 1 | `database.init_db` 把 `executescript()` 套在 `tx()` 里 | SQLite 报 "no transaction is active"，**建库直接失败** |
| 2 | `baidu_parser` 用 `#content_left` 容器判断"有结果" | 空结果页被误判 → **未收录永远判不出来** |
| 3 | 规则顺序错误（小页面降级先于空文案判定） | 短空结果页降级 `unknown`，漏判 not_indexed |
| 4 | `diff` 比对时 `skip_baseline=True` | 第 2 天永远 diff 不出变化，要等第 3 天 |
| 5 | `get_container(settings)` 被当 FastAPI 依赖 | 所有 POST/PATCH 请求体被多包一层 `{payload,settings}`，**前端全挂** |
| 6 | `ReportService._html_writer` 属性与方法同名 | 实例属性遮蔽方法 → 导出报 `'NoneType' object is not callable` |
| 7 | `Container.reload()` 写 A 处读 B 处配置 | Web 设置页改配置**静默失效** |
| 8 | `CronTrigger(misfire_grace_time=...)` | 参数不属于 CronTrigger → **调度器启动即崩**，`serve` 起不来 |
| 9 | 抖动时先改 `minute` 再算 `hour` | 小时进位算错，触发时间偏移 |
| 10 | `StatusChecker.probe()` 缺 `return info` | 返回 None → 误标 `timeout`，`response_ms`/SSL 全空 |
| 11 | 站点列表筛选 `COUNT` 未拼 `post_where` | 筛选时报 `Incorrect number of bindings` |
| 12 | CLI 与子命令模块循环导入 | `sgm --help` 直接 ImportError |
| 13 | 顶层 `add/remove/list/import` 未注册 | 文档承诺的快捷命令不存在 |
| 14 | `upsert_by_domain` 把 `to_row()` 的 JSON tags 回灌实体 | pydantic 报 `Input should be a valid list` |
| 15 | `--db` 切换时未重建容器单例 | 换库不生效，仍读写上一个库 |
| 16 | SQL 左连返回 NULL 字段 | `Site.index_status/title` 校验失败 |

**第二轮（QA 对抗性验证暴露的边界问题）**

| # | 问题 | 影响 | 修复 |
|---|---|---|---|
| 17 | 域名含 tab/换行被 `urlsplit` 静默吞掉 | `example.com\tfoo` → 假域名 `example.comfoo`，污染站点库 | `strip_scheme_and_path` 遇到控制字符直接判无效 |
| 18 | 同日重复跑 diff 重复计事件 | 手动重跑 / 调度重试导致「新增收录 N 个」虚高 | `change_events` 加 `UNIQUE(site_id, event_type, stat_date)` 唯一索引 + `ON CONFLICT DO NOTHING` |
| 19 | **导入 500 站要 107 秒**（PRD 要求 <10s） | 大批量建站几乎不可用 | 根因是 WAL 库在 Windows 上每次 `close()` 触发检查点约 100ms，而每条记录要建 3 次连接。改为**按线程复用连接池** → **0.38 秒（提速 280 倍）** |
| 20 | `report --date abc` 裸抛 `ValueError` 堆栈 | 用户看到满屏 Traceback，不知道哪里错 | 新增 `is_date_key()` 校验，CLI 层抛 `click.BadParameter` 给出可读提示 |

> ⚠️ 注意：连接池修复（#19）踩过一次坑——最初做成全局共享连接池，导致
> `SQLite objects created in a thread can only be used in that same thread`，
> 7 个 API 测试挂掉。最终按 **线程隔离**（`threading.get_ident()` 为 key）才正确。
> 内存库（`:memory:`）单独用条件变量保证同一时刻只借给一个调用方，否则每次连接都是新空库。

### EXE 单文件交付（实测）

用户要求交付 Windows 单文件 `sgm.exe`，已用 PyInstaller onefile 打包并独立验收：

| 项目 | 结果 |
|---|---|
| 产物 | `dist/sgm.exe`，**32 MB** 单文件（从 72 MB 瘦身；含原生 GUI 所需的 pywebview/pythonnet，运行期 .NET/WebView2 取自系统，不进包） |
| 形态 | **无控制台窗口（console=False）**：双击 exe 直接进入原生桌面窗口（pywebview 封装的 ECharts 仪表盘，WebView2 后端）；CLI 能力（`sgm check/report/serve…`）仍保留，经管道可捕获 |
| 独立验收 | **32/0 PASS**（干净空目录跑 exe，不依赖源码目录；含 --help/--version/init/import/list/add/report/serve 全部子命令 + 仪表盘 200 + 静态资源 + 数据落 exe 同级） |
| 冻结校验 | 冻结后 `import webview`（pythonnet/clr_loader）成功、服务就绪、退出码 0（无显示环境用 `SGM_HEADLESS_GUI=1` 开关验证关键路径） |
| 冷启动 | 首次解压约 11–22 秒，后续运行秒级 |

**打包与运行时修复的 5 个关键缺陷（均已完成回归）**

| # | 问题 | 影响 | 修复 |
|---|---|---|---|
| 21 | playwright 的 node.exe（83 MB）被静态收集进包 | exe 虚胖到 72 MB 且启动慢 | `excludes` 剔除 playwright/PIL，TDK 解析改用 `importlib.import_module` 动态导入，体积降到 31 MB |
| 22 | Windows 默认 SSL 仅有 64 条系统 CA | 大量站点被误判 `tls_failed`（如 example.com 实测 ssl.ok=False） | `_build_ssl_context()` 用 `certifi` 的 165 条 CA 与系统 CA 取并集，实测 `example.com ssl.ok=True` |
| 23 | httpx 默认 `trust_env=True` | 环境里的 `HTTP_PROXY` 变量劫持直连请求，全部超时 | AsyncClient 加 `trust_env=False`，代理只走配置里的代理池 |
| 24 | 百度请求状态码写入了站点健康字段 `http_status` | 百度侧的 403/503 污染了站点可达性判断（误以为网站挂了） | 新增 `index_http_status` 列隔离，百度状态码单独存，不再覆盖 `http_status` |
| 25 | `settings._data_root()` 在源码运行时落到包目录 | 源码模式数据写错位置，与 exe 模式不一致 | 改为项目根 `parents[2]` 取数据目录；exe 模式走 `_MEIPASS` 同级 |
| 26 | 窗口化 exe（console=False）`sys.stdout/stderr` 为 `None`，uvicorn 默认 `DefaultFormatter` 调 `stream.isatty()` 崩 | 双击直接抛 `AttributeError: 'NoneType' object has no attribute 'isatty'`，原生窗口根本弹不出来 | 窗口化下把 `stdout/stderr` 重定向到 `data/logs/sgm-gui.log`；给 uvicorn 传自定义 `log_config`（标准 `Formatter`，不依赖 `isatty`/颜色）；源码模拟 `stdout=None` + 冻结 exe 双重验证通过 |
| 27 | `POST /api/system/run`（开始检测）在 `async` 路由里**同步跑完整流水线**，阻塞 uvicorn 事件循环 | 点「开始检测」报 `Failed to fetch`（请求挂到连接断开），期间全部 API 卡死 | 改为后台线程 `threading.Thread(target=scheduler.run_pipeline, daemon=True).start()` 启动即返回 `{"status":"started"}`；前端改成轮询 `/api/system/status` 显示进度（phase/done/total）+「取消检测」按钮（409 防重入）；源码 + 冻结 exe 实测 `/run` 秒回（13–70ms）、`/health` 秒回证明事件循环不再阻塞 |

> ⚠️ **生产必读**：未配置代理池直接请求百度，几乎必然触发验证码/风控，
> 报告里大部分站点会沦为 `unknown`（待人工确认）。正式批量跑前请务必在
> `data/config.yaml` 配 5–10 个可用代理（或 `proxies` 表导入），否则收录判定不可用。

**原生桌面窗口（GUI）说明**
- 双击 `sgm.exe`（或无参数 `sgm.exe gui`）即弹出窗口，内嵌已有 ECharts 仪表盘：总览卡片 / 收录率趋势 / 掉收录图 / 站点表格（含 TDK、SSL、状态码）/ 详情抽屉（TDK、证书、重定向链、事件），顶部有「开始检测」「下载 HTML / Excel」「刷新」与筛选，并新增「➕ 添加域名」按钮（弹窗支持批量粘贴域名、可选统一分组、显示成功/失败明细、完成后自动刷新列表与分组下拉），无需命令行即可录入站点。
- 窗口基于 **WebView2**（Windows 10/11 自带）。若系统缺 WebView2 运行库，自动降级为「默认浏览器打开仪表盘」并弹窗提示，功能不受影响。
- 窗口关闭即优雅退出（uvicorn 线程 + 调度器随进程结束回收，`_MEI` 临时目录正常清理）。
- 注意：窗口化 exe 收不到 Ctrl+C，若需用 `sgm serve` 起独立网页仪表盘，建议在普通控制台里跑（或直接从 GUI 窗口操作）；GUI 模式本身已覆盖全部可视化操作。



**代理配置 GUI（2026-10-29 交付）**
- 工具栏新增「⚙ 代理配置」按钮，弹窗内可视化维护代理池，三种来源全覆盖：
  1. **隧道代理**：服务商自动换 IP 的单入口，按一条代理录入（来源标记 `tunnel`）；
  2. **批量固定 IP**：粘贴多行 `host:port`（支持 `host:port:user:pass`、`scheme://user:pass@host:port`），逐条入库参与加权/轮询轮换（来源标记 `batch`）；
  3. **API 动态获取**：配置提取 API 的 URL/凭据/刷新间隔，手动或定时拉取 IP 列表入库（来源标记 `api`），已消失的 IP 自动软下线；每个 API 源可「立即刷新/删除」，并显示最近拉取数量与错误。
- 弹窗同时提供代理池总览（总数/启用/健康数）与明细表：脱敏地址、来源徽章、健康度、成功/失败计数，支持单个「启用/禁用/删除」。
- 后端新增接口：`POST /api/system/proxies/batch`（批量导入，幂等）、`GET/POST/DELETE /api/system/proxies/api-sources`（API 源管理，密码一律脱敏返回）、`POST /api/system/proxies/api-sources/{id}/refresh`（后台线程拉取，立即返回）。
- 数据库 schema 升级至 v4：`proxies` 表新增 `source_ref` 列 + 新增 `proxy_api_sources` 表；旧库启动时自动补列，无需手工迁移。
- 测试：`pytest -q` 77 passed（原 69 + 新增 8），GUI 启动注册 API 源刷新任务失败不影响主流程（try/except 包裹）。
