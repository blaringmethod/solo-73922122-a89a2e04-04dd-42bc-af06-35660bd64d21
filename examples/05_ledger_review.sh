#!/usr/bin/env bash
# 示例5: 旧移交台账复核 —— 求"搁置份数最少"的取舍方案(只读 API, 不写库)
#
# 场景: 质控员拿到 4 份编号各异的旧台账, 流域归属互有出入.
#   核查区间 [2020-01-01, 2021-01-01) 内该站有两条观测(2020-03-01, 2020-09-01).
#   台账 L-2020-Q1Q2 与 L-2020-H2 在 6 月重叠且流域冲突; L-2020-FULL 一份即可覆盖全年.
# 期望: 最优搁置 1 份(留 L-2020-H2 + L-2020-FULL), 并逐份给出"必留/必搁/皆可"分类.
set -euo pipefail

API=${API_URL:-http://localhost:8000}
SID=${SID:-ST-REVIEW-$(date +%Y%m%d%H%M%S)}

echo "== 站点: $SID =="

echo "-- 1. 建站, 写入归属时段与两条观测(库内现状)"
curl -sS -X POST "$API/stations" -H 'Content-Type: application/json' \
  -d "{\"station_id\":\"$SID\",\"name\":\"台账复核示例站\"}" | jq .
curl -sS -X POST "$API/changesets" -H 'Content-Type: application/json' -d "{
  \"operations\": [
    {\"op\":\"insert_period\",\"station_id\":\"$SID\",\"watershed\":\"WS-CURRENT\",
     \"valid_from\":\"2020-01-01T00:00:00Z\",\"valid_to\":\"2021-01-01T00:00:00Z\"},
    {\"op\":\"insert_observation\",\"station_id\":\"$SID\",\"observed_at\":\"2020-03-01T00:00:00Z\",\"value\":10.5},
    {\"op\":\"insert_observation\",\"station_id\":\"$SID\",\"observed_at\":\"2020-09-01T00:00:00Z\",\"value\":11.2}
  ]
}" | jq .

echo "-- 2. 台账复核: 4 份台账, 求搁置最少的取舍方案"
curl -sS -X POST "$API/ledger-review" -H 'Content-Type: application/json' -d "{
  \"station_id\": \"$SID\",
  \"check_from\": \"2020-01-01T00:00:00Z\",
  \"check_to\":   \"2021-01-01T00:00:00Z\",
  \"ledgers\": [
    {\"ledger_id\": \"L-2020-Q1Q2\", \"periods\": [
      {\"watershed\": \"WS-A\", \"valid_from\": \"2020-01-01T00:00:00Z\", \"valid_to\": \"2020-07-01T00:00:00Z\"}]},
    {\"ledger_id\": \"L-2020-H2\", \"periods\": [
      {\"watershed\": \"WS-B\", \"valid_from\": \"2020-06-01T00:00:00Z\", \"valid_to\": \"2021-01-01T00:00:00Z\"}]},
    {\"ledger_id\": \"L-2020-FULL\", \"periods\": [
      {\"watershed\": \"WS-A\", \"valid_from\": \"2020-01-01T00:00:00Z\", \"valid_to\": \"2020-06-01T00:00:00Z\"},
      {\"watershed\": \"WS-B\", \"valid_from\": \"2020-06-01T00:00:00Z\", \"valid_to\": \"2021-01-01T00:00:00Z\"}]},
    {\"ledger_id\": \"L-ARCHIVE-2018\", \"periods\": [
      {\"watershed\": \"WS-A\", \"valid_from\": \"2018-01-01T00:00:00Z\", \"valid_to\": \"2019-01-01T00:00:00Z\"}]}
  ]
}" | jq .
# 解读: L-2020-Q1Q2 与另两份在 6 月冲突 -> 每种最优方案都搁置;
#       L-2020-H2 / L-2020-FULL 每种最优方案都保留;
#       L-ARCHIVE-2018 不涉核查区间, 保留无代价 -> 每种最优方案都保留. min_shelved=1.

echo "-- 3. 无解情形: 下半年观测没有任何台账覆盖"
curl -sS -X POST "$API/ledger-review" -H 'Content-Type: application/json' -d "{
  \"station_id\": \"$SID\",
  \"check_from\": \"2020-01-01T00:00:00Z\",
  \"check_to\":   \"2021-01-01T00:00:00Z\",
  \"ledgers\": [
    {\"ledger_id\": \"L-ONLY-H1\", \"periods\": [
      {\"watershed\": \"WS-A\", \"valid_from\": \"2020-01-01T00:00:00Z\", \"valid_to\": \"2020-06-01T00:00:00Z\"}]}
  ]
}" | jq .
