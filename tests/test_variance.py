import json
from io import BytesIO

import pytest

from app.config import ROOT
from app.keys import ReplayError
from app.modules.m7_release.service import compute, execute
from app.modules.m7_release.store import Catalog
from app.modules.m8_variance.service import basis_points, exact
from app.modules.m8_variance.store import StatementStore


@pytest.fixture(scope="session")
def variance_bundle():
    return (ROOT / "sample_data" / "variance.json").read_bytes()


@pytest.fixture(scope="session")
def variance_plan(variance_bundle):
    return compute(variance_bundle)


def _by_reason(plan):
    return {row["原因码"]: row for row in plan["差额说明"]["原因汇总"]}


def test_variance_total_ties_to_reconcile(variance_plan):
    check = variance_plan["分区勾稽"]
    variance = variance_plan["差额说明"]
    assert check["行数变化"] == "20"
    assert variance["守恒校验"]["差额合计"] == 20
    assert variance["守恒校验"]["勾稽一致"] is True
    assert variance["守恒校验"]["差异"] == 0


def test_all_reasons_present_with_share(variance_plan):
    reasons = _by_reason(variance_plan)
    assert set(reasons) == {
        "LATE_REVISION",
        "DUPLICATE",
        "KEY_MERGE",
        "KEY_COLLISION",
        "UNEXPLAINED",
    }
    bps = sum(row["占比基点"] for row in variance_plan["差额说明"]["原因汇总"])
    assert abs(bps - 10000) <= 4  # 基点按向零取整，舍入误差不超过条目数
    assert reasons["UNEXPLAINED"]["条数"] == 2


def test_partition_residual_and_owner(variance_plan):
    blocks = {row["分区"]: row for row in variance_plan["差额说明"]["分区差额"]}
    assert blocks["orders@2026-11-20"]["未解释余量"] == 4
    assert blocks["facts@2026-11-20"]["未解释余量"] == 6
    unexplained = [
        row for row in variance_plan["差额说明"]["明细"] if row["原因码"] == "UNEXPLAINED"
    ]
    assert all(row["责任"]["platform"]["责任方"] == "值班平台团队" for row in unexplained)
    assert variance_plan["差额说明"]["未解释余量"]["补数责任方"] == "值班平台团队"


def test_key_collision_never_charged_to_source_team(variance_plan):
    collisions = [row for row in variance_plan["差额说明"]["明细"] if row["原因码"] == "KEY_COLLISION"]
    assert collisions and all(row["来源团队"] == "buyers-team" for row in collisions)
    for row in collisions:
        assert row["责任"]["platform"]["责任方"] == "平台键治理团队"
        assert row["责任"]["source"]["责任方"] == "平台键治理团队"
        assert row["责任"]["platform"]["计入来源迟到"] is False
        assert row["责任"]["source"]["计入来源迟到"] is False
    # 来源团队的迟到/重复账里不得出现撞键记录；buyers-team 在来源口径只剩 KEY_MERGE 一条
    for owner in variance_plan["差额说明"]["责任汇总"]["source"]:
        if owner["责任方"] == "buyers-team":
            assert owner["条数"] == 1
    for owner in variance_plan["差额说明"]["责任汇总"]["platform"]:
        if owner["责任方"] == "平台键治理团队":
            assert owner["条数"] == len(collisions) + 1  # 改键合并也归平台键治理


def test_caliber_changes_owner_but_not_reason(variance_plan):
    merge = next(row for row in variance_plan["差额说明"]["明细"] if row["原因码"] == "KEY_MERGE")
    assert merge["责任"]["platform"]["责任方"] == "平台键治理团队"
    assert merge["责任"]["source"]["责任方"] == "buyers-team"
    assert merge["原因码"] == "KEY_MERGE"  # 口径切换不改原因
    late = next(row for row in variance_plan["差额说明"]["明细"] if row["原因码"] == "LATE_REVISION")
    assert late["责任"]["platform"]["责任方"] == late["责任"]["source"]["责任方"]


def test_each_line_carries_source_recompute_evidence(variance_plan):
    for row in variance_plan["差额说明"]["明细"]:
        if row["原因码"] == "UNEXPLAINED":
            continue
        evidence = row["复算证据"]
        assert evidence["来源原始行号"] == row["原始行号"]
        assert evidence["来源原始记录"] == row["原始记录"]
        assert "目标行数 110 − 基线行数 100 = 分区差额 10" in evidence["复算公式"]


