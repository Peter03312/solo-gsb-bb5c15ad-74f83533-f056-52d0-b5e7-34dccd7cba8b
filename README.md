# Alias Router — 邮件投递前别名路由判定服务

基于 **Python + FastAPI + SQLite** 的 HTTP API：维护一份路由草稿（精确地址规则 + 域兜底规则），
发布为不可变版本后，对给定收件地址做逐跳别名展开，返回去重后的终点邮箱及各自到达路径。

## 功能要点

- **规则模型**：每条规则匹配一个完整邮箱地址（`exact`）或整个域（`domain`），动作二选一：
  - `reject`：拒收（不可带目标）；
  - `route`：指向若干目标，目标分为 `alias`（内部别名，继续逐跳解析）与 `mailbox`（终点邮箱，不再递归）。
- **解析语义**：每一跳先查精确规则、再查域兜底；展开所有分支后按终点去重，返回每个终点的全部到达路径。
  任一分支**拒收 / 无路由 / 成环** → 整次解析失败（422），不返回部分结果。
- **草稿编辑**：整体替换，必须携带所见修订号（乐观锁），冲突返回 409。
  无效地址、重复规则、空目标（`route` 无目标 / `reject` 带目标）在编辑期即拒绝（422）。
- **发布**：在单个事务（`BEGIN IMMEDIATE`）内原子核对修订号，并校验**所有规则目标可解析且无环**；
  成功则生成不可变版本，失败则保持旧版本不变。
- **历史版本**：所有发布版本永久可查，按任意历史版本解析可复现当时的结果。
- **投递预览**：`POST /preview` 在同一读取快照内比较当前草稿与某个已发布版本，
  只列出投递结果有变化的地址与域兜底类别，并给出两侧完整结果；只读，不改动草稿或历史版本。

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

服务监听 `0.0.0.0:8000`，SQLite 数据落在命名卷 `router-data`（容器内 `/data/router.db`），
首次启动自动建表，无需手工初始化。

也可以只用 Docker：

```bash
docker build -t alias-router .
docker run -p 8000:8000 -v router-data:/data alias-router
```

## 本地开发启动

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000   # 默认库文件 ./router.db
pytest tests/ -q                                   # 运行测试
```

## 配置（环境变量）

| 变量          | 默认值（容器内）     | 说明                 |
| ------------- | -------------------- | -------------------- |
| `ROUTER_DB`   | `/data/router.db`    | SQLite 数据库文件路径 |
| `ROUTER_HOST` | `0.0.0.0`            | 监听地址             |
| `ROUTER_PORT` | `8000`               | 监听端口             |

地址与端口：`http://localhost:8000`（Compose 已映射 8000:8000），交互式文档见 `http://localhost:8000/docs`。

## API 一览

| 方法 | 路径              | 说明                                   |
| ---- | ----------------- | -------------------------------------- |
| GET  | `/health`         | 健康检查                               |
| GET  | `/draft`          | 查看草稿（含当前修订号）               |
| PUT  | `/draft`          | 整体替换草稿，需携带所见修订号         |
| POST | `/publish`        | 核对修订号 + 全量校验后发布不可变版本  |
| GET  | `/versions`       | 列出全部已发布版本                     |
| GET  | `/versions/{n}`   | 查看某个历史版本的完整规则             |
| POST | `/resolve`        | 按指定版本解析收件地址                 |
| POST | `/preview`        | 预览草稿相对基线版本的投递变化（只读） |

## 调用示例

### 1. 编辑草稿（当前修订号 0）

```bash
curl -X PUT http://localhost:8000/draft -H 'Content-Type: application/json' -d '{
  "revision": 0,
  "rules": [
    {"match_type": "exact", "match_key": "boss@corp.example.com", "action": "route", "targets": [
      {"kind": "alias",   "value": "assistant@corp.example.com"},
      {"kind": "mailbox", "value": "boss-backup@mail.example.com"}
    ]},
    {"match_type": "exact", "match_key": "assistant@corp.example.com", "action": "route", "targets": [
      {"kind": "mailbox", "value": "alice@mail.example.com"},
      {"kind": "alias",   "value": "team@corp.example.com"}
    ]},
    {"match_type": "exact", "match_key": "team@corp.example.com", "action": "route", "targets": [
      {"kind": "mailbox", "value": "alice@mail.example.com"},
      {"kind": "mailbox", "value": "bob@mail.example.com"}
    ]},
    {"match_type": "exact", "match_key": "blocked@corp.example.com", "action": "reject"},
    {"match_type": "domain", "match_key": "corp.example.com", "action": "route", "targets": [
      {"kind": "mailbox", "value": "catchall@mail.example.com"}
    ]}
  ]}'
# => {"revision": 1, "rules": [...]}
```

修订号过期时返回 `409`：`{"detail": {"error": "revision_conflict", "current_revision": 1, ...}}`。

### 2. 发布（原子核对修订号 + 校验目标可解析、无环）

```bash
curl -X POST http://localhost:8000/publish -H 'Content-Type: application/json' -d '{"revision": 1}'
# => {"version": 1, "source_revision": 1, "rule_count": 5}
```

校验失败（如目标别名无路由、成环）返回 `422` 且旧版本不受影响：

```json
{"detail": {"error": "validation_failed", "problems": [{"seed": "...", "error": "cycle", "chain": [...]}]}}
```

### 3. 解析收件地址

```bash
curl -X POST http://localhost:8000/resolve -H 'Content-Type: application/json' \
  -d '{"address": "boss@corp.example.com", "version": 1}'
```

