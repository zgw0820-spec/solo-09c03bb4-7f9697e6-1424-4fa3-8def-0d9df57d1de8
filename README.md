# 登录名排序规则迁移预检（utf8mb4_bin → utf8mb4_0900_ai_ci）

账户库准备把 `login_name` 的比较规则从 `utf8mb4_bin` 改为
`utf8mb4_0900_ai_ci`（大小写不敏感、重音不敏感，且为 `NO PAD`）。
本服务用 **FastAPI + MySQL 8** 对“拟修改后的整批登录名”做只读冲突预检：
枚举账户表上所有含登录名的普通唯一索引（含复合键、字符前缀），在 MySQL
内部按目标排序规则判定冲突，并按索引列出每个冲突组的账号整数主键。

---

## 1. 快速开始（Docker Compose）

```bash
cd <本仓库目录>
docker compose up --build
```

会启动：

- `mysql`：`mysql:8.0`，库名 `account_audit`，账号 `audit/auditpw`，
  自动执行 `docker/mysql/initdb/` 下的建表与示例数据；宿主机映射 `13306`。
- `api`：FastAPI/Uvicorn，宿主机访问 <http://127.0.0.1:8000>
  （交互式文档 <http://127.0.0.1:8000/docs>）。

健康检查：

```bash
curl -s http://127.0.0.1:8000/healthz
```

### 一条可直接试的预检请求

```bash
curl -sS -X POST http://127.0.0.1:8000/precheck \
  -H 'Content-Type: application/json' \
  --data @examples/precheck_request.json | python3 -m json.tool
```

`examples/precheck_request.json`：

```json
{
  "changes": [
    {"id": 1003, "login_name": "alice"},
    {"id": 1011, "login_name": "mallory"},
    {"id": 1012, "login_name": "MALLORY"}
  ]
}
```

真实返回节选（MySQL 8.0.43 实测）：

```json
{
  "status": "conflicts_found",
  "has_conflicts": true,
  "target_collation": "utf8mb4_0900_ai_ci",
  "total_accounts": 12,
  "submitted_changes": 3,
  "snapshot": {
    "isolation_level": "REPEATABLE-READ",
    "consistent_snapshot": true,
    "same_snapshot_for_all_indexes": true,
    "server_version": "8.0.43"
  },
  "index_names_checked": [
    "uk_login_name",
    "uk_login_prefix4",
    "uk_login_recovery",
    "uk_login_region",
    "uk_tenant_login"
  ],
  "conflicts_by_index": {
    "uk_login_name": [[1001, 1002, 1003], [1005, 1006], [1007, 1008], [1011, 1012]],
    "uk_login_prefix4": [[1001, 1002, 1003], [1005, 1006], [1007, 1008],
                         [1009, 1010], [1011, 1012]],
    "uk_login_recovery": [],
    "uk_login_region": [[1001, 1002], [1005, 1006], [1007, 1008], [1011, 1012]],
    "uk_tenant_login": [[1001, 1002, 1003], [1007, 1008], [1011, 1012]]
  },
  "total_conflict_groups": 16
}
```

`conflicts_by_index` 即“按索引 → 每个冲突组的账号主键数组”。
更详细的每组 `key_values`、是否涉及本次提交、NULL 跳过行数等见完整响应的
`indexes[]`。

### 未知主键：整批退回

```bash
curl -i -X POST http://127.0.0.1:8000/precheck \
  -H 'Content-Type: application/json' \
  --data @examples/precheck_unknown.json
```

```http
HTTP/1.1 404 Not Found
```

```json
{"code": "UNKNOWN_ACCOUNT_IDS", "unknown_ids": [999999], "submitted_count": 2}
```

只要有一个主键在快照中不存在，就返回 `404` 与全部 `unknown_ids`，
**不返回任何索引预检结果**（整批拒绝）。

### “只看整批改完后的最终值”：交换两个登录名不会误报