def test_misdeclared_kind_is_rejected(variance_bundle):
    bundle = json.loads(variance_bundle)
    bundle["arrivals"][0]["kind"] = "duplicate"
    with pytest.raises(ReplayError) as exc:
        compute(json.dumps(bundle, ensure_ascii=False).encode())
    assert exc.value.code == "VARIANCE_EVIDENCE"


def test_baseline_mismatch_rejected(variance_bundle):
    bundle = json.loads(variance_bundle)
    bundle["baseline"][0]["rows"] = 99
    with pytest.raises(ReplayError) as exc:
        compute(json.dumps(bundle, ensure_ascii=False).encode())
    assert exc.value.code == "VARIANCE_BASELINE"


def test_retry_does_not_create_new_statement(tmp_path, variance_bundle):
    catalog = Catalog(tmp_path / "plans.sqlite")
    plan, reused = execute(catalog, variance_bundle)
    assert reused is False
    again, reused = execute(catalog, variance_bundle)
    assert reused is True and again["差额说明"]["说明编号"] == plan["差额说明"]["说明编号"]

    store = StatementStore(tmp_path / "variance.sqlite")
    first, created = store.issue(plan["差额说明"])
    second, created_again = store.issue(again["差额说明"])
    assert created is True and created_again is False
    assert first["说明编号"] == second["说明编号"]
    assert len(store.history()) == 1
    events = [row["事件"] for row in store.audit_trail()]
    assert events == ["ISSUE_CREATED", "ISSUE_REUSED"]


def test_statement_store_is_append_only_and_queryable(tmp_path, variance_plan):
    store = StatementStore(tmp_path / "variance.sqlite")
    statement, _ = store.issue(variance_plan["差额说明"])
    assert store.get(statement["说明编号"])["说明编号"] == statement["说明编号"]
    assert store.get_by_plan(variance_plan["批次"]["编号"])["说明编号"] == statement["说明编号"]
    events = {row["事件"] for row in store.audit_trail()}
    assert {"ISSUE_CREATED", "GET"} <= events
    # 账本物理表没有更新/删除通道
    import sqlite3

    with sqlite3.connect(store.path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(variance_statements)")}
        assert columns == {"id", "plan_id", "document", "created_at"}


def test_http_issue_idempotent_and_delete_refused(client, variance_bundle):
    run = client.post(
        "/api/plans", data={"bundle": (BytesIO(variance_bundle), "variance.json")}
    ).json
    plan_id = run["id"]
    first = client.post(f"/api/variance/{plan_id}/issue")
    second = client.post(f"/api/variance/{plan_id}/issue")
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json["说明编号"] == second.json["说明编号"]
    assert len(client.get("/api/variance/statements").json) == 1

    refused = client.delete(f"/api/variance/statement/{first.json['说明编号']}")
    assert refused.status_code == 405 and refused.json["code"] == "IMMUTABLE"
    # 拒绝删除后旧说明仍可回查
    assert client.get(f"/api/variance/{plan_id}").status_code == 200


def test_caliber_parameter_and_console(client, variance_bundle):
    run = client.post(
        "/api/plans", data={"bundle": (BytesIO(variance_bundle), "variance.json")}
    ).json
    ok = client.get(f"/api/variance/{run['id']}?caliber=source")
    assert ok.status_code == 200 and ok.json["口径"] == "source"
    bad = client.get(f"/api/variance/{run['id']}?caliber=finance")
    assert bad.status_code == 422 and bad.json["code"] == "CALIBER"
    page = client.get(f"/console/variance?plan={run['id']}&caliber=source")
    assert page.status_code == 200 and "来源口径".encode() in page.data


def test_large_counts_rendered_as_exact_strings():
    big = 2**53 + 5
    assert exact(big) == str(big) and isinstance(exact(big), str)
    assert basis_points(big, big * 2) == 5000
    assert basis_points(-3, 10) == -3000


def test_bundles_without_arrivals_have_no_variance(client):
    run = client.post("/api/plans", json={"sample": "clean"}).json
    pack = client.get(f"/api/export/{run['id']}").json
    assert pack["差额说明"] is None
    assert client.get(f"/api/variance/{run['id']}").status_code == 404