```json
{
  "address": "boss@corp.example.com",
  "version": 1,
  "endpoints": [
    {"mailbox": "alice@mail.example.com", "paths": [
      ["boss@corp.example.com", "assistant@corp.example.com", "alice@mail.example.com"],
      ["boss@corp.example.com", "assistant@corp.example.com", "team@corp.example.com", "alice@mail.example.com"]
    ]},
    {"mailbox": "bob@mail.example.com", "paths": [
      ["boss@corp.example.com", "assistant@corp.example.com", "team@corp.example.com", "bob@mail.example.com"]
    ]},
    {"mailbox": "boss-backup@mail.example.com", "paths": [
      ["boss@corp.example.com", "boss-backup@mail.example.com"]
    ]}
  ]
}
```

域兜底（无精确规则时）：`nobody@corp.example.com` → `catchall@mail.example.com`。

任一分支失败则整次失败（`422`，无部分结果）：

```bash
curl -X POST http://localhost:8000/resolve -H 'Content-Type: application/json' \
  -d '{"address": "blocked@corp.example.com", "version": 1}'
# => {"detail": {"error": "rejected", "address": "blocked@corp.example.com", "chain": [...]}}
```

`error` 取值：`rejected`（命中拒收规则）、`no_route`（别名无路由）、`cycle`（成环）。

### 4. 历史版本查询与复现

```bash
curl http://localhost:8000/versions        # 全部版本
curl http://localhost:8000/versions/1      # 某版本的完整规则快照
```

草稿后续修改、发布新版本后，仍可用 `"version": 1` 调 `/resolve` 复现当时的判定结果。

### 5. 发布前预览：草稿相对某版本的投递变化

接着上面的例子：v1 已发布，管理员把 `blocked@corp.example.com` 从拒收改为投递
（草稿修订号 1 → 2），发布前先用 `/preview` 核对投递变化：

```bash
curl -X PUT http://localhost:8000/draft -H 'Content-Type: application/json' -d '{
  "revision": 1,
  "rules": [
    {"match_type": "exact", "match_key": "boss@corp.example.com", "action": "route", "targets": [
      {"kind": "alias",   "value": "assistant@corp.example.com"},
      {"kind": "mailbox", "value": "boss-backup@mail.example.com"}
    ]},
    {"match_type": "exact", "match_key": "assistant@corp.example.com", "action": "route", "targets": [
      {"kind": "mailbox", "value": "alice@mail.example.com"},
      {"kind": "alias",   "value": "team@corp.example.com"}
    ]},
    {"match_type": "exact", "match_key": "team@corp.example.com", "action": "route", "targets": [
      {"kind": "mailbox", "value": "alice@mail.example.com"},
      {"kind": "mailbox", "value": "bob@mail.example.com"}
    ]},
    {"match_type": "exact", "match_key": "blocked@corp.example.com", "action": "route", "targets": [
      {"kind": "mailbox", "value": "blocked@mail.example.com"}
    ]},
    {"match_type": "domain", "match_key": "corp.example.com", "action": "route", "targets": [
      {"kind": "mailbox", "value": "catchall@mail.example.com"}
    ]}
  ]}'

curl -X POST http://localhost:8000/preview -H 'Content-Type: application/json' \
  -d '{"baseline_version": 1, "revision": 2}'
```

响应只列出结果有变化的项，两侧完整结果一并给出（`baseline` / `draft`）：

```json
{
  "baseline_version": 1,
  "revision": 2,
  "changed_addresses": [
    {
      "address": "blocked@corp.example.com",
      "baseline": {"status": "failed", "error": {
        "error": "rejected", "message": "blocked@corp.example.com rejected by rule",
        "address": "blocked@corp.example.com", "chain": ["blocked@corp.example.com"]
      }},
      "draft": {"status": "ok", "endpoints": [
        {"mailbox": "blocked@mail.example.com",
         "paths": [["blocked@corp.example.com", "blocked@mail.example.com"]]}
      ]}
    }
  ],
  "changed_domain_fallbacks": []
}
```

语义说明：

- **探测范围**：两侧全部精确规则地址 ∪ 全部别名目标（与发布校验同一套种子），
  别名下游的变化会追溯到引用它的入口地址一并列出；两侧出现过域规则的每个域，
  排除前述具体地址后的兜底地址类别用一个避开两侧精确地址与别名目标的示例地址
  （如 `preview-probe@corp.example.com`）代表，变化列在 `changed_domain_fallbacks`。
- **比较口径**：沿用现有精确优先、逐跳展开、整次失败语义；终点、到达路径、
  拒收 / 无路由 / 成环任一不同即列出，路径排列顺序不计差异。
- **草稿未通过发布校验**（存在无路由 / 成环）时预览仍返回 `200`，
  相应地址的 `draft` 侧展示 `failed` 结果，便于发布前定位问题。
- **只读**：预览不改动草稿或历史版本。修订号过期返回 `409`
  （`revision_conflict`），基线版本不存在返回 `404`（`version_not_found`）。

## 项目结构

```
app/
  config.py    # 环境变量配置
  db.py        # SQLite 连接、建表、BEGIN IMMEDIATE 事务
  routing.py   # 地址校验、规则集、逐跳解析、发布前全量校验
  models.py    # 请求模型与编辑期校验（无效地址/重复规则/空目标）
  store.py     # 草稿与版本的持久化、修订号核对
  main.py      # FastAPI 路由
tests/         # pytest：API 全流程 + 解析单元测试
Dockerfile / docker-compose.yml
```
