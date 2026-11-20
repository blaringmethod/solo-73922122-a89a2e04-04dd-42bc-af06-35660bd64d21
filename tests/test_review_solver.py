"""求解器纯函数单元测试(不需要数据库/API).

包含一个独立实现的暴力枚举作为对拍: 随机小规模用例下,
ReviewSolver 的最优值与逐份分类必须与暴力结果一致.

运行: python -m pytest tests/test_review_solver.py -v
"""
from __future__ import annotations

import itertools
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

from app.review import (  # noqa: E402
    BOTH,
    KEPT_IN_ALL,
    SHELVED_IN_ALL,
    LedgerPeriod,
    ReviewInput,
    ReviewSolver,
)

UTC = timezone.utc
T0 = datetime(2020, 1, 1, tzinfo=UTC)


def brute_force(periods: list[list[LedgerPeriod]], check_from, check_to, obs_times):
    """独立实现的暴力解: 返回 (min_shelved 或 None, {ledger_idx: 分类})."""
    n = len(periods)

    def clipped(p: LedgerPeriod):
        a, b = max(p.valid_from, check_from), min(p.valid_to, check_to)
        return (a, b, p.watershed) if a < b else None

    cp = [[c for c in (clipped(p) for p in ps) if c] for ps in periods]

    def feasible(S: frozenset) -> bool:
        kept = [seg for i in S for seg in cp[i]]
        # 同一时刻至多一个流域: 任意两条(含同一份台账内)重叠时段流域必须一致
        for (a1, b1, w1), (a2, b2, w2) in itertools.combinations(kept, 2):
            if max(a1, a2) < min(b1, b2) and w1 != w2:
                return False
        # 每条观测都被覆盖
        for t in obs_times:
            if not any(a <= t < b for a, b, _ in kept):
                return False
        return True

    best_sets: list[frozenset] = []
    best_r = -1
    for r in range(n, -1, -1):  # 从大到小: 第一个可行层即最优(保留最多)
        layer = [frozenset(c) for c in itertools.combinations(range(n), r)
                 if feasible(frozenset(c))]
        if layer:
            best_sets = layer
            best_r = r
            break
    if best_r < 0:
        return None, {}
    classes: dict[int, set[str]] = {i: set() for i in range(n)}
    for S in best_sets:
        for i in range(n):
            classes[i].add("keep" if i in S else "shelve")
    disp = {}
    for i in range(n):
        tags = classes[i]
        if tags == {"keep"}:
            disp[i] = KEPT_IN_ALL
        elif tags == {"shelve"}:
            disp[i] = SHELVED_IN_ALL
        else:
            disp[i] = BOTH
    return n - best_r, disp


def run_solver(periods, check_from, check_to, obs_times):
    solver = ReviewSolver(
        ReviewInput(
            check_from=check_from,
            check_to=check_to,
            ledger_ids=[f"L{i}" for i in range(len(periods))],
            periods=periods,
        )
    )
    for t in obs_times:
        solver.mark_observation(t)
    return solver.solve()


def test_random_against_brute_force():
    rng = random.Random(20261120)
    for case in range(400):
        n = rng.randint(0, 5)
        check_from = T0
        check_to = T0 + timedelta(days=rng.choice([30, 90, 365]))
        span = (check_to - check_from).total_seconds()
        periods = []
        for _ in range(n):
            ps = []
            for _ in range(rng.randint(0, 3)):
                a = T0 + timedelta(seconds=rng.uniform(-0.5, 1.2) * span)
                b = a + timedelta(seconds=rng.uniform(0.02, 0.6) * span)
                ps.append(LedgerPeriod(watershed=rng.choice(["W1", "W2", "W3"]),
                                       valid_from=a, valid_to=b))
            periods.append(ps)
        obs_times = sorted(
            T0 + timedelta(seconds=rng.uniform(0, 1) * span)
            for _ in range(rng.randint(0, 6))
        )
        want_min, want_disp = brute_force(periods, check_from, check_to, obs_times)
        got = run_solver(periods, check_from, check_to, obs_times)
        if want_min is None:
            assert not got.feasible, f"case {case}: 应无解但得到 {got}"
        else:
            assert got.feasible, f"case {case}: 应有解(min_shelved={want_min})"
            assert got.min_shelved == want_min, f"case {case}"
            got_disp = {i: got.dispositions[f"L{i}"] for i in range(n)}
            assert got_disp == want_disp, (
                f"case {case}: 分类不一致\nwant={want_disp}\ngot ={got_disp}"
            )
            # solution 必须真的是一个最优可行方案
            kept = [i for i in range(n) if got.solution[f"L{i}"] == "keep"]
            assert len(kept) == n - want_min


