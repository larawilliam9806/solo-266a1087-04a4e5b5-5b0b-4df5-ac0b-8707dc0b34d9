"""End-to-end tests for district-group subtotal reissue (片区行业小计补发)."""

from __future__ import annotations

import itertools
import threading

from fastapi.testclient import TestClient

from app import suppression

# Fixture A: 3x3 table, only the 2x2 top-left block is masked.
A = "2024Q1"
A_DISTRICTS = ["东城区", "西城区", "南城区"]
A_INDUSTRIES = ["制造业", "建筑业", "批发零售"]
A_VALUES = [
    [10, 20, 30],
    [20, 10, 30],
    [30, 40, 20],
]
A_MASK = [(0, 0), (0, 1), (1, 0), (1, 1)]

# Fixture B: 6x2 table, fully masked; used for laminar and interleaving tests.
B = "2024Q2"
B_DISTRICTS = ["第一区", "第二区", "第三区", "第四区", "第五区", "第六区"]
B_INDUSTRIES = ["制造业", "服务业"]
B_VALUES = [
    [10, 50],
    [20, 40],
    [30, 30],
    [40, 20],
    [50, 10],
    [60, 60],
]
B_MASK = [(i, j) for i in range(6) for j in range(2)]


def publish(client, quarter, districts, industries, values, mask):
    body = {
        "districts": [
            {"name": name, "total": sum(row)}
            for name, row in zip(districts, values)
        ],
        "industries": [
            {"name": name,
             "total": sum(values[i][j] for i in range(len(values)))}
            for j, name in enumerate(industries)
        ],
    }
    assert client.put(
        f"/api/v1/editor/quarters/{quarter}/totals", json=body
    ).status_code == 200
    assert client.post(
        f"/api/v1/editor/quarters/{quarter}/detail", json={"values": values}
    ).status_code == 200
    assert client.put(
        f"/api/v1/editor/quarters/{quarter}/mask",
        json={"cells": [list(c) for c in mask]},
    ).status_code == 200
    assert client.post(
        f"/api/v1/editor/quarters/{quarter}/publish", json={}
    ).status_code == 200


def publish_a(client, quarter=A):
    publish(client, quarter, A_DISTRICTS, A_INDUSTRIES, A_VALUES, A_MASK)


def publish_b(client, quarter=B):
    publish(client, quarter, B_DISTRICTS, B_INDUSTRIES, B_VALUES, B_MASK)


def reissue(client, quarter, industry, districts):
    return client.post(
        f"/api/v1/editor/quarters/{quarter}/subtotals",
        json={"industry": industry, "districts": districts},
    )


# ------------------------------------------------------------------ happy path

