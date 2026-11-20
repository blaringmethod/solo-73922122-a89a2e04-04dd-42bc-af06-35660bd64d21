"""台账复核端点 /ledger-review 集成测试.

需要运行中的 API 与数据库(默认 localhost:8000 / localhost:5432).
运行: API_URL=... DATABASE_URL=... python -m pytest tests/test_ledger_review.py -v
"""
from __future__ import annotations

import os
import uuid

import httpx
import psycopg
import pytest

API = os.environ.get("API_URL", "http://localhost:8000")
DSN = os.environ.get("DATABASE_URL", "postgresql://watershed:watershed@localhost:5432/watershed")


def new_sid() -> str:
    return "R-" + uuid.uuid4().hex[:12]


@pytest.fixture()
def client():
    with httpx.Client(base_url=API, timeout=60) as c:
        yield c


def mk_station_with_obs(client: httpx.Client, sid: str, obs_times: list[str],
                        period_from="2019-01-01T00:00:00Z", period_to="2022-01-01T00:00:00Z"):
    """建站 + 一个全覆盖归属时段 + 若干观测(供复核扫描)."""
    assert client.post("/stations", json={"station_id": sid}).status_code == 201
    ops = [{"op": "insert_period", "station_id": sid, "watershed": "WS-DB",
            "valid_from": period_from, "valid_to": period_to}]
    ops += [{"op": "insert_observation", "station_id": sid, "observed_at": t, "value": 1.0}
            for t in obs_times]
    r = client.post("/changesets", json={"operations": ops})
    assert r.status_code == 201, r.text


def review(client: httpx.Client, sid: str, ledgers: list[dict],
           check_from="2020-01-01T00:00:00Z", check_to="2021-01-01T00:00:00Z") -> dict:
    r = client.post("/ledger-review", json={
        "station_id": sid, "check_from": check_from, "check_to": check_to, "ledgers": ledgers,
    })
    assert r.status_code == 200, r.text
    return r.json()


def ledger(lid: str, *periods: tuple[str, str, str]) -> dict:
    return {"ledger_id": lid, "periods": [
        {"watershed": w, "valid_from": a, "valid_to": b} for w, a, b in periods
    ]}


def dispositions(body: dict) -> dict:
    return {d["ledger_id"]: d["disposition"] for d in body["ledgers"]}


# ---------------------------------------------------------------------------
# 最优解与逐份分类
# ---------------------------------------------------------------------------

def test_basic_min_shelved_and_classification(client):
    sid = new_sid()
    mk_station_with_obs(client, sid, ["2020-03-01T00:00:00Z", "2020-09-01T00:00:00Z"])
    body = review(client, sid, [
        ledger("L1", ("WS-A", "2020-01-01T00:00:00Z", "2020-07-01T00:00:00Z")),
        ledger("L2", ("WS-B", "2020-06-01T00:00:00Z", "2021-01-01T00:00:00Z")),
        ledger("L3", ("WS-A", "2020-01-01T00:00:00Z", "2020-06-01T00:00:00Z"),
               ("WS-B", "2020-06-01T00:00:00Z", "2021-01-01T00:00:00Z")),
    ])
    assert body["feasible"] is True
    assert body["observations_scanned"] == 2
    # L1 与 L2/L3 均在 6 月重叠且流域冲突; 唯一最优: 留 {L2, L3}
    assert body["min_shelved"] == 1
    assert body["solution"] == {"L1": "shelve", "L2": "keep", "L3": "keep"}
    assert dispositions(body) == {
        "L1": "shelved_in_all_optimal",
        "L2": "kept_in_all_optimal",
        "L3": "kept_in_all_optimal",
    }