```bash
curl -s -X POST http://127.0.0.1:8000/precheck \
  -H 'Content-Type: application/json' \
  --data @examples/precheck_swap.json
```

把 `1011 dave → erin`、`1012 erin → dave` 整体交换，最终状态没有冲突，
因此不会出现 `[1011, 1012]` 组。实现上用
`COALESCE(临时表.new_login_name, 快照.login_name)` 直接构造“整批最终态”，
而不是逐行改写，避免了中间态误报。

空请求体 / `{}` / `{"changes": []}` 表示不改任何行，可用于纯存量数据的
迁移风险扫描。

---

## 2. 请求格式

主格式（整数主键 + 拟改登录名）：

```json
{"changes": [{"id": 1001, "login_name": "Alice"}]}
```

为方便值班员/脚本，还兼容：

```json
{"changes": {"1001": "Alice", "1002": "Bob"}}
[{"id": 1001, "login_name": "Alice"}]
{"1001": "Alice"}
{"account_ids": [1001], "login_names": ["Alice"]}
```

- `id` 必须是 JSON 整数（对象键允许数字字符串）；`true/false`、小数、
  非数字字符串一律 `422`。
- 同一批中 `id` 重复 → `422`。
- 登录名超过 `information_schema.COLUMNS.CHARACTER_MAXIMUM_LENGTH` → `422`。
- 请求体不是合法 JSON → `400`。

路径别名：`POST /precheck`、`POST /api/precheck`、
`POST /api/v1/collation-precheck`、`POST /accounts/collation-precheck`。
另有 `GET /indexes`（索引枚举结果）、`GET /healthz`（`/health`）。

---

## 3. 需求与实现要点对照

| 要求 | 实现 |
| --- | --- |
| 按整数主键提交拟改登录名，未知主键整批退回 | `parsing.py` 严格整数校验；同一快照内 `TEMPORARY TABLE LEFT JOIN account ... IS NULL` 找未知 id；有未知 id 即 `404 UNKNOWN_ACCOUNT_IDS`，不出报告 |
| 只使用整批改完后的值 | 每请求一个 UUID 临时表存放本批 `(id, new_login_name)`；候选关系为 `account a LEFT JOIN temp c`，登录名取 `COALESCE(c.new_login_name, a.login_name)`，只建模最终态 |
| 枚举所有含登录名的普通唯一索引 | 查 `information_schema.STATISTICS`，筛选 `NON_UNIQUE=0` 且任一列（忽略大小写）等于登录名列；自动包含复合索引与 `SUB_PART` 字符前缀 |
| 覆盖复合键、字符前缀 | 按 `SEQ_IN_INDEX` 还原全部键部分；前缀用 `LEFT(col, SUB_PART)`；复合键按全部键表达式 `GROUP BY` |
| 登录名按目标规则比较 | 仅对登录名表达式显式 `COLLATE utf8mb4_0900_ai_ci`（COALESCE 两侧都加，避免 collation mix） |
| 其余列沿用原规则 | 其他键部分直接用 `a.col` / `LEFT(a.col, n)`，继承列自身排序规则（如示例中 `region_code=utf8mb4_bin`，`US` 与 `us` 不冲突） |
| 含 NULL 的键跳过 | 外层 `WHERE k0 IS NOT NULL AND k1 IS NOT NULL ...` 后再分组；并返回每个索引的 `null_key_rows_skipped` |
| 在 MySQL 内判定冲突 | 冲突组、`COUNT(*)`、`JSON_ARRAYAGG(id)`、`JSON_ARRAY(键值)` 全部由 MySQL 的 `GROUP BY ... HAVING COUNT(*)>1` 完成，Python 只排序/整形 |
| 按索引列出每组冲突主键 | `conflicts_by_index[索引名] = [[id,...], ...]`；`indexes[].conflict_groups[]` 含完整明细 |
| 各索引用同一已提交快照 | 单连接内 `SET transaction_isolation='REPEATABLE-READ'` + `START TRANSACTION WITH CONSISTENT SNAPSHOT`，未知 id 校验、计数、所有索引查询都在同一读视图内，最后 `ROLLBACK` |
| 核查期间账户照常写入 | 全部为非锁定一致性读，不写账户表；自测中有并发写 `account` 的线程持续提交，预检始终成功且不被阻塞 |
| Docker Compose 启动 MySQL + API | 根目录 `docker-compose.yml`，含健康检查与依赖顺序；附 `examples/` 可试请求 |

