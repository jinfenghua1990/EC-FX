# 电商核销中心 V0.8.1 · 品牌返利按渠道配置

V0.8 把系统正式拆成“原始数据层 → 字段映射 → 标准数据层 → 算法层 → 报表层”。以后每个模块上传原始资料后，不再直接混进报表；只有完成字段映射并校验通过的标准数据才进入正式算法。

## V0.8.1 重点

- 品牌返利改成**渠道第一维度**：先选快手/抖音/拼多多等渠道，再填写该渠道返利项目和比例。
- 比例输入改成人类习惯的百分比：输入 `5` 就是 `5%`，系统内部保存为 `0.05`。
- 品牌作为可选二级筛选：留空/`*` 代表该渠道全部品牌。
- 同一渠道可以叠加多个返利组成项，例如运营费、广告费、物流费、年度返利。
- 月度试算结果新增**按渠道拆分**、按渠道组成项拆分、未匹配采购行统计。
- 采购标准数据将“采购渠道”设为必需字段，避免返利规则误套到其他渠道。

- 平台结算、物流、采购、库存、费用、垫付、银行全部增加统一的字段映射与标准化流程。
- 每个上传批次都有状态、原始行数、标准行数、异常行数和数据追溯。
- 原始文件和原始行永久保留；标准数据可重新生成，不破坏原始资料。
- 采购返利优先从标准化采购单计算，不再把 0610 采购表当长期主数据源。
- 品牌返利支持多个“返利组成项”叠加，并通过优先级避免同一组成项重复命中。
- 销售出库继续以旺店通为唯一主来源；手工调整新增“平台”范围和“乘系数”，原始旺店通数据仍不被覆盖。
- 统一端口保持 `8010`，模块使用固定 API 路径，不额外开放网络端口。

详细说明：`docs/v0.8-standardization-pipeline.md`

---

# 电商核销中心 V0.6.2 · GitHub API 在线更新版

## V0.6.2 重点

- 在线更新改为 **GitHub API + Fine-grained Token**，不再要求群晖绑定 Git 仓库、SSH Key 或 Deploy Key。
- `.env` 只需配置 `GITHUB_REPO` / `GITHUB_BRANCH` / `GITHUB_TOKEN`。
- 网页“检查在线更新”直接读取 GitHub `VERSION`；“下载并安装”直接下载 GitHub 分支 ZIP，然后交给群晖后台任务安全安装。
- “上传更新包”继续保留，作为 GitHub 网络异常时的备用。
- 仍然保护 `.env`、`data/`、`backups/`、数据库卷，不会被更新包覆盖。
- 无更新密码。

### `.env` 新增

```env
GITHUB_REPO=jinfenghua1990/ecommerce-reconciliation
GITHUB_BRANCH=main
GITHUB_TOKEN=github_pat_你的只读Token
```

> Token 只保存在群晖 `.env`，不要提交到 GitHub。

---

# 电商核销中心 V0.5 · 群晖正式运维版

这是从当前 V0.4 手工部署方式升级到“**GitHub 管代码 + 群晖一键更新**”的正式运维版。

> 目标：这一次升级仍需手动覆盖一次文件。完成 V0.5 + GitHub 绑定后，后续版本不再下载 ZIP、不再手动覆盖 `backend/`。

## V0.5 解决什么

- **依赖固化进 Docker 镜像**：容器启动不再执行 `pip install`，群晖重启不会再因为 PyPI 超时导致系统起不来。
- **GitHub 作为唯一代码主仓库**：程序代码、核销算法、页面和迁移都从 `main` 更新。
- **数据库 Alembic 迁移**：数据库字段升级有版本，不再靠临时 `ALTER TABLE`。
- **升级前自动备份**：每次更新先 `pg_dump`，同时备份 `data/`。
- **健康检查**：升级后必须 `/healthz` 正常才算成功。
- **失败自动恢复**：更新失败自动恢复升级前代码和数据库。
- **一键回滚上一版**：先备份当前数据，再恢复上一版本。
- **群晖任务计划支持**：可以在 DSM 任务计划程序里点“运行”完成更新/备份/回滚，不必每次进 Container Manager 改 YAML。
- **密钥继续只留群晖 `.env`**：旺店通 AppSecret 不进入 GitHub、不进入发布 ZIP。

## 当前核销算法

V0.5 完整继承 V0.4 / 0610 核销算法，包括：

- 8 折未税成本口径
- 0.52 / 0.65 店铺折扣
- PDD 格力高品牌过滤
- 快手 / 抖音 41% × 1.06 返利
- 仓储、物流、垫付、人员费用
- 原 TTL 口径 + 全渠道校正版
- 核销母表长原始订单号 `src_order_no` 扩容到 1024
- 旺店通正式授权 Adapter

## 目录结构

