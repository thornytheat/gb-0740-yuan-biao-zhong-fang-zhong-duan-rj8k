from app.modules import VIEWS


def test_each_independent_module(client):
    result = client.post("/api/plans", json={"sample": "nightly"}).json
    for route, (_, fields) in VIEWS.items():
        if route == "variance":
            # nightly 样例不带到达证据，差额说明页应明确 404，而不是产出半成品
            assert client.get(f"/api/{route}/{result['id']}").status_code == 404
            continue
        api = client.get(f"/api/{route}/{result['id']}")
        assert api.status_code == 200 and set(api.json) == set(fields)
        assert client.get(f"/console/{route}?plan={result['id']}").status_code == 200


def test_variance_module_end_to_end(client):
    result = client.post("/api/plans", json={"sample": "variance"}).json
    api = client.get(f"/api/variance/{result['id']}")
    assert api.status_code == 200
    assert set(api.json) == {"口径", "差额说明"}
    page = client.get(f"/console/variance?plan={result['id']}")
    assert page.status_code == 200 and "差额说明".encode() in page.data
    issued = client.post(f"/api/variance/{result['id']}/issue")
    assert issued.status_code == 201
    assert client.get("/api/variance/statements").json[0]["勾稽一致"] is True


def test_distinct_expected_results(client):
    clean = client.post("/api/plans", json={"sample": "clean"}).json
    night = client.post("/api/plans", json={"sample": "nightly"}).json
    assert clean["summary"]["待写"] == clean["rollback"]["演练写入"] == 2
    assert night["summary"]["受影响"] > 10
    assert night["rollback"]["恢复一致"]
