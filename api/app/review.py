"""台账复核求解器(纯函数, 不依赖数据库).

问题: 旧移交台账(每份含若干左闭右开归属时段)的流域归属有出入.
在核查区间 [check_from, check_to) 内选择整份"保留/搁置":

  * 保留的时段可重叠, 但同一时刻至多一个流域(重叠时段流域必须一致);
  * 区间内该站每条观测都须被至少一份保留台账的时段覆盖;
  * 目标: 搁置份数最少(= 保留份数最多).

台账数 N <= 16, 每份时段 <= 8, 故用位掩码枚举全部 2^N 个子集,
找出所有最优(保留最多)子集, 并对每份台账给出分类:
  kept_in_all_optimal   —— 每种最优方案都保留
  shelved_in_all_optimal —— 每种最优方案都搁置
  both                  —— 两种处理都有可能

内存说明: 本模块状态量只与台账/时段数(有界: 16 份 x 8 条)相关,
与观测条数无关; 观测由调用方流式逐条喂入 mark_observation().
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# 分类取值(与 API 响应一致)
KEPT_IN_ALL = "kept_in_all_optimal"
SHELVED_IN_ALL = "shelved_in_all_optimal"
BOTH = "both"


@dataclass
class LedgerPeriod:
    """一条台账归属时段(已归一化到 UTC 的左闭右开区间)."""

    watershed: str
    valid_from: datetime
    valid_to: datetime


@dataclass
class ReviewInput:
    """求解输入: 核查区间 + 各台账时段."""

    check_from: datetime
    check_to: datetime
    ledger_ids: list[str]
    periods: list[list[LedgerPeriod]]  # 与 ledger_ids 对齐


@dataclass
class UncoveredRange:
    """有观测但无任何台账时段覆盖的基本区间."""

    interval_from: datetime
    interval_to: datetime
    observation_count: int


@dataclass
class ReviewResult:
    feasible: bool
    observations_scanned: int = 0
    min_shelved: Optional[int] = None
    solution: Optional[dict[str, str]] = None          # ledger_id -> "keep"/"shelve"
    dispositions: Optional[dict[str, str]] = None      # ledger_id -> 分类
    self_conflicted: list[str] = field(default_factory=list)  # 自身时段互相矛盾的台账
    uncovered: list[UncoveredRange] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


class ReviewSolver:
    """一次核查的状态机: 先登记台账, 再流式喂观测, 最后 solve().

    时间轴被所有台账时段端点切分为"基本区间"(<= 2*16*8+1 = 257 个),
    每个基本区间内"覆盖它的台账集合"恒定; 观测只需按所属区间计数,
    因此内存占用与观测条数无关.
    """

    def __init__(self, data: ReviewInput):
        if not data.check_from < data.check_to:
            raise ValueError("check_from must be before check_to")
        self.data = data
        self.n = len(data.ledger_ids)

        # --- 裁剪时段到核查区间 ---
        clipped: list[list[tuple[datetime, datetime, str]]] = []
        for periods in data.periods:
            ps = []
            for p in periods:
                a = max(p.valid_from, data.check_from)
                b = min(p.valid_to, data.check_to)
                if a < b:
                    ps.append((a, b, p.watershed))
            clipped.append(ps)

        # --- 基本区间划分 ---
        bounds = {data.check_from, data.check_to}
        for ps in clipped:
            for a, b, _ in ps:
                bounds.add(a)
                bounds.add(b)
        self.boundaries: list[datetime] = sorted(bounds)
        m = len(self.boundaries) - 1

        # cover[j]: 覆盖基本区间 j 的台账位掩码
        self.cover = [0] * m
        # ws[j][i]: 台账 i 在基本区间 j 内声明的流域集合
        ws: list[dict[int, set[str]]] = [dict() for _ in range(m)]
        for i, ps in enumerate(clipped):
            bit = 1 << i
            for a, b, w in ps:
                lo = bisect.bisect_left(self.boundaries, a)
                hi = bisect.bisect_left(self.boundaries, b)
                for j in range(lo, hi):
                    self.cover[j] |= bit
                    s = ws[j].get(i)
                    if s is None:
                        ws[j][i] = {w}
                    else:
                        s.add(w)

        # --- 冲突图(只与台账有关, 与观测无关) ---
        # conflict[i]: 与台账 i 在某时刻流域不一致的台账位掩码
        self.conflict = [0] * self.n
        # self_conflict: 同一台账在同一时刻声明了两个不同流域(任何方案都必须搁置)
        self.self_conflict = 0
        for j in range(m):
            items = list(ws[j].items())
            for i, s in items:
                if len(s) > 1:
                    self.self_conflict |= 1 << i
            for x in range(len(items)):
                ix, sx = items[x]
                for y in range(x + 1, len(items)):
                    iy, sy = items[y]
                    # 存在某时刻两台账流域不一致 <=> 流域集合不都是同一个单元素集
                    if not (len(sx) == 1 and len(sy) == 1 and sx == sy):
                        self.conflict[ix] |= 1 << iy
                        self.conflict[iy] |= 1 << ix

        # --- 观测流式状态(每基本区间一个计数器) ---
        self._counts = [0] * m
        self.observations_scanned = 0

    def mark_observation(self, observed_at: datetime) -> None:
        """登记一条核查区间内的观测(调用方保证 check_from <= t < check_to)."""
        j = bisect.bisect_right(self.boundaries, observed_at) - 1
        # t < check_to == boundaries[-1], 故 j 合法
        self._counts[j] += 1
        self.observations_scanned += 1

    def solve(self) -> ReviewResult:
        data = self.data
        n = self.n
        self_conflicted_ids = [
            data.ledger_ids[i] for i in range(n) if (self.self_conflict >> i) & 1
        ]

        # 需要覆盖的基本区间 -> 其覆盖台账位掩码
        required = [self.cover[j] for j in range(len(self._counts)) if self._counts[j] > 0]

        uncovered = [
            UncoveredRange(self.boundaries[j], self.boundaries[j + 1], self._counts[j])
            for j in range(len(self._counts))
            if self._counts[j] > 0 and self.cover[j] == 0
        ]
        if uncovered:
            total = sum(u.observation_count for u in uncovered)
            return ReviewResult(
                feasible=False,
                observations_scanned=self.observations_scanned,
                self_conflicted=self_conflicted_ids,
                uncovered=uncovered,
                reasons=[
                    f"{total} 条观测落在无任何台账时段覆盖的区间内, 无论如何取舍都无法覆盖"
                ],
            )

        # hitmask[i]: 台账 i 能覆盖的"需求区间"位图
        hitmask = [0] * n
        for j, mask in enumerate(required):
            mm = mask
            while mm:
                lsb = mm & -mm
                hitmask[lsb.bit_length() - 1] |= 1 << j
                mm ^= lsb
        all_required = (1 << len(required)) - 1

        # 枚举全部 2^n 子集: indep[S]=无冲突, cover[S]=需求覆盖位图
        size = 1 << n
        indep = bytearray(size)
        indep[0] = 1
        cover = [0] * size
        best_pop = -1
        for S in range(1, size):
            lsb = S & -S
            i = lsb.bit_length() - 1
            rest = S ^ lsb
            ok = (
                indep[rest]
                and not (self.self_conflict >> i) & 1
                and (self.conflict[i] & rest) == 0
            )
            indep[S] = 1 if ok else 0
            cover[S] = cover[rest] | hitmask[i]
            if ok and cover[S] == all_required and S.bit_count() > best_pop:
                best_pop = S.bit_count()
        if best_pop < 0 and all_required == 0:
            best_pop = 0  # 无观测需要覆盖时, 空集也可能是最优(全部台账自冲突)

        if best_pop < 0:
            return ReviewResult(
                feasible=False,
                observations_scanned=self.observations_scanned,
                self_conflicted=self_conflicted_ids,
                reasons=[
                    "台账间的流域冲突与观测覆盖要求无法同时满足: "
                    "覆盖全部观测所必需的台账组合必然引入同一时刻多个流域"
                ],
            )

        # 第二遍: 汇总所有最优子集, 对每份台账分类
        full = size - 1
        kept_any = 0    # 至少在一种最优方案中被保留
        shelved_any = 0  # 至少在一种最优方案中被搁置
        example: Optional[int] = None
        for S in range(size):
            if S.bit_count() != best_pop or not indep[S] or cover[S] != all_required:
                continue
            if example is None:
                example = S
            kept_any |= S
            shelved_any |= full ^ S
            if kept_any == full and shelved_any == full:
                break  # 全部台账都已确定为 both, 提前结束

        assert example is not None
        dispositions: dict[str, str] = {}
        for i in range(n):
            k = (kept_any >> i) & 1
            s = (shelved_any >> i) & 1
            if k and s:
                dispositions[data.ledger_ids[i]] = BOTH
            elif k:
                dispositions[data.ledger_ids[i]] = KEPT_IN_ALL
            else:
                dispositions[data.ledger_ids[i]] = SHELVED_IN_ALL

        solution = {
            data.ledger_ids[i]: ("keep" if (example >> i) & 1 else "shelve")
            for i in range(n)
        }
        return ReviewResult(
            feasible=True,
            observations_scanned=self.observations_scanned,
            min_shelved=n - best_pop,
            solution=solution,
            dispositions=dispositions,
            self_conflicted=self_conflicted_ids,
        )