```text
/volume1/docker/ecommerce-reconciliation/
├── backend/                 # 程序代码（GitHub）
├── migrations/              # Alembic 数据库迁移（GitHub）
├── scripts/                 # 更新 / 备份 / 回滚脚本（GitHub）
├── docs/                    # 算法和架构说明（GitHub）
├── Dockerfile               # 镜像构建（GitHub）
├── docker-compose.yml       # 标准 Compose（GitHub）
├── compose.yaml             # 群晖 UI 使用的副本（不进 GitHub）
├── .env                     # 旺店通 / 本机配置（不进 GitHub）
├── data/                    # 原始文件与运行数据（不进 GitHub）
├── backups/                 # 自动备份（不进 GitHub）
└── .deploy/                 # 更新历史（不进 GitHub）
```

## 从你当前群晖 V0.4 升级到 V0.5（只做这一次）

你当前项目路径保持不变：

```text
/volume1/docker/ecommerce-reconciliation
```

### 1. 先停止项目

Container Manager → 项目 → `ecommerce-reconciliation` → 停止。

### 2. 覆盖 V0.6 文件

把 V0.6 压缩包解压后的**项目内部文件**上传到：

```text
/volume1/docker/ecommerce-reconciliation/
```

选择“合并 / 覆盖”。

V0.6 包**不包含真实 `.env`**，不会覆盖你群晖里的旺店通密钥；也不包含历史数据库卷。

### 3. 确认 `.env`

继续保留：

```env
APP_VERSION=0.6.1
APP_IMAGE_TAG=local
POSTGRES_PASSWORD=recon
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
PIP_DEFAULT_TIMEOUT=120
PIP_RETRIES=10

WDT_SID=jxf
WDT_APPKEY=jxf-gw
WDT_APPSECRET=这里必须是真实AppSecret
WDT_BASE_URL=https://api.wangdian.cn/openapi2/
WDT_TIMEOUT_SECONDS=30
RECON_AMOUNT_TOLERANCE=0.02
```

### 4. 群晖 YAML 整份替换

Container Manager → 项目 → `ecommerce-reconciliation` → YAML 配置，把内容替换为项目中的 `compose.synology.yml`（它与 `docker-compose.yml` 当前一致）。

关键变化是：

```text
以前：python:3.12-slim → 每次启动 pip install → uvicorn
现在：Dockerfile 构建一次依赖 → 容器启动直接 migrate → uvicorn
```

### 5. 构建 / 启动

第一次 V0.5 构建需要下载 Python 依赖，但**只在 Build 阶段执行一次**。以后普通重启不再下载依赖。

正常后：

```text
http://192.168.3.55:8010
http://192.168.3.55:8010/healthz
```

`/healthz` 会返回当前版本。

## GitHub 绑定（只做一次）

建议创建独立私有仓库：

```text
jinfenghua1990/ecommerce-reconciliation
```

仓库创建为空仓库后，在项目目录执行：

```bash
sh scripts/bootstrap-git.sh https://github.com/jinfenghua1990/ecommerce-reconciliation.git
```

如果机器安装了 GitHub CLI，也可：

```bash
sh scripts/publish-github.sh ecommerce-reconciliation --private
```

> 私有仓库在群晖执行 `git fetch` 需要 GitHub 凭证。可使用 SSH Deploy Key 或只读 PAT；不要把 PAT / AppSecret 写进仓库。

## 后续更新：一条命令

GitHub 有新版本后，在群晖项目目录运行：

```bash
sh scripts/update.sh
```

它自动执行：

```text
检查本地修改
→ git fetch
→ 备份 PostgreSQL + data/
→ fast-forward main
→ Docker build（requirements 未变时会走缓存）
→ Alembic upgrade head
→ 重启 app
→ /healthz 健康检查
→ 记录更新历史
```

如果任一步失败：

```text
停止新 app
→ 恢复升级前数据库备份
→ git reset 回旧 commit
→ 重建旧镜像
→ 恢复运行
```

## 群晖“点一下更新”（推荐）

DSM → 控制面板 → 任务计划程序 → 新增 → 触发的任务 → 用户定义的脚本。

运行用户建议 `root`。

### 更新系统

```bash
/bin/sh /volume1/docker/ecommerce-reconciliation/scripts/synology-task-update.sh >> /volume1/docker/ecommerce-reconciliation/backups/update.log 2>&1
```

任务名称可设为：`核销系统-更新`。

以后你只需要在任务计划程序里选中它 → **运行**。

### 每日备份

```bash
/bin/sh /volume1/docker/ecommerce-reconciliation/scripts/synology-task-backup.sh >> /volume1/docker/ecommerce-reconciliation/backups/backup.log 2>&1
```

建议每天凌晨执行一次，默认保留最近 14 份。

### 回滚上一版

```bash
/bin/sh /volume1/docker/ecommerce-reconciliation/scripts/synology-task-rollback.sh >> /volume1/docker/ecommerce-reconciliation/backups/rollback.log 2>&1
```