def test_both_classification(client):
    """两份互相冲突但各自都能单独胜任的台账: 最优方案里留哪份都行."""
    sid = new_sid()
    mk_station_with_obs(client, sid, ["2020-03-01T00:00:00Z", "2020-09-01T00:00:00Z"])
    body = review(client, sid, [
        ledger("LA", ("WS-A", "2020-01-01T00:00:00Z", "2020-06-01T00:00:00Z")),
        ledger("LB", ("WS-B", "2020-01-01T00:00:00Z", "2020-06-01T00:00:00Z")),
        ledger("LC", ("WS-B", "2020-06-01T00:00:00Z", "2021-01-01T00:00:00Z")),
    ])
    assert body["min_shelved"] == 1
    assert dispositions(body) == {
        "LA": "both", "LB": "both", "LC": "kept_in_all_optimal",
    }
    # 返回的具体方案本身也要合法: LA/LB 恰留其一, LC 必留
    sol = body["solution"]
    assert sol["LC"] == "keep"
    assert {sol["LA"], sol["LB"]} == {"keep", "shelve"}


def test_same_watershed_overlap_is_not_conflict(client):
    """重叠但流域一致 -> 不冲突, 全部保留."""
    sid = new_sid()
    mk_station_with_obs(client, sid, ["2020-03-01T00:00:00Z"])
    body = review(client, sid, [
        ledger("L1", ("WS-A", "2020-01-01T00:00:00Z", "2020-08-01T00:00:00Z")),
        ledger("L2", ("WS-A", "2020-02-01T00:00:00Z", "2021-01-01T00:00:00Z")),
    ])
    assert body["min_shelved"] == 0
    assert dispositions(body) == {"L1": "kept_in_all_optimal",
                                  "L2": "kept_in_all_optimal"}


def test_free_ledger_kept_in_all_optimal(client):
    """不覆盖任何观测也不冲突的台账: 保留无代价, 每种最优方案都保留."""
    sid = new_sid()
    mk_station_with_obs(client, sid, ["2020-03-01T00:00:00Z"])
    body = review(client, sid, [
        ledger("USEFUL", ("WS-A", "2020-01-01T00:00:00Z", "2021-01-01T00:00:00Z")),
        ledger("EMPTY", ),  # 零时段
        ledger("OUTSIDE", ("WS-B", "2018-01-01T00:00:00Z", "2019-01-01T00:00:00Z")),
    ])
    assert body["min_shelved"] == 0
    assert dispositions(body) == {k: "kept_in_all_optimal"
                                  for k in ("USEFUL", "EMPTY", "OUTSIDE")}


def test_self_conflicted_ledger(client):
    """单份台账内部时段互相矛盾: 任何方案都必须搁置."""
    sid = new_sid()
    mk_station_with_obs(client, sid, ["2020-03-01T00:00:00Z", "2020-09-01T00:00:00Z"])
    body = review(client, sid, [
        ledger("BAD", ("WS-A", "2020-01-01T00:00:00Z", "2020-08-01T00:00:00Z"),
               ("WS-B", "2020-04-01T00:00:00Z", "2021-01-01T00:00:00Z")),
        ledger("GOOD", ("WS-A", "2020-01-01T00:00:00Z", "2021-01-01T00:00:00Z")),
    ])
    assert body["feasible"] is True
    assert body["min_shelved"] == 1
    assert body["self_conflicted"] == ["BAD"]
    assert dispositions(body) == {"BAD": "shelved_in_all_optimal",
                                  "GOOD": "kept_in_all_optimal"}


def test_boundary_semantics(client):
    """左闭右开: 观测恰在时段终点不被该时段覆盖; 恰在核查区间终点不参与核查."""
    sid = new_sid()
    mk_station_with_obs(client, sid, ["2020-06-01T00:00:00Z", "2021-01-01T00:00:00Z"])
    # 观测 2020-06-01 恰为 L-LEFT 终点 -> 不算覆盖; 须由 L-RIGHT 覆盖
    body = review(client, sid, [
        ledger("L-LEFT", ("WS-A", "2020-01-01T00:00:00Z", "2020-06-01T00:00:00Z")),
        ledger("L-RIGHT", ("WS-A", "2020-06-01T00:00:00Z", "2021-01-01T00:00:00Z")),
    ])
    assert body["feasible"] is True
    assert body["observations_scanned"] == 1  # 2021-01-01 恰在 check_to, 不在区间内
    assert dispositions(body)["L-RIGHT"] == "kept_in_all_optimal"

    # 只有 L-LEFT 时该观测无覆盖 -> 无解
    body2 = review(client, sid, [
        ledger("L-LEFT", ("WS-A", "2020-01-01T00:00:00Z", "2020-06-01T00:00:00Z")),
    ])
    assert body2["feasible"] is False
    assert body2["uncovered_ranges"][0]["observation_count"] == 1


