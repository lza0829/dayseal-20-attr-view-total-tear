"""流水营业日归属（line-day attribution）的唯一事实来源。

每条流水带两类日期：

* ``occurred_at`` —— 事件实际发生时刻，由登记 / 导入 / 修改请求给出；
* ``business_day`` —— 请求声明要落到的营业日。

两者可能跨日。若照单全收，流水会落进错误的封账分区，列表过滤、封账预览、
批次合计各按一套日期算账。本模块把「发生日怎么折出来、和声明营业日比对、
最终落库哪个 ``business_day``」收成全局唯一裁决点。

全局互斥，只贯彻一种策略
------------------------
生效策略：**强制归入发生日**（``force_occurrence_day``）。

* 请求可以成功；落库 ``business_day`` 一律以 ``occurred_at`` 折出的发生日为准，
  覆盖请求声明的营业日，并写「归属校正」留痕（``attribution_corrections``）。
* 发生日与声明日一致时不写校正留痕。
* 列表过滤、封账预览、批次合计、总览统计全部只按**落库 business_day** 归集，
  不再回读请求声明日——禁止「列表按声明日展示、合计按发生日」的撕裂。
* 已封账营业日上的流水拒绝再改归属（服务层 409 + SQLite 触发器双保险）。

另一种策略「拒收跨日」（``reject``：发生日 ≠ 声明日即登记 / 导入 / 修改失败）
**没有实现**。两种策略互斥，不允许按营业日 / 收银机 / 导入来源混用；
本模块顶部的 ``ACTIVE_POLICY`` 是全系统唯一的开关。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from fastapi import HTTPException

# ---------------------------------------------------------------- 策略选择

POLICY_FORCE_OCCURRENCE_DAY = "force_occurrence_day"
POLICY_REJECT = "reject"

#: 全局唯一生效策略；另一种策略不实现，也不允许按天/按机切换。
ACTIVE_POLICY = POLICY_FORCE_OCCURRENCE_DAY

if ACTIVE_POLICY != POLICY_FORCE_OCCURRENCE_DAY:  # pragma: no cover - 防御性检查
    raise RuntimeError(
        f"未实现的流水归属策略：{ACTIVE_POLICY}；"
        f"本代码库只实现 {POLICY_FORCE_OCCURRENCE_DAY}"
    )

POLICY_LABEL = "强制归入发生日"
POLICY_DESCRIPTION = (
    "请求可成功，但落库 business_day 一律以 occurred_at 折出的发生日为准，覆盖声明日，"
    "并写归属校正留痕；封账预览、批次合计、列表过滤与总览都只按落库 business_day 归集。"
    "已封账营业日的流水禁止再改归属。"
)

#: 一切归集（列表、预览、批次、总览）的唯一依据。
AGGREGATION_BASIS = "stored_business_day"

#: 校正留痕里记录的动作。
ACTION_CREATE = "create"
ACTION_IMPORT = "import"
ACTION_PATCH = "patch"
ACTION_REPAIR = "repair"

#: 归属校正 / 归属巡检的两条数字都按落库 business_day 归集。


# ---------------------------------------------------------------- 日期解析


def _invalid(field: str, value: Any, hint: str) -> HTTPException:
    return HTTPException(
        400,
        {
            "code": "invalid_day_value",
            "field": field,
            "message": f"{field} 非法（{value!r}）：{hint}",
        },
    )


def normalize_day(value: Any, field: str = "business_day") -> str:
    """把营业日归一成 ``YYYY-MM-DD``；非法值抛 400。"""
    text = str(value or "").strip()
    if not text:
        raise _invalid(field, value, "不能为空，格式 YYYY-MM-DD")
    try:
        day = date.fromisoformat(text)
    except ValueError:
        raise _invalid(field, value, "必须是 YYYY-MM-DD 格式的日期") from None
    return day.isoformat()


def parse_occurred_at(value: Any, *, field: str = "occurred_at") -> datetime:
    """解析 ``occurred_at``；格式非法抛 400。

    要求日期前缀就是该时刻（按其自身时区）的日历日，落库后
    ``substr(occurred_at, 1, 10)`` 就是发生日——与 SQLite 触发器校验同一口径，
    避免「应用层折一次、库里再折一次」的两套数。
    """
    text = str(value or "").strip()
    if not text:
        raise _invalid(field, value, "必须是 ISO 8601 时间，如 2026-11-16T09:12:00Z")
    try:
        literal_day = date.fromisoformat(text[:10])
    except ValueError:
        raise _invalid(
            field, value, "必须是 ISO 8601 时间，如 2026-11-16T09:12:00Z"
        ) from None
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        moment = datetime.fromisoformat(candidate)
    except ValueError:
        raise _invalid(
            field, value, "必须是 ISO 8601 时间，如 2026-11-16T09:12:00Z"
        ) from None
    if moment.date() != literal_day:
        raise _invalid(
            field,
            value,
            "日期前缀必须是发生时刻自身时区下的日历日（扩展格式 YYYY-MM-DDThh:mm:ss）",
        )
    return moment


def occurrence_day(occurred_at: Any) -> str:
    """从 ``occurred_at`` 折出发生日（``YYYY-MM-DD``）。"""
    return parse_occurred_at(occurred_at).date().isoformat()


def try_occurrence_day(occurred_at: Any) -> str | None:
    """宽松版本：仅用于巡检历史数据，解析失败返回 ``None``。"""
    text = str(occurred_at or "").strip()
    try:
        if len(text) < 10:
            return None
        literal_day = date.fromisoformat(text[:10])
        candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        moment = datetime.fromisoformat(candidate)
        if moment.date() != literal_day:
            return None
        return literal_day.isoformat()
    except ValueError:
        return None


def complete_timestamp(declared_day: str) -> str:
    """未给 ``occurred_at`` 时，按声明营业日补一个当日时刻。

    不拿系统时钟当发生时刻：那会悄悄把「今天的登记」折到另一个日期。
    营业日由调用方声明，缺省时刻只补时间不补日期；所以这种补全不会产生校正。
    """
    now = datetime.now(timezone.utc)
    return f"{normalize_day(declared_day)}T{now.strftime('%H:%M:%S')}Z"


# ---------------------------------------------------------------- 唯一裁决


@dataclass(frozen=True)
class Resolution:
    """一次写入口的归属结果；落库、列表、预览、合计都只认它。"""

    business_day: str
    declared_business_day: str
    occurred_day: str
    occurred_at: str

    @property
    def corrected(self) -> bool:
        """落库日是否覆盖了声明日（发生日为准）。"""
        return self.business_day != self.declared_business_day

    @property
    def reason(self) -> str | None:
        if not self.corrected:
            return None
        return (
            f"发生日 {self.occurred_day} 与声明营业日 {self.declared_business_day} "
            "不一致，按全局策略归入发生日"
        )

    def as_meta(self) -> dict[str, Any]:
        return {
            "business_day": self.business_day,
            "declared_business_day": self.declared_business_day,
            "occurred_day": self.occurred_day,
            "occurred_at": self.occurred_at,
            "attribution_corrected": self.corrected,
            "attribution_policy": ACTIVE_POLICY,
            "attribution_reason": self.reason,
            "aggregation_basis": AGGREGATION_BASIS,
        }


def resolve(
    declared_business_day: Any,
    occurred_at: Any,
    *,
    timestamp_missing: bool = False,
) -> Resolution:
    """裁决一条流水的归属：``business_day`` = 发生日，声明日只作留痕。"""
    occurred_missing = timestamp_missing or occurred_at is None or not str(occurred_at).strip()
    if declared_business_day is None or str(declared_business_day).strip() == "":
        if occurred_missing:
            raise HTTPException(400, "必须提供 business_day 或 occurred_at 之一")
        declared = occurrence_day(occurred_at)
    else:
        declared = normalize_day(declared_business_day)

    if occurred_missing:
        occurred_text = complete_timestamp(declared)
    else:
        occurred_text = str(occurred_at).strip()

    occurred = occurrence_day(occurred_text)
    return Resolution(
        business_day=occurred,
        declared_business_day=declared,
        occurred_day=occurred,
        occurred_at=occurred_text,
    )


# ---------------------------------------------------------------- 元数据


def describe() -> dict[str, Any]:
    """暴露给前端 / 接口的策略元数据（全局唯一，不可切换）。"""
    return {
        "policy": ACTIVE_POLICY,
        "label": POLICY_LABEL,
        "description": POLICY_DESCRIPTION,
        "implemented": True,
        "aggregation_basis": AGGREGATION_BASIS,
        "enforced_on": ["create", "import", "patch"],
        "correction_log": "attribution_corrections",
        "alternatives": [
            {
                "policy": POLICY_REJECT,
                "label": "拒收跨日",
                "implemented": False,
                "reason": (
                    "与当前策略互斥；本代码库只在一种策略下运行，"
                    "不提供按营业日/收银机切换"
                ),
            }
        ],
        "rules": {
            "consistent": "发生日 = 声明日，落库 business_day = 发生日，不写校正留痕",
            "cross_day": "请求可成功，但落库 business_day 覆盖为发生日，并写归属校正留痕",
            "aggregation": "列表过滤、封账预览、批次合计、总览只按落库 business_day 归集",
            "sealed_day": "已封账营业日的流水拒绝再改归属（新增/修改/删除均 409）",
        },
    }