def test_no_ledgers_no_observations():
    res = run_solver([], T0, T0 + timedelta(days=1), [])
    assert res.feasible and res.min_shelved == 0 and res.solution == {}


def test_no_ledgers_with_observation_infeasible():
    res = run_solver([], T0, T0 + timedelta(days=1), [T0 + timedelta(hours=1)])
    assert not res.feasible
    assert res.uncovered and res.uncovered[0].observation_count == 1


def test_self_conflicted_ledger_must_be_shelved():
    bad = [
        LedgerPeriod("W1", T0, T0 + timedelta(days=10)),
        LedgerPeriod("W2", T0 + timedelta(days=5), T0 + timedelta(days=15)),
    ]
    good = [LedgerPeriod("W1", T0, T0 + timedelta(days=30))]
    obs = [T0 + timedelta(days=2), T0 + timedelta(days=20)]
    res = run_solver([bad, good], T0, T0 + timedelta(days=30), obs)
    assert res.feasible and res.min_shelved == 1
    assert res.self_conflicted == ["L0"]
    assert res.dispositions == {"L0": SHELVED_IN_ALL, "L1": KEPT_IN_ALL}


def test_both_classification():
    # L0 与 L1 同时段但流域冲突(只能留其一); L2 覆盖后半段(必留)
    half = timedelta(days=15)
    p = [LedgerPeriod("W1", T0, T0 + half)]
    q = [LedgerPeriod("W2", T0, T0 + half)]
    r = [LedgerPeriod("W2", T0 + half, T0 + 2 * half)]
    obs = [T0 + timedelta(days=1), T0 + timedelta(days=20)]
    res = run_solver([p, q, r], T0, T0 + 2 * half, obs)
    assert res.feasible and res.min_shelved == 1
    assert res.dispositions == {"L0": BOTH, "L1": BOTH, "L2": KEPT_IN_ALL}


def test_boundary_left_closed_right_open():
    # 观测恰在时段终点: 不算被该时段覆盖; 恰在起点: 算
    left = [LedgerPeriod("W1", T0, T0 + timedelta(days=10))]
    right = [LedgerPeriod("W1", T0 + timedelta(days=10), T0 + timedelta(days=20))]
    obs = [T0 + timedelta(days=10)]  # 只属于 right
    res = run_solver([left, right], T0, T0 + timedelta(days=20), obs)
    assert res.feasible and res.min_shelved == 0  # left 不冲突, 保留无代价
    assert res.dispositions["L1"] == KEPT_IN_ALL
    # 只有 left 时该观测无覆盖 -> 无解
    res2 = run_solver([left], T0, T0 + timedelta(days=20), obs)
    assert not res2.feasible


def test_sixteen_ledgers_performance():
    # 16 份台账 x 8 条时段: 完整枚举 2^16 也应在秒级完成
    import time

    periods = []
    for i in range(16):
        ps = []
        for k in range(8):
            a = T0 + timedelta(days=10 * k)
            ps.append(LedgerPeriod(f"W{(i + k) % 3}", a, a + timedelta(days=5)))
        periods.append(ps)
    obs = [T0 + timedelta(days=10 * k + 1) for k in range(8)]
    start = time.monotonic()
    res = run_solver(periods, T0, T0 + timedelta(days=90), obs)
    elapsed = time.monotonic() - start
    assert res.feasible
    assert elapsed < 5, f"求解过慢: {elapsed:.1f}s"