> 排序规则的 `PAD` 属性也由 MySQL 负责：实测
> `utf8mb4_0900_ai_ci` 为 `NO PAD`（`'gina' <> 'gina '`），而
> `utf8mb4_bin` 为 `PAD SPACE`。在 SQL 中显式 `COLLATE` 目标规则后，
> 这些边界行为与真实建唯一索引时完全一致，无需在 Python 里重写比较规则。

### 生成的判定 SQL（示意）

对每个索引动态生成（标识符来自目录并做反引号转义，前缀长度强转整数，
排序规则名经白名单正则校验）：

```sql
SELECT JSON_ARRAYAGG(z.`id`) AS account_ids_json,
       COUNT(*)              AS group_size,
       JSON_ARRAY(z.`k0`, z.`k1`) AS group_key_json
FROM (
  SELECT a.`id` AS `id`,
         COALESCE(c.`new_login_name` COLLATE utf8mb4_0900_ai_ci,
                  a.`login_name`      COLLATE utf8mb4_0900_ai_ci) COLLATE utf8mb4_0900_ai_ci AS `k0`,
         a.`region_code` AS `k1`           -- 其他列保持原排序规则
  FROM `account_audit`.`account` a
  LEFT JOIN `_pc_changes_<uuid>` c ON c.`id` = a.`id`
) z
WHERE z.`k0` IS NOT NULL AND z.`k1` IS NOT NULL
GROUP BY z.`k0`, z.`k1`
HAVING COUNT(*) > 1;
```

---

## 4. 示例数据与基线预期

`docker/mysql/initdb/02_seed.sql` 的 12 个账号都满足当前 `utf8mb4_bin`
下的全部唯一索引，但在目标规则下基线（空批次）结果为：

| 索引 | 冲突组数 | 冲突主键组 | 专门验证点 |
| --- | ---: | --- | --- |
| `uk_login_name` | 4 | `[1001,1002]` `[1003,1004]` `[1005,1006]` `[1007,1008]` | 大小写、重音 |
| `uk_login_prefix4` | 5 | 上述 4 组 + `[1009,1010]` | `abcd1000/ABCD2000` 仅前 4 字符冲突，只有前缀索引报 |
| `uk_tenant_login` | 2 | `[1001,1002]` `[1007,1008]` | `1003/1004` 租户不同，不冲突 |
| `uk_login_region` | 3 | `[1001,1002]` `[1005,1006]` `[1007,1008]` | `region_code` 为 `utf8mb4_bin`，`us/US` 不合并 |
| `uk_login_recovery` | 0 | — | 4 行含 NULL 键部分，`null_key_rows_skipped=4` |
| `uk_recovery_email` | 不检查 | — | 不含 `login_name` |
| `idx_status` | 不检查 | — | 非唯一索引 |

---

## 5. 自测与验证结果

本环境没有 Docker/root，无法实际执行 `docker compose up`；因此用
**用户态 MySQL 8.0.43 通用二进制 + 本地 Uvicorn** 跑了同一套
`docker/mysql/initdb` 脚本和同一个 API 镜像内的代码，等价验证。

自动化用例 `tests/test_precheck.py`（13 项，全部通过）：

```text
13 passed in 2.43s
```