# ---------------------------------------------------------------------------
# 无解
# ---------------------------------------------------------------------------

def test_infeasible_uncovered_observation(client):
    sid = new_sid()
    mk_station_with_obs(client, sid, ["2020-03-01T00:00:00Z", "2020-09-01T00:00:00Z"])
    body = review(client, sid, [
        ledger("L1", ("WS-A", "2020-01-01T00:00:00Z", "2020-06-01T00:00:00Z")),
    ])
    assert body["feasible"] is False
    assert body["min_shelved"] is None and body["solution"] is None
    assert body["ledgers"] is None
    assert body["reasons"]
    assert body["uncovered_ranges"] == [
        {"interval_from": "2020-06-01T00:00:00Z",
         "interval_to": "2021-01-01T00:00:00Z",
         "observation_count": 1}
    ]


def test_infeasible_conflict_vs_coverage(client):
    """两条观测分别只能由两份互相冲突的台账覆盖 -> 无解."""
    sid = new_sid()
    mk_station_with_obs(client, sid, ["2020-02-01T00:00:00Z", "2020-10-01T00:00:00Z"])
    body = review(client, sid, [
        ledger("L1", ("WS-A", "2020-01-01T00:00:00Z", "2020-08-01T00:00:00Z")),
        ledger("L2", ("WS-B", "2020-04-01T00:00:00Z", "2021-01-01T00:00:00Z")),
    ])
    assert body["feasible"] is False
    assert body["uncovered_ranges"] == []  # 不是无覆盖, 而是冲突与覆盖不可兼得
    assert body["reasons"]


def test_infeasible_no_ledgers_with_observations(client):
    sid = new_sid()
    mk_station_with_obs(client, sid, ["2020-03-01T00:00:00Z"])
    body = review(client, sid, [])
    assert body["feasible"] is False


def test_no_observations_in_interval_keep_all(client):
    """区间内无观测: 覆盖要求为空, 只要不冲突就全保留."""
    sid = new_sid()
    mk_station_with_obs(client, sid, ["2020-03-01T00:00:00Z"])
    body = review(client, sid, [
        ledger("L1", ("WS-A", "2020-01-01T00:00:00Z", "2021-01-01T00:00:00Z")),
        ledger("L2", ("WS-B", "2020-01-01T00:00:00Z", "2021-01-01T00:00:00Z")),
    ], check_from="2021-06-01T00:00:00Z", check_to="2021-07-01T00:00:00Z")
    # 注意: 两台账在核查区间外冲突不影响; 区间内无观测
    assert body["observations_scanned"] == 0
    assert body["feasible"] is True and body["min_shelved"] == 0


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------

def test_validation_errors(client):
    sid = new_sid()
    mk_station_with_obs(client, sid, [])
    base = {"station_id": sid,
            "check_from": "2020-01-01T00:00:00Z", "check_to": "2021-01-01T00:00:00Z"}

    def post(**kw):
        return client.post("/ledger-review", json={**base, **kw})

    assert post(ledgers=[ledger("X"), ledger("X")]).status_code == 422          # 编号重复
    assert post(check_from="2021-01-01T00:00:00Z",
                check_to="2020-01-01T00:00:00Z", ledgers=[]).status_code == 422  # 区间反向
    assert post(ledgers=[ledger(f"L{i}") for i in range(17)]).status_code == 422  # 超 16 份
    assert post(ledgers=[ledger("L1", *[( "WS-A", f"2020-01-{d:02d}T00:00:00Z",
                                          f"2020-01-{d:02d}T12:00:00Z")
                                        for d in range(1, 10)])]
                ).status_code == 422                                            # 超 8 条
    assert post(ledgers=[ledger("L1", ("WS-A", "2020-06-01T00:00:00Z",
                                       "2020-01-01T00:00:00Z"))]
                ).status_code == 422                                            # 时段反向
    r = client.post("/ledger-review", json={**base, "station_id": "NO-SUCH", "ledgers": []})
    assert r.status_code == 404                                                 # 站点不存在


