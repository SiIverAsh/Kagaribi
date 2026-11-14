# 🔥 Kagaribi（篝火）

> 篝火即烽燧，一站一堆火，远远一看就知道哪条线还通着。

**Kagaribi** 是独立于播放器的番剧采集源健康度验证探针，自动检测 [Predidit/KazumiRules](https://github.com/Predidit/KazumiRules) 规则存活性（兼容旧式 `api 1-8` XPath 规则与新式 `api 8-9` JSON API 规则），每 6 小时自动输出 `report.json` 与 Markdown 健康度榜单。

---

## 四级探针架构

前级失败或跳过时，后级自动标记为 `skipped`：

| 级别 | 名称 | 验证内容 | 技术栈 | 默认开关 |
| :--- | :--- | :--- | :--- | :---: |
| **L1** | `reachable` | `baseURL` DNS + TLS + `HTTP < 400`，且非 `<2KB` 域名停放页（`parked_domain`） | `httpx[http2]` | 开启 |
| **L2** | `search` | 多长青番剧关键词（海贼王 / EVA / 火影忍者）并集检索，至少命中 1 条非空标题与合法详情地址 | `httpx` + `lxml` / `jsonpath-ng` | 开启 |
| **L3** | `chapter` | 访问首条详情页或章节 API，解析出 `≥1` 条播放线路且线路内 `≥1` 集 | `httpx` + `lxml` / `jsonpath-ng` | 开启 |
| **L4** | `playable` | Headless 浏览器打开首集页面，仅拦截嗅探 `.m3u8` / `.mp4` 请求并立即中止下载 | `playwright`（可选） | `--deep` 开启 |

## 榜单状态说明

| 标识 | 状态 | 判定口径 |
| :---: | :--- | :--- |
| 🟢 | `healthy` | L1 + L2 + L3 全部通过（开启 `--deep` 时需 L4 同步通过） |
| 🟡 | `degraded` | 站点 L1 可达但 L2/L3 规则失效待修，或启用验证码保护（`antiCrawlerConfig.enabled=true` 自动标 `skipped`） |
| 🔴 | `dead` | L1 不可达（DNS 失效、TLS 握手失败、`HTTP ≥ 400`、超时或命中域名停放页） |

---

## 快速使用

### 1. 安装依赖

```powershell
uv sync
```

如需启用 `--deep`（L4 播放页嗅探）：

```powershell
uv sync --extra deep
uv run playwright install chromium
```

### 2. 运行探测

```powershell
# 直接探测本地 KazumiRules 目录（默认产出 report.json 与 report.md）
uv run kagaribi probe --rules /tmp/kazumi-rules

# 直接传入 Git 仓库 URL 自动浅克隆并探测
uv run kagaribi probe --rules https://github.com/Predidit/KazumiRules --out report.json

# 仅探测单个采集源
uv run kagaribi probe --rules /tmp/kazumi-rules --only LMM

# 开启 L4 深度直链嗅探
uv run kagaribi probe --rules /tmp/kazumi-rules --only LMM --deep
```

Windows PowerShell 下亦可直接通过项目虚拟环境调用：

```powershell
.\.venv\Scripts\python.exe -m kagaribi.cli probe --rules C:\tmp\kazumi-rules --out report.json
```

### 3. 运行单元测试

全部测试基于 `pytest` + `respx` 本地 Mock，零外网依赖：

```powershell
uv run pytest -v
```

---

## 探测边界与礼貌策略

- **只读验证**：仅校验规则解析链路是否畅通；**不下载视频流、不绕过验证码、不存储任何受版权保护的媒体内容**。
- **站点友好**：默认全局并发 `8`、单域名并发上限 `2`，请求间附带随机 Jitter，单源超时 `20s` 且仅对瞬态网络错误重试 1 次。
- **UA 策略**：优先使用规则内声明的 `userAgent` 与 `referer`，缺省回退至桌面端 Chrome UA。