覆盖：健康检查/MySQL 8 版本；索引枚举（5 个目标索引、前缀长度 4、
复合键顺序、排除无关索引）；基线全部预期组；示例批次的“并入已有组 +
批内互撞 + 旧冲突消失”；交换改名的最终态语义；映射请求格式；未知主键
404 整批拒绝；重复 id / 布尔 id / 超长 / 非法 JSON；**并发写账户表期间
连续 8 次预检全部 200 且结果稳定**；直连 MySQL 验证
`START TRANSACTION WITH CONSISTENT SNAPSHOT` 后其他会话提交的新行在本
快照中不可见。另有 12 个并发 HTTP 预检请求全部 `200`。

额外做了“权威对照（oracle）”：把候选最终数据写入 scratch 库并把
`login_name` 直接改成 `utf8mb4_0900_ai_ci`，然后对每个索引执行真实的
`ALTER TABLE ... ADD UNIQUE KEY`。基线 / 示例批次 / 交换 三种场景下，
5 个索引“是否冲突”的结论与本预检**逐一完全一致**
（`REFERENCE_CHECK PASS`）。

在装有 Docker 的机器上可直接跑同一套测试：

```bash
docker compose up --build -d
# 宿主机：
API_BASE_URL=http://127.0.0.1:8000 DB_HOST=127.0.0.1 DB_PORT=13306 \
  pytest tests/
```

不用 Docker 的本地运行方式（自备 MySQL 8）：

```bash
mysql -uroot -p < docker/mysql/initdb/01_schema.sql
mysql -uroot -p account_audit < docker/mysql/initdb/02_seed.sql
python3 -m venv .venv && . .venv/bin/activate
pip install -r api/requirements.txt
DB_HOST=127.0.0.1 DB_PORT=3306 DB_USER=root DB_PASSWORD=... \
  DB_NAME=account_audit ACCOUNT_SCHEMA=account_audit \
  uvicorn app.main:app --app-dir api --host 127.0.0.1 --port 8000
```

> 测试在缺少 `httpx` 或连不上 MySQL 时会自动 skip，不会产生 collection error。

---

## 6. 配置（环境变量）

| 变量 | Compose 默认 | 说明 |
| --- | --- | --- |
| `DB_HOST/PORT/NAME/USER/PASSWORD` | `mysql/3306/account_audit/audit/auditpw` | MySQL 连接（该库级权限含建临时表） |
| `ACCOUNT_SCHEMA` / `ACCOUNT_TABLE` | `account_audit` / `account` | 账户库与表名 |
| `ID_COLUMN` / `LOGIN_COLUMN` | `id` / `login_name` | 整数主键列、登录名列 |
| `TARGET_COLLATION` | `utf8mb4_0900_ai_ci` | 拟迁移到的排序规则 |

---

## 7. 已知边界 / 待完成

1. **未在本沙箱实际运行 Docker Compose**（环境无 docker daemon 且无
   root）；Compose 文件仅做了 YAML 解析与字段核对，实际
   `docker compose up --build` 待在有 Docker 的环境执行。已用等价的
   MySQL 8.0.43 + Uvicorn 完成功能自测。
2. **函数索引 / 表达式键、引用 `login_name` 的生成列**：不属于“普通
   列唯一索引”，服务会把这类含登录名的唯一索引放入 `skipped_indexes`
   并注明原因，而不是给出可能错误的结论；当前没有解析表达式/重算生成列。
3. 预检是全表候选关系 + 分组，超大账户表上会有相应扫描/临时表开销；
   目前语句级读超时 120s，尚未做分批/采样或增量优化（预检要求全量准确）。
4. 未知主键当前返回 `404`；若团队规范希望用 `400/422`，改
   `app/main.py` 中一处状态码即可。
5. 尚未实现真正的 `ALTER TABLE ... MODIFY ... COLLATE ...` 迁移执行与
   回滚，也未做鉴权/审计落库——本服务定位为迁移前只读预检。