# ---------------------------------------------------------------------------
# 只读与快照语义
# ---------------------------------------------------------------------------

def test_review_is_read_only(client):
    """复核不改变库内数据."""
    sid = new_sid()
    mk_station_with_obs(client, sid, ["2020-03-01T00:00:00Z"])
    before_p = client.get(f"/stations/{sid}/periods").json()
    before_o = client.get(f"/stations/{sid}/observations").json()
    review(client, sid, [ledger("L1", ("WS-A", "2020-01-01T00:00:00Z", "2021-01-01T00:00:00Z"))])
    assert client.get(f"/stations/{sid}/periods").json() == before_p
    assert client.get(f"/stations/{sid}/observations").json() == before_o


def test_review_sees_only_committed(client):
    """未提交事务的观测不参与核查(观测取自已提交状态)."""
    sid = new_sid()
    mk_station_with_obs(client, sid, ["2020-03-01T00:00:00Z"])
    holder = psycopg.connect(DSN)  # 默认 READ COMMITTED, 非 autocommit
    try:
        holder.execute(
            "INSERT INTO observations (station_id, observed_at, value)"
            " VALUES (%s, '2020-09-01 00:00+00', 9.9)", (sid,))
        body = review(client, sid, [
            ledger("L1", ("WS-A", "2020-01-01T00:00:00Z", "2021-01-01T00:00:00Z")),
        ])
        assert body["observations_scanned"] == 1  # 未提交的那条不可见
        holder.commit()
        body = review(client, sid, [
            ledger("L1", ("WS-A", "2020-01-01T00:00:00Z", "2021-01-01T00:00:00Z")),
        ])
        assert body["observations_scanned"] == 2
    finally:
        holder.close()


def test_memory_independent_of_observation_count(client):
    """大量观测: 结果正确且 API 进程 RSS 增长有界(流式扫描, 不整批载入)."""
    sid = new_sid()
    mk_station_with_obs(client, sid, [])
    n = 200_000
    with psycopg.connect(DSN) as c:
        c.execute(
            """
            INSERT INTO observations (station_id, observed_at, value)
            SELECT %s, timestamptz '2020-01-01 00:00+00' + (g || ' seconds')::interval, 1.0
            FROM generate_series(0, %s) g
            """,
            (sid, n - 1),
        )
        c.commit()

    rss_before = _api_rss_kb()
    body = review(client, sid, [
        ledger("L1", ("WS-A", "2020-01-01T00:00:00Z", "2021-01-01T00:00:00Z")),
        ledger("L2", ("WS-B", "2020-06-01T00:00:00Z", "2021-01-01T00:00:00Z")),
    ])
    rss_after = _api_rss_kb()

    assert body["observations_scanned"] == n
    assert body["feasible"] is True
    # L2 与 L1 在 [2020-06-01, 2021-01-01) 冲突; L1 单独即可全覆盖 -> 最优搁置 L2
    assert body["min_shelved"] == 1
    assert dispositions(body) == {"L1": "kept_in_all_optimal",
                                  "L2": "shelved_in_all_optimal"}
    if rss_before is not None and rss_after is not None:
        growth_mb = (rss_after - rss_before) / 1024
        # 流式扫描下增长应极小; 若整批 fetchall, 20 万行 dict 会增长数十 MB
        assert growth_mb < 48, f"API 进程 RSS 增长 {growth_mb:.1f} MB, 疑似非流式读取"


def _api_rss_kb():
    """读取本机 uvicorn(API)进程的 VmRSS(kB); 找不到返回 None(跳过内存断言).

    按 API_URL 的端口匹配进程, 避免命中同机其他 uvicorn 实例.
    """
    from urllib.parse import urlparse

    port = urlparse(API).port or 8000
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().decode(errors="ignore")
                if "uvicorn" in cmd and "app.main" in cmd and f"--port\x00{port}\x00" in cmd:
                    with open(f"/proc/{pid}/status") as f:
                        for line in f:
                            if line.startswith("VmRSS"):
                                return int(line.split()[1])
            except (FileNotFoundError, PermissionError):
                continue
    except FileNotFoundError:
        pass
    return None