回滚前会先备份“当前数据库”；然后恢复上一次升级前数据库和代码，因此当前数据仍留有备份副本。

## 手工备份 / 恢复

备份：

```bash
sh scripts/backup.sh
```

恢复某一份数据库：

```bash
sh scripts/restore-db.sh backups/db_YYYYMMDD_HHMMSS.dump
```

## 数据库迁移

容器启动时自动运行：

```text
Alembic upgrade head
```

V0.4 及更早数据库没有 `alembic_version` 时，系统会识别现有业务表并先登记 baseline，再执行后续迁移，不会删除历史数据。

当前迁移：

- `0001_baseline`：V0.5/V0.6 共用基线
- `0002_expand_legacy_src_order_no`：原始订单号 160 → 1024

## 健康与版本

```text
GET /healthz
GET /api/status
```

例如：

```json
{"ok": true, "app": "电商核销中心", "version": "0.6.1"}
```

## GitHub CI

`.github/workflows/ci.yml` 会在 push / PR 时：

- 跑 0610 核销算法测试
- 跑导入测试
- 跑旺店通签名测试
- 连续执行两次 Alembic migration，验证幂等
- Build Docker 镜像

只有 GitHub 代码通过测试后，再让群晖更新。

## 安全约束

- `.env` 永远不提交 GitHub
- `data/`、`backups/`、`.deploy/` 永远不提交 GitHub
- AppSecret 只保存在群晖 `.env`
- PostgreSQL 只绑定群晖 `127.0.0.1:5433`
- Web 只开放 `8010`
- 不把 Docker Socket 挂给 Web 应用，因此没有在核销页面内直接执行 Docker 更新；更新放在 DSM 任务计划程序里更安全

## 测试结果

V0.5 本地验证：

```text
4 passed
Alembic fresh DB upgrade: passed
Alembic second upgrade (idempotent): passed
Shell syntax: passed
Python compile: passed
```

## V0.6 网页系统更新中心

右上角新增 **「系统更新」**。完成一次群晖任务计划配置后，后续升级可直接在网页完成：

- **检查在线更新**：项目绑定 GitHub 后读取 `origin/main` 是否有新提交；
- **下载并安装**：群晖后台自动 `git fetch/pull → 数据库备份 → Docker build → Alembic → 重启 → health check`；
- **上传更新包**：不依赖 GitHub，可把本项目格式的 ZIP 直接在网页上传并安装；
- **回滚上一版本**：恢复上一次升级前数据库备份与代码；
- **更新日志**：网页持续显示群晖宿主机执行日志。

### 只需一次的群晖设置

`.env` 增加管理员更新密码：

```env
```

DSM → 控制面板 → 任务计划 → 新增用户定义脚本，用户选择 `root`，每分钟执行：

```bash
/bin/sh /volume1/docker/ecommerce-reconciliation/scripts/synology-task-web-update.sh
```

网页不会直接控制 Docker：Web 容器只向 `data/system-update/` 写入请求，Docker/数据库操作由群晖 root 任务执行。这样无需把 `/var/run/docker.sock` 暴露给网页应用。

### 更新包保护规则

更新包不会覆盖 `.env`、`data/`、`backups/`、`.git/`、`compose.yaml`。安装前会自动备份 PostgreSQL 和当前代码；构建、迁移或健康检查失败时会尝试恢复旧版本。


## V0.6.1 变更

- 移除网页更新中心的更新密码要求。
- 局域网内可直接检查更新、上传更新包、立即更新和回滚。
- 修复版本号显示兜底，避免出现 `vundefined`。
- 注意：任何能访问 8010 的局域网设备都可操作更新中心。

## V0.7 模块化数据中心

新增 `/modules` 页面与 `/api/data/*` 稳定接口。所有模块共用 8010 端口：销售出库从旺店通自动同步；平台结算、物流、采购、库存、费用、垫付、银行流水上传到各自固定接口；品牌返利改为“采购单 × 可配置返利规则”计算。销售出库增加独立手工调整层，原始旺店通数据永远不被覆盖。详见 `docs/v0.7-module-hub.md`。


## V0.8.4 更新加固

- 在线更新继续使用 GitHub API + Token，不要求 SSH / Deploy Key / 本地 Git。
- 上传更新包和 GitHub API 在线更新统一走 staging，自动剥离 ZIP 最外层目录，避免“壳套壳”。
- 更新前自动备份代码与 PostgreSQL；成功后记录可回滚元数据，网页“回滚上一版本”对包更新/在线更新同样有效。
- 更新时先清理受管代码目录再复制新版，避免旧 Python/静态文件残留。
- 数据库迁移完成后强制校验 Alembic 当前 revision == 包声明 revision。
- 渠道返利：渠道精确匹配优先于通配规则；同渠道下品牌精确规则再优先于全部品牌规则；最后才比较 priority。
- 返利规则支持网页编辑，不必“停用旧规则 + 新建一条”才能改比例。
