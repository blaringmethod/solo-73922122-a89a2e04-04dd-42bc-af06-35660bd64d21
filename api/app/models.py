"""请求模型. 所有时刻必须为带时区的 ISO-8601(建议 UTC 'Z')."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Optional, Union

from pydantic import AwareDatetime, BaseModel, Field


def to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """归一化到 UTC; None 原样返回."""
    if dt is None:
        return None
    return dt.astimezone(timezone.utc)


class StationIn(BaseModel):
    station_id: str = Field(min_length=1, max_length=64)
    name: Optional[str] = None


class ObservationIn(BaseModel):
    station_id: str
    observed_at: AwareDatetime
    value: float


# ---------------------------------------------------------------------------
# 变更集操作(一笔事务内按序执行, 提交时统一校验)
# ---------------------------------------------------------------------------

class InsertPeriodOp(BaseModel):
    op: Literal["insert_period"]
    station_id: str
    watershed: str
    valid_from: AwareDatetime
    valid_to: Optional[AwareDatetime] = None


class UpdatePeriodOp(BaseModel):
    op: Literal["update_period"]
    period_id: int
    station_id: Optional[str] = None
    watershed: Optional[str] = None
    valid_from: Optional[AwareDatetime] = None
    valid_to: Optional[AwareDatetime] = None
    clear_valid_to: bool = False  # valid_to 未给出且此项为真时, 把终点改回 NULL(至今有效)


class DeletePeriodOp(BaseModel):
    op: Literal["delete_period"]
    period_id: int


class InsertObservationOp(BaseModel):
    op: Literal["insert_observation"]
    station_id: str
    observed_at: AwareDatetime
    value: float


class UpdateObservationOp(BaseModel):
    op: Literal["update_observation"]
    observation_id: int
    station_id: Optional[str] = None
    observed_at: Optional[AwareDatetime] = None
    value: Optional[float] = None


class DeleteObservationOp(BaseModel):
    op: Literal["delete_observation"]
    observation_id: int


Operation = Annotated[
    Union[
        InsertPeriodOp,
        UpdatePeriodOp,
        DeletePeriodOp,
        InsertObservationOp,
        UpdateObservationOp,
        DeleteObservationOp,
    ],
    Field(discriminator="op"),
]


class ChangeSet(BaseModel):
    """一笔事务: 操作按序执行, 中途可暂时违规, 提交时必须全部满足."""

    operations: list[Operation] = Field(min_length=1)


# ---------------------------------------------------------------------------
# 便捷操作请求体
# ---------------------------------------------------------------------------

class TransferRequest(BaseModel):
    """转属: 在 at 时刻截断当前时段, 同时开启归属新流域的时段."""

    at: AwareDatetime
    new_watershed: str = Field(min_length=1)


class SplitRequest(BaseModel):
    """拆分: 把包含 at 的时段在 at 处一分为二."""

    at: AwareDatetime
    right_watershed: Optional[str] = None  # 右半段流域, 缺省与原时段相同


class MergeRequest(BaseModel):
    """合并: 把在 at 处首尾相接的两个时段并为一段."""

    at: AwareDatetime
    watershed: Optional[str] = None  # 合并后流域, 缺省取左段流域


class CorrectRequest(BaseModel):
    """历史更正: 把 [valid_from, valid_to) 子区间改属另一流域(自动切分原时段)."""

    valid_from: AwareDatetime
    valid_to: AwareDatetime
    watershed: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# 台账复核(只读分析)
# ---------------------------------------------------------------------------

class LedgerPeriodIn(BaseModel):
    """台账中的一条归属时段: 左闭右开 [valid_from, valid_to), UTC 起止均必填."""

    watershed: str = Field(min_length=1, max_length=128)
    valid_from: AwareDatetime
    valid_to: AwareDatetime


class LedgerIn(BaseModel):
    """一份移交台账: 整份保留或整份搁置."""

    ledger_id: str = Field(min_length=1, max_length=64)
    periods: list[LedgerPeriodIn] = Field(default_factory=list, max_length=8)


class LedgerReviewRequest(BaseModel):
    """台账复核请求: 站号 + UTC 左闭右开核查区间 + 至多 16 份编号各异的台账."""

    station_id: str = Field(min_length=1, max_length=64)
    check_from: AwareDatetime
    check_to: AwareDatetime
    ledgers: list[LedgerIn] = Field(default_factory=list, max_length=16)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "station_id": "ST-1",
                    "check_from": "2020-01-01T00:00:00Z",
                    "check_to": "2021-01-01T00:00:00Z",
                    "ledgers": [
                        {
                            "ledger_id": "L-2020-A",
                            "periods": [
                                {"watershed": "WS-A",
                                 "valid_from": "2020-01-01T00:00:00Z",
                                 "valid_to": "2020-07-01T00:00:00Z"}
                            ],
                        },
                        {
                            "ledger_id": "L-2020-B",
                            "periods": [
                                {"watershed": "WS-B",
                                 "valid_from": "2020-06-01T00:00:00Z",
                                 "valid_to": "2021-01-01T00:00:00Z"}
                            ],
                        },
                        {
                            "ledger_id": "L-2020-FULL",
                            "periods": [
                                {"watershed": "WS-A",
                                 "valid_from": "2020-01-01T00:00:00Z",
                                 "valid_to": "2020-06-01T00:00:00Z"},
                                {"watershed": "WS-B",
                                 "valid_from": "2020-06-01T00:00:00Z",
                                 "valid_to": "2021-01-01T00:00:00Z"},
                            ],
                        },
                    ],
                }
            ]
        }
    }


class UncoveredRangeOut(BaseModel):
    """有观测但无任何台账时段覆盖的时间段."""

    interval_from: datetime
    interval_to: datetime
    observation_count: int


class LedgerDispositionOut(BaseModel):
    """单份台账在所有最优方案中的处置分类."""

    ledger_id: str
    disposition: Literal["kept_in_all_optimal", "shelved_in_all_optimal", "both"]


class LedgerReviewResponse(BaseModel):
    """复核结果. feasible=false 时 min_shelved/solution/ledgers 为 null, reasons 说明原因."""

    station_id: str
    check_from: datetime
    check_to: datetime
    observations_scanned: int
    feasible: bool
    min_shelved: Optional[int] = None
    solution: Optional[dict[str, Literal["keep", "shelve"]]] = None
    ledgers: Optional[list[LedgerDispositionOut]] = None
    self_conflicted: list[str] = Field(default_factory=list)
    uncovered_ranges: list[UncoveredRangeOut] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