def test_reissue_computes_subtotal_of_the_group(client):
    publish_a(client)
    response = reissue(client, A, "制造业", ["东城区", "西城区"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["quarter"] == A
    assert body["industry"] == "制造业"
    assert body["districts"] == ["东城区", "西城区"]
    assert body["subtotal"] == 30          # 10 + 20
    assert body["created"] is True


def test_reissue_uses_the_published_detail_not_the_latest(client):
    publish_b(client)
    # A later detail revision must not change what a subtotal reports.
    shifted = [row[:] for row in B_VALUES]
    shifted[0][0], shifted[0][1] = 15, 45
    shifted[5][0], shifted[5][1] = 55, 65
    assert client.post(
        f"/api/v1/editor/quarters/{B}/detail", json={"values": shifted}
    ).status_code == 200
    response = reissue(client, B, "制造业", ["第一区", "第二区"])
    assert response.status_code == 200
    assert response.json()["subtotal"] == 30   # 10 + 20 as published, not 35


def test_reissue_is_idempotent_for_the_same_group(client):
    publish_a(client)
    first = reissue(client, A, "制造业", ["东城区", "西城区"])
    assert first.status_code == 200 and first.json()["created"] is True

    again = reissue(client, A, "制造业", ["西城区", "东城区"])  # any order
    assert again.status_code == 200
    body = again.json()
    assert body["created"] is False
    assert body["subtotal"] == first.json()["subtotal"]
    assert body["published_at"] == first.json()["published_at"]

    listed = client.get(f"/api/v1/bulletins/{A}/subtotals").json()
    assert len(listed["subtotals"]) == 1


def test_reissue_with_fully_visible_bulletin(client):
    publish(client, "2024Q4", A_DISTRICTS, A_INDUSTRIES, A_VALUES, [])
    response = reissue(client, "2024Q4", "制造业", ["东城区", "南城区"])
    assert response.status_code == 200
    assert response.json()["subtotal"] == 40   # 10 + 30, nothing to protect


# ------------------------------------------------------------- laminar groups

def test_overlapping_groups_must_be_nested(client):
    publish_b(client)
    assert reissue(client, B, "制造业", ["第一区", "第二区"]).status_code == 200
    # A superset contains the first group: allowed.
    assert reissue(
        client, B, "制造业",
        ["第一区", "第二区", "第三区", "第四区", "第五区", "第六区"],
    ).status_code == 200
    # Partial overlap is rejected.
    response = reissue(client, B, "制造业", ["第二区", "第三区"])
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "district_groups_overlap"
    conflicts = error["details"]["conflicts"]
    assert {tuple(c["districts"]) for c in conflicts} == {("第一区", "第二区")}
    # The same overlap is fine for a different industry of the quarter.
    assert reissue(client, B, "服务业", ["第二区", "第三区"]).status_code == 200


def test_disjoint_groups_coexist(client):
    publish_b(client)
    assert reissue(client, B, "制造业", ["第一区", "第二区"]).status_code == 200
    assert reissue(client, B, "制造业", ["第三区", "第四区"]).status_code == 200
    listed = client.get(f"/api/v1/bulletins/{B}/subtotals").json()
    assert len(listed["subtotals"]) == 2


# ------------------------------------------------------- confidentiality check

def test_subtotal_that_pins_masked_cells_is_rejected(client):
    publish_a(client)
    # 南城区's 制造业 cell is visible (30), so the group fixes the masked cell
    # (东城区, 制造业) to exactly 10 -- and the 2x2 block collapses with it.
    response = reissue(client, A, "制造业", ["东城区", "南城区"])
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "subtotal_breaks_protection"
    cells = {(c["row"], c["col"]) for c in error["details"]["cells"]}
    assert cells == {(0, 0), (0, 1), (1, 0), (1, 1)}
    # Nothing was published; the bulletin is untouched.
    assert client.get(f"/api/v1/bulletins/{A}").status_code == 200
    assert client.get(f"/api/v1/bulletins/{A}/subtotals").json() == {
        "quarter": A, "subtotals": []}


def test_check_covers_all_previously_published_subtotals(client):
    publish_b(client)
    assert reissue(client, B, "制造业", ["第一区", "第二区"]).status_code == 200
    # On its own this group would be safe; combined with the subtotal above
    # it pins (第三区, 制造业) and, through the row total, (第三区, 服务业).
    response = reissue(client, B, "制造业", ["第一区", "第二区", "第三区"])
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "subtotal_breaks_protection"
    cells = {(c["row"], c["col"]) for c in error["details"]["cells"]}
    assert cells == {(2, 0), (2, 1)}
    # The failed reissue changes nothing: bulletin and old subtotals remain.
    assert client.get(f"/api/v1/bulletins/{B}").status_code == 200
    listed = client.get(f"/api/v1/bulletins/{B}/subtotals").json()
    assert [s["districts"] for s in listed["subtotals"]] == [["第一区", "第二区"]]


def test_check_covers_subtotals_regardless_of_order(client):
    publish_b(client, quarter="2024Q3")
    # The larger group first, then the nested smaller one: same protection.
    assert reissue(
        client, "2024Q3", "制造业", ["第一区", "第二区", "第三区"]
    ).status_code == 200
    response = reissue(client, "2024Q3", "制造业", ["第一区", "第二区"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "subtotal_breaks_protection"


def test_subtotals_of_other_quarters_do_not_interfere(client):
    publish_b(client, quarter="2024Q3")
    publish_b(client, quarter="2024Q4")
    assert reissue(
        client, "2024Q3", "制造业", ["第一区", "第二区"]
    ).status_code == 200
    # The same group in another quarter is an independent release.
    assert reissue(
        client, "2024Q4", "制造业", ["第一区", "第二区"]
    ).status_code == 200
    # And the other quarter may still release the nested group afterwards.
    assert reissue(
        client, "2024Q4", "制造业", ["第一区", "第二区", "第三区"]
    ).status_code == 409


# ------------------------------------------------------------------ public API

def test_public_subtotal_query_by_quarter(client):
    publish_a(client)
    empty = client.get(f"/api/v1/bulletins/{A}/subtotals")
    assert empty.status_code == 200
    assert empty.json() == {"quarter": A, "subtotals": []}

    reissue(client, A, "制造业", ["东城区", "西城区"])
    reissue(client, A, "建筑业", ["东城区", "西城区", "南城区"])
    listed = client.get(f"/api/v1/bulletins/{A}/subtotals").json()
    assert listed["quarter"] == A
    assert [s["subtotal"] for s in listed["subtotals"]] == [30, 70]
    entry = listed["subtotals"][0]
    assert entry["industry"] == "制造业"
    assert entry["districts"] == ["东城区", "西城区"]
    assert set(entry) == {"industry", "districts", "subtotal", "published_at"}

    assert client.get("/api/v1/bulletins/2024Q4/subtotals").status_code == 404
    assert client.get(
        "/api/v1/bulletins/not-a-quarter/subtotals").status_code == 422


# ------------------------------------------------------------------ validation

def test_reissue_requires_a_published_quarter(client):
    response = reissue(client, "2024Q4", "制造业", ["东城区", "西城区"])
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "quarter_not_found"

    # Registered but not yet published.
    body = {
        "districts": [{"name": n, "total": sum(r)}
                      for n, r in zip(A_DISTRICTS, A_VALUES)],
        "industries": [{"name": n, "total": t} for n, t in
                       zip(A_INDUSTRIES, [60, 70, 80])],
    }
    assert client.put(
        f"/api/v1/editor/quarters/{A}/totals", json=body).status_code == 200
    response = reissue(client, A, "制造业", ["东城区", "西城区"])
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "bulletin_not_found"


def test_reissue_validates_the_request(client):
    publish_a(client)
    response = reissue(client, A, "不存在的行业", ["东城区", "西城区"])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_industry"

    response = reissue(client, A, "制造业", ["东城区", "不存在的区"])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_districts"

    # Fewer than two districts, duplicates, blanks: plain validation errors.
    assert reissue(client, A, "制造业", ["东城区"]).status_code == 422
    assert reissue(client, A, "制造业", ["东城区", "东城区"]).status_code == 422
    assert reissue(client, A, "制造业", []).status_code == 422


def test_reissue_requires_an_editor_token(client):
    publish_a(client)
    no_auth = TestClient(client.app)
    body = {"industry": "制造业", "districts": ["东城区", "西城区"]}
    url = f"/api/v1/editor/quarters/{A}/subtotals"
    assert no_auth.post(url, json=body).status_code == 401
    no_auth.headers.update({"Authorization": "Bearer nope"})
    assert no_auth.post(url, json=body).status_code == 403


# ------------------------------------------------------------------ concurrency

def test_interleaved_same_quarter_reissues_are_checked_in_order(client):
    """Two racing groups that are only safe alone: exactly one gets through."""
    publish_b(client)
    other = TestClient(client.app)
    other.headers.update({"Authorization": f"Bearer {client.editor_token}"})

    barrier = threading.Barrier(2)
    outcomes = []

    def fire(c, districts):
        barrier.wait(timeout=10)
        outcomes.append(reissue(c, B, "制造业", districts).status_code)

    threads = [
        threading.Thread(target=fire,
                         args=(client, ["第一区", "第二区"])),
        threading.Thread(target=fire,
                         args=(other, ["第一区", "第二区", "第三区"])),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(outcomes) == [200, 409]
    listed = client.get(f"/api/v1/bulletins/{B}/subtotals").json()
    assert len(listed["subtotals"]) == 1


def test_other_quarters_complete_while_one_quarter_is_checking(
        client, monkeypatch):
    publish_a(client)
    publish_b(client)
    started = threading.Event()
    release = threading.Event()
    real_check = suppression.reissue_check

    def slow_check(values, masked, constraints, row_totals, col_totals):
        if len(values) == 3:                 # quarter A's table
            started.set()
            assert release.wait(timeout=10)
        return real_check(values, masked, constraints, row_totals, col_totals)

    monkeypatch.setattr(suppression, "reissue_check", slow_check)

    outcomes = {}

    def slow_lane():
        outcomes["a"] = reissue(
            client, A, "制造业", ["东城区", "西城区"]).status_code

    thread = threading.Thread(target=slow_lane)
    thread.start()
    assert started.wait(timeout=10)
    # Quarter B's reissue finishes while quarter A is still being checked.
    assert reissue(client, B, "制造业", ["第一区", "第二区"]).status_code == 200
    release.set()
    thread.join(timeout=30)
    assert outcomes["a"] == 200


# ------------------------------------------------------- bounds cross-checks

def test_reissue_bounds_without_subtotals_match_cell_bounds():
    import random

    rng = random.Random(11)
    for _ in range(30):
        n, m = rng.randint(2, 4), rng.randint(2, 4)
        values = [[rng.randint(0, 5) for _ in range(m)] for _ in range(n)]
        mask_set = {(i, j) for i in range(n) for j in range(m)
                    if rng.random() < 0.5}
        if not mask_set:
            continue
        row_totals = [sum(r) for r in values]
        col_totals = [sum(values[i][j] for i in range(n)) for j in range(m)]
        got = suppression.reissue_bounds(values, mask_set, [],
                                         row_totals, col_totals)
        expected = {c: suppression.cell_bounds(values, sorted(mask_set), c)
                    for c in mask_set}
        assert got == expected


def test_reissue_bounds_match_brute_force():
    import random

    rng = random.Random(7)
    for _ in range(40):
        n, m = rng.randint(2, 3), rng.randint(2, 3)
        values = [[rng.randint(0, 3) for _ in range(m)] for _ in range(n)]
        cells = [(i, j) for i in range(n) for j in range(m)]
        mask_set = {c for c in cells if rng.random() < 0.5}
        if not mask_set or len(mask_set) > 4:
            continue
        constraints = []
        for j in range(m):
            family: list[set] = []
            for _ in range(rng.randint(0, 3)):
                rows = {i for i in range(n) if rng.random() < 0.6}
                if len(rows) < 2:
                    continue
                laminar = all(
                    rows <= old or old <= rows or not (rows & old)
                    for old in family
                )
                if laminar:
                    family.append(rows)
                    constraints.append((
                        j, frozenset(rows),
                        sum(values[i][j] for i in rows),
                    ))
        row_totals = [sum(r) for r in values]
        col_totals = [sum(values[i][j] for i in range(n)) for j in range(m)]
        got = suppression.reissue_bounds(values, mask_set, constraints,
                                         row_totals, col_totals)
        assert got == brute_bounds_with_subtotals(values, mask_set,
                                                  constraints)


def brute_bounds_with_subtotals(values, mask_set, constraints):
    """Enumerate every matrix matching margins, visible cells, subtotals."""
    n, m = len(values), len(values[0])
    rows = [sum(r) for r in values]
    cols = [sum(values[i][j] for i in range(n)) for j in range(m)]
    visible_row = [sum(values[r][c] for c in range(m)
                       if (r, c) not in mask_set) for r in range(n)]
    visible_col = [sum(values[r][c] for r in range(n)
                       if (r, c) not in mask_set) for c in range(m)]
    target_r = [rows[r] - visible_row[r] for r in range(n)]
    target_c = [cols[c] - visible_col[c] for c in range(m)]
    cells = sorted(mask_set)
    limit = max(rows + cols)
    out = {}
    for combo in itertools.product(range(limit + 1), repeat=len(cells)):
        assignment = dict(zip(cells, combo))
        if any(sum(assignment.get((r, c), 0) for c in range(m))
               != target_r[r] for r in range(n)):
            continue
        if any(sum(assignment.get((r, c), 0) for r in range(n))
               != target_c[c] for c in range(m)):
            continue
        satisfied = all(
            sum(assignment[(i, j)] if (i, j) in mask_set else values[i][j]
                for i in group) == subtotal
            for j, group, subtotal in constraints
        )
        if not satisfied:
            continue
        for cell, value in assignment.items():
            lo, hi = out.get(cell, (value, value))
            out[cell] = (min(lo, value), max(hi, value))
    return out
