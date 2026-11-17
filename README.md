# Dayseal（日封台）

收银流水按营业日归集，日结封账后锁定；长短款可查。

- 前端 http://127.0.0.1:5700
- API http://127.0.0.1:10700

## 流水营业日归属（全局唯一）

生效策略：**强制归入发生日**（`force_occurrence_day`）。系统只贯彻这一种策略，
不按营业日 / 收银机 / 导入来源混用；「拒收跨日」不在本代码库实现。

| 情形 | 请求 | 落库 `business_day` | 留痕 |
| --- | --- | --- | --- |
| `occurred_at` 发生日 = 声明 `business_day` | 成功 | 发生日（= 声明日） | 不写 |
| 发生日 ≠ 声明日 | **成功**，覆盖声明日 | 发生日 | 写 `attribution_corrections` |
| 已封账营业日的流水要再改归属 | 409 拒绝 | 不变 | — |

- 写入口（登记 `POST /api/lines`、导入 `POST /api/lines/import`、修改 `PATCH /api/lines/{id}`）
  全部经过同一裁决点 `app/attribution.py`，不允许各写各的折算。
- **唯一归属结果**：列表过滤、封账预览、批次合计、总览统计都只按**落库 `business_day`**
  归集，接口同时回传 `occurred_day` 与 `attribution_corrected` 供核对；
  不存在「列表按声明日展示、合计按发生日」的两套数。
- **分区键唯一**：落库 `business_day` 是全局唯一分区键，读路径不存在第二套键
  （既不看声明日、也不解析 `note` 里的标记）；声明日只进归属留痕表作审计附件。
- **矛盾行不得进新封**：预览把落库日 ≠ 发生日的行单列为 `conflicting_lines` 并置
  `sealable: false`，封账对同日直接 409（`attribution_conflict`），该行不会在两种
  日期口径下各计一次或都不计。已 sealed 分区的批次合计在任何归属修复后保持不变。
- **留痕必须与落库列一致**：自检接口报告 `trail_mismatches`（留痕的
  `stored_business_day` 与流水当前落库日不符，或留痕内部 `occurred_day` 自相矛盾）。
- 未给 `occurred_at` 时，只补当日时刻、不补日期（不会拿系统时钟偷偷改归属）。
- 导入是**整批一个事务**：任一行解析或封账校验失败，整批不落库，错误带行号。
- DB 触发器兜底：`cash_lines.business_day` 必须等于 `substr(occurred_at,1,10)`，
  绕过服务层直接写库也写不出脏行。
- 归属校正留痕 `GET /api/lines/corrections?business_day=YYYY-MM-DD`（按落库日过滤）；
  策略元数据 `GET /api/lines/attribution`。
- 旧库启动时自动收拢：`business_day` 与 `occurred_at` 不一致的历史行归入发生日并补写
  `repair` 留痕；已封分区保持不动。自检接口 `GET /api/close/integrity` 报告残留脏行、
  留痕与落库列不符、以及批次合计漂移（正常应为 `ok: true`）。

## 启动

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 10700 --app-dir .
```

```powershell
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 5700
```

## 测试

```powershell
cd backend
.\.venv\Scripts\pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest tests -q
```
