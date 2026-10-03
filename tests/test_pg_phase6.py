"""Mk13 Phase 6 — policy rows and skills on the shared Postgres.

What must hold for "a rule/skill added on one laptop is visible on all of them" to be safe:
1. equivalence — the enforcer on the database answers EXACTLY like the enforcer on policy.csv
                 for every (member x obj x act) we can name; the stored skills parse to the
                 same skills (and the same LLM-facing schemas) as the files;
2. seeding     — an empty database is seeded from the checkout once, even when several laptops
                 start together; a seeded database is never re-seeded;
3. propagation — a rule or skill written through one pool ("laptop A") is live for another
                 pool's enforcer / registry ("laptop B") on its next call, with no restart;
4. contract    — FilePolicyStore and PgPolicyStore behave the same (rules, has_row, add_rows
                 idempotency, version moves on change); malformed rows are refused;
5. sync-config — reports drift, adds missing rules, prunes only file-seeded rules (never ones
                 added from the dashboard), never overwrites a database skill unless asked;
6. cost        — steady-state skill loading is zero round trips; enforcement adds none;
7. wiring      — dashboard department registration / toolkit grants / skill editor write to the
                 database; a real run_agent executes a skill that exists only in the database.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from app.config import get_settings
from app.db import make_pool
from app.harness.enforcer import PolicyEnforcer
from app.harness.pg_policy import PgPolicyStore, file_rows, normalise, seed_policy
from app.harness.policy_store import MODEL_PATH, POLICY_PATH, FilePolicyStore, RulesAdapter
from app.llm.mock import MockLLM
from app.ops.schema import TraceEventType
from app.skills.model import parse_skill_document
from app.skills.pg_registry import PgSkillRegistry, db_path
from app.skills.registry import SkillRegistry
from tests.conftest import TEST_URL
from tests.test_agent import ALICE, BOB

ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = ROOT / "skills"

SKILL_DOC = """---
name: {name}
trigger: "run the {name} procedure"
scope: dept/marketing
owner_role: marketing_agent
version: {version}
args_schema:
  q:
    type: string
    description: "query"
steps:
  - look up q
tool_calls:
  - tool: crm_search_customer
    args:
      query: "{{{{q}}}}"
---

Body v{version}.
"""


def skill_doc(name: str, version: int = 1) -> str:
    return SKILL_DOC.format(name=name, version=version)


def laptop(schema: str, tag: str, size: int = 2):
    """Another process's pool on the same database (its own caches, its own connections)."""
    return make_pool(TEST_URL, kwargs={"options": f"-c search_path={schema}"}, max_size=size, name=f"{schema}-{tag}")


def members_version(pool) -> int:
    with pool.connection() as conn:
        return int(conn.execute("SELECT value FROM meta WHERE key = 'members_version'").fetchone()["value"])


@pytest.fixture
def skills_copy(tmp_path) -> Path:
    dst = tmp_path / "skills"
    shutil.copytree(SKILLS_DIR, dst)
    return dst


@pytest.fixture
def policy_copy(tmp_path) -> Path:
    dst = tmp_path / "policy.csv"
    shutil.copy(POLICY_PATH, dst)
    return dst


@pytest.fixture(params=["file", "postgres"])
def policy_store(request, policy_copy):
    if request.param == "postgres":
        if not TEST_URL:
            pytest.skip("set TEST_DATABASE_URL (make test-pg) for Postgres tests")
        pool, _admin, _schema = request.getfixturevalue("pg")
        return PgPolicyStore(pool, seed_from=policy_copy, version_ttl=0)
    return FilePolicyStore(policy_copy)


# ==================================================================== 1. equivalence


def _every_question():
    """Every (sub, dom, obj, act) worth asking: each demo member in their own and in every other
    domain, against every obj/act the policy mentions plus deliberately unknown ones."""
    from app.auth.users import get_store

    rules = FilePolicyStore(POLICY_PATH).rules()["p"]
    objs = sorted({r[2] for r in rules} | {"unknown_obj", "skill_nope"})
    acts = sorted({r[3] for r in rules} | {"delete", "nope"})
    doms = sorted({r[1] for r in rules} | {"nowhere"})
    subs = [m.username for m in get_store().list()] + ["stranger@acme.com"]
    return [(s, d, o, a) for s in subs for d in doms for o in objs for a in acts]


def test_database_enforcer_answers_exactly_like_the_file(pg):
    pool, _admin, _schema = pg
    on_file = PolicyEnforcer(policy_path=POLICY_PATH)
    on_db = PolicyEnforcer(policy_store=PgPolicyStore(pool, version_ttl=3600))  # nothing changes mid-test
    questions = _every_question()
    assert len(questions) > 5_000  # non-vacuous
    # the built Casbin instances (the version guard is covered by the propagation tests below)
    diffs = [q for q in questions if on_file._enforcer.enforce(*q) != on_db._enforcer.enforce(*q)]
    assert diffs == []
    assert sum(on_db._enforcer.enforce(*q) for q in questions) > 100  # and it does grant things


def test_seeded_rules_are_the_files_rules_in_order(pg):
    pool, _admin, _schema = pg
    store = PgPolicyStore(pool)
    assert store.rows() == file_rows(POLICY_PATH)
    assert store.rules() == {"p": [list(r[1:]) for r in file_rows(POLICY_PATH)]}


def test_stored_skills_equal_the_files(pg, skills_copy):
    pool, _admin, _schema = pg
    disk = SkillRegistry(skills_copy)
    db = PgSkillRegistry(pool, root=skills_copy, version_ttl=0)
    d_skills, d_err = disk.load_with_errors()
    s_skills, s_err = db.load_with_errors()
    assert d_err == s_err == []
    assert [s.name for s in s_skills] == sorted(s.name for s in d_skills)
    by_name = {s.name: s for s in d_skills}
    for s in s_skills:
        assert SkillRegistry.schema_for(s) == SkillRegistry.schema_for(by_name[s.name])
        assert (s.tool_calls, s.steps, s.scope, s.owner_role, s.body) == (
            by_name[s.name].tool_calls, by_name[s.name].steps, by_name[s.name].scope,
            by_name[s.name].owner_role, by_name[s.name].body,
        )
        assert s.path == db_path(s.name)


# ==================================================================== 2. seeding


def test_seed_happens_once_and_never_again(pg, policy_copy):
    pool, _admin, _schema = pg
    first = PgPolicyStore(pool, seed_from=policy_copy)
    n = len(first.rows())
    assert n == len(file_rows(policy_copy))
    # a later start-up with an edited checkout does NOT re-seed or add the new line
    policy_copy.write_text(policy_copy.read_text(encoding="utf-8") + "p, x_agent, x, thing, read\n", encoding="utf-8")
    other = laptop(pg[2], "late")
    try:
        assert seed_policy(other, policy_copy) == 0
        assert len(PgPolicyStore(other, seed_from=policy_copy).rows()) == n
    finally:
        other.close()


def test_simultaneous_first_boots_seed_exactly_once(pg, skills_copy):
    _pool, _admin, schema = pg
    pools = [laptop(schema, f"boot{i}") for i in range(6)]
    errors: list[BaseException] = []
    start = threading.Barrier(len(pools))

    def boot(p):
        try:
            start.wait()
            PgPolicyStore(p)
            PgSkillRegistry(p, root=skills_copy)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=boot, args=(p,)) for p in pools]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert errors == []
        with pools[0].connection() as conn:
            assert conn.execute("SELECT COUNT(*) AS n FROM policy_rules").fetchone()["n"] == len(file_rows(POLICY_PATH))
            assert conn.execute("SELECT COUNT(*) AS n FROM skills").fetchone()["n"] == len(SkillRegistry(skills_copy).load_all())
            markers = conn.execute("SELECT key FROM meta WHERE key LIKE '%%_seeded' ORDER BY key").fetchall()
        assert [m["key"] for m in markers] == ["policy_seeded", "skills_seeded"]
    finally:
        for p in pools:
            p.close()


# ==================================================================== 3. propagation across laptops


def test_rule_added_on_laptop_a_is_enforced_on_laptop_b_without_restart(pg):
    pool_a, _admin, schema = pg
    pool_b = laptop(schema, "b")
    try:
        enforcer_b = PolicyEnforcer(policy_store=PgPolicyStore(pool_b, version_ttl=0))
        # alice is marketing; there is no rule for 'reports'
        assert enforcer_b.enforce("alice@acme.com", "marketing", "reports", "read") is False
        assert PgPolicyStore(pool_a).add_rows([("p", "marketing_agent", "marketing", "reports", "read")], "admin@acme.com") == 1
        assert enforcer_b.enforce("alice@acme.com", "marketing", "reports", "read") is True
        assert enforcer_b.enforce("bob@acme.com", "hr", "reports", "read") is False  # still per-domain
        # and removal propagates the same way
        assert PgPolicyStore(pool_a).remove_rows(
            [("p", "marketing_agent", "marketing", "reports", "read")], sources=("dashboard",)
        ) == 1
        assert enforcer_b.enforce("alice@acme.com", "marketing", "reports", "read") is False
    finally:
        pool_b.close()


def test_skill_saved_on_laptop_a_is_visible_on_laptop_b(pg, skills_copy):
    pool_a, _admin, schema = pg
    pool_b = laptop(schema, "b")
    try:
        a = PgSkillRegistry(pool_a, root=skills_copy, version_ttl=0)
        b = PgSkillRegistry(pool_b, root=skills_copy, version_ttl=0)
        assert b.get("fresh_skill") is None
        a.save(parse_skill_document(skill_doc("fresh_skill")), actor="admin@acme.com")
        assert b.get("fresh_skill").version == 1
        a.save(parse_skill_document(skill_doc("fresh_skill", version=2)))
        assert b.get("fresh_skill").version == 2  # an edit anywhere is the next run's skill
        assert b.get("fresh_skill").body == "Body v2."
        assert a.delete("fresh_skill") is True and a.delete("fresh_skill") is False
        assert b.get("fresh_skill") is None
    finally:
        pool_b.close()


def test_own_write_is_visible_at_once_even_with_a_long_poll_interval(pg, skills_copy):
    pool, _admin, _schema = pg
    reg = PgSkillRegistry(pool, root=skills_copy, version_ttl=3600)
    policy = PgPolicyStore(pool, version_ttl=3600)
    enforcer = PolicyEnforcer(policy_store=policy)
    reg.load_all()
    reg.save(parse_skill_document(skill_doc("mine")))
    assert reg.get("mine") is not None
    assert not enforcer.enforce("alice@acme.com", "marketing", "skill_mine", "use")
    policy.add_rows([("p", "marketing_agent", "marketing", "skill_mine", "use")])
    assert enforcer.enforce("alice@acme.com", "marketing", "skill_mine", "use")


# ==================================================================== 4. contract (both back-ends)


def test_policy_store_contract(policy_store):
    rules = policy_store.rules()
    assert ["admin", "admin", "*", "*"] in rules["p"]
    assert policy_store.has_row(("p", "hr_agent", "hr", "hr", "read"))
    assert not policy_store.has_row(("p", "hr_agent", "hr", "hr", "delete"))  # deliberately absent
    v0 = policy_store.version()
    new = [("p", "legal_agent", "legal", "*", "*"), ("p", "legal_head", "legal", "*", "*")]
    assert policy_store.add_rows(new, "admin@acme.com") == 2
    assert policy_store.add_rows(new, "admin@acme.com") == 0  # idempotent
    assert policy_store.add_rows([new[0], new[0]]) == 0
    assert all(policy_store.has_row(r) for r in new)
    assert policy_store.version() != v0
    assert len([r for r in policy_store.rules()["p"] if r[0] == "legal_agent"]) == 1


def test_file_store_appends_in_the_historic_format(policy_copy):
    FilePolicyStore(policy_copy).add_rows([("p", "legal_agent", "legal", "*", "*")])
    assert policy_copy.read_text(encoding="utf-8").splitlines()[-1] == "p, legal_agent, legal, *, *"


def test_file_enforcer_picks_up_a_hand_edit_without_a_member_change(policy_copy):
    enforcer = PolicyEnforcer(policy_path=policy_copy)
    assert not enforcer.enforce("alice@acme.com", "marketing", "reports", "read")
    with open(policy_copy, "a", encoding="utf-8") as fh:
        fh.write("p, marketing_agent, marketing, reports, read\n")
    assert enforcer.enforce("alice@acme.com", "marketing", "reports", "read")


@pytest.mark.parametrize(
    "row",
    [
        ("x", "a", "b", "c", "d"),
        ("p", "a", "b", "c"),
        ("p", "a", "b", "c", "d", "e"),
        ("p", "a", "", "c", "d"),
        ("g", "a", "b"),
        (),
    ],
)
def test_malformed_rows_are_refused(row):
    with pytest.raises(ValueError):
        normalise(row)


def test_bad_rows_never_reach_the_table(pg):
    pool, _admin, _schema = pg
    store = PgPolicyStore(pool)
    before = len(store.rows())
    with pytest.raises(ValueError):
        store.add_rows([("p", "ok", "ok", "ok", "ok"), ("p", "broken", "", "x", "y")])
    assert len(store.rows()) == before  # validated before anything is sent


def test_values_with_commas_and_quotes_round_trip(pg):
    """The CSV could not hold these; the table must not mangle them."""
    pool, _admin, _schema = pg
    store = PgPolicyStore(pool)
    odd = ("p", "role'x", "dom,1", 'obj"2', "act;--")
    store.add_rows([odd])
    assert store.has_row(odd)
    assert list(odd[1:]) in store.rules()["p"]


def test_g_rows_are_stored_with_their_own_arity(pg):
    pool, _admin, _schema = pg
    store = PgPolicyStore(pool)
    store.add_rows([("g", "zed@acme.com", "marketing_agent", "marketing")])
    assert ["zed@acme.com", "marketing_agent", "marketing"] in store.rules()["g"]
    enforcer = PolicyEnforcer(policy_store=store)
    assert enforcer.enforce("zed@acme.com", "marketing", "crm", "read")


def test_malformed_stored_skill_is_reported_not_run(pg, skills_copy):
    pool, admin, schema = pg
    reg = PgSkillRegistry(pool, root=skills_copy, version_ttl=0)
    admin.execute(f'SET search_path TO "{schema}"')
    admin.execute(
        "INSERT INTO skills (name, scope, document, source, updated_at) VALUES ('bad', 'central', 'no frontmatter', 'dashboard', 'now')"
    )
    admin.execute("UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'members_version'")
    skills, errors = reg.load_with_errors()
    assert "bad" not in {s.name for s in skills} and any("no_frontmatter" in e for e in errors)
    assert len(skills) == len(SkillRegistry(skills_copy).load_all())


def test_table_constraints_reject_unknown_ptype_and_source(pg):
    import psycopg

    pool, _admin, _schema = pg
    PgPolicyStore(pool)
    with pool.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO policy_rules (ptype, v0, v1, v2, source, created_at) VALUES ('q', 'a', 'b', 'c', 'file', 'now')")
    with pool.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO policy_rules (ptype, v0, v1, v2, v3, source, created_at) VALUES ('p', 'a', 'b', 'c', 'd', 'hax', 'now')")


# ==================================================================== 5. sync-config


def test_drift_add_and_prune_leave_dashboard_rules_alone(pg, policy_copy):
    pool, _admin, _schema = pg
    store = PgPolicyStore(pool, seed_from=policy_copy)
    store.add_rows([("p", "legal_agent", "legal", "*", "*")], "admin@acme.com")  # from the dashboard
    text = policy_copy.read_text(encoding="utf-8")
    text = text.replace("p, hr_agent, hr, hr, create\n", "")  # removed in git
    text += "p, hr_agent, hr, reports, read\n"  # added in git
    policy_copy.write_text(text, encoding="utf-8")

    missing, stale = store.drift(policy_copy)
    assert missing == [("p", "hr_agent", "hr", "reports", "read")]
    assert stale == [("p", "hr_agent", "hr", "hr", "create")]
    assert store.add_rows(missing, "sync-config", source="file") == 1
    assert store.remove_rows(stale) == 1
    assert store.drift(policy_copy) == ([], [])
    assert store.has_row(("p", "legal_agent", "legal", "*", "*"))  # dashboard rule survives
    # prune never removes dashboard rows even if asked by key
    assert store.remove_rows([("p", "legal_agent", "legal", "*", "*")]) == 0


def test_skill_import_keeps_database_edits_unless_overwrite(pg, skills_copy):
    pool, _admin, _schema = pg
    reg = PgSkillRegistry(pool, root=skills_copy, version_ttl=0)
    edited = parse_skill_document(skill_doc("launch_campaign", version=9).replace("dept/marketing", "dept/marketing"))
    reg.save(edited)  # a dashboard edit
    new_dir = skills_copy / "central" / "brand_new"
    new_dir.mkdir()
    (new_dir / "skill.md").write_text(skill_doc("brand_new").replace("dept/marketing", "central"), encoding="utf-8")

    missing, differs, errors = reg.drift()
    assert [s.name for s in missing] == ["brand_new"] and [s.name for s in differs] == ["launch_campaign"]
    assert errors == []
    assert reg.import_skills(missing + differs) == 1  # only the new one
    assert reg.get("launch_campaign").version == 9
    assert reg.import_skills(differs, overwrite=True) == 1
    assert reg.get("launch_campaign").version != 9
    assert reg.drift()[:2] == ([], [])


def test_sync_config_cli_end_to_end(pg, tmp_path):
    """The real command, in a subprocess, pointed at this test's private schema."""
    _pool, _admin, schema = pg
    sep = "&" if "?" in TEST_URL else "?"
    url = f"{TEST_URL}{sep}options=-csearch_path%3D{schema}"
    env = {**os.environ, "DATABASE_URL": url}
    env.pop("TEST_DATABASE_URL", None)

    def run(*args):
        return subprocess.run(
            [sys.executable, "-m", "app.db", "sync-config", *args],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120, check=False,
        )

    first = run("--dry-run")
    assert first.returncode == 0, first.stderr
    assert "0 rule(s) in policy.csv missing" in first.stdout  # the command seeded the empty schema
    assert "dry run" in first.stdout
    check = laptop(schema, "cli")
    try:
        assert len(PgPolicyStore(check).rows()) == len(file_rows(POLICY_PATH))
        with check.connection() as conn:
            conn.execute("DELETE FROM policy_rules WHERE v0 = 'hr_agent' AND v2 = 'hr' AND v3 = 'create'")
        second = run()
        assert second.returncode == 0, second.stderr
        assert "+ p, hr_agent, hr, hr, create" in second.stdout and "+1 rule(s)" in second.stdout
        assert PgPolicyStore(check).has_row(("p", "hr_agent", "hr", "hr", "create"))
    finally:
        check.close()
    assert "DATABASE_URL" not in first.stdout and "test@" not in first.stdout + first.stderr  # no secrets


# ==================================================================== 6. cost


def test_steady_state_skill_loading_costs_no_round_trips(pg, skills_copy, monkeypatch):
    pool, _admin, _schema = pg
    reg = PgSkillRegistry(pool, root=skills_copy, version_ttl=3600)
    reg.load_all()
    calls = []
    real = PgSkillRegistry._read
    monkeypatch.setattr(PgSkillRegistry, "_read", lambda self: calls.append(1) or real(self))
    borrowed = []
    real_conn = pool.connection
    monkeypatch.setattr(pool, "connection", lambda *a, **k: borrowed.append(1) or real_conn(*a, **k))
    for _ in range(50):
        reg.load_all()
        reg.get("launch_campaign")
    assert calls == [] and borrowed == []


def test_steady_state_enforcement_reads_the_rules_only_on_change(pg):
    pool, _admin, _schema = pg
    store = PgPolicyStore(pool, version_ttl=3600)
    enforcer = PolicyEnforcer(policy_store=store)
    reads = []
    real = store.rows
    store.rows = lambda: reads.append(1) or real()  # type: ignore[method-assign]
    for _ in range(200):
        enforcer.enforce("alice@acme.com", "marketing", "crm", "read")
    assert reads == []
    store.add_rows([("p", "marketing_agent", "marketing", "reports", "read")])
    enforcer.enforce("alice@acme.com", "marketing", "crm", "read")
    assert reads == [1]


def test_every_write_bumps_the_shared_counter_once(pg, skills_copy):
    pool, _admin, _schema = pg
    store = PgPolicyStore(pool)
    reg = PgSkillRegistry(pool, root=skills_copy)
    v = members_version(pool)
    store.add_rows([("p", "a_agent", "a", "x", "read")])
    assert members_version(pool) == v + 1
    store.add_rows([("p", "a_agent", "a", "x", "read")])  # no-op: no bump
    assert members_version(pool) == v + 1
    reg.save(parse_skill_document(skill_doc("counted")))
    assert members_version(pool) == v + 2
    reg.delete("nope")  # no-op: no bump
    assert members_version(pool) == v + 2


# ==================================================================== 7. wiring


def test_factories_pick_the_backend(monkeypatch, pg, skills_copy):
    from app.harness import policy_store as ps
    from app.skills import pg_registry

    pool, _admin, _schema = pg
    assert isinstance(ps.open_policy_store(), FilePolicyStore)
    assert type(SkillRegistry.open(skills_copy)) is SkillRegistry
    monkeypatch.setattr("app.db.is_enabled", lambda: True)
    monkeypatch.setattr("app.harness.pg_policy.get_pool", lambda: pool)
    monkeypatch.setattr(pg_registry, "get_pool", lambda: pool)
    monkeypatch.setattr(get_settings().skills, "skills_dir", skills_copy)
    assert isinstance(ps.open_policy_store(), PgPolicyStore)
    reg = SkillRegistry.open()
    assert isinstance(reg, PgSkillRegistry) and SkillRegistry.open() is reg  # one cache per pool


def test_casbin_adapter_ignores_unknown_ptypes():
    import casbin

    e = casbin.Enforcer(str(MODEL_PATH), RulesAdapter({"p": [["r", "d", "o", "a"]], "zz": [["x"]]}))
    e.load_policy()
    assert e.get_policy() == [["r", "d", "o", "a"]]


@pytest.fixture
def dashboard_on_pg(pg, skills_copy, monkeypatch):
    """The dashboard with its policy + skill stores on the test schema."""
    import app.dashboard.app as dash

    pool, _admin, _schema = pg
    store = PgPolicyStore(pool, version_ttl=0)
    reg = PgSkillRegistry(pool, root=skills_copy, version_ttl=0)
    monkeypatch.setattr(dash, "_policy_store", lambda: store)
    comps = dict(dash._get_components())
    comps["skills"] = reg
    monkeypatch.setattr(dash, "_components", comps)
    return dash, store, reg


def test_dashboard_department_registration_writes_shared_rules(dashboard_on_pg):
    from fastapi.testclient import TestClient

    from tests.test_members_mgmt import AUTH

    dash, store, _reg = dashboard_on_pg
    client = TestClient(dash.app)
    resp = client.post("/api/departments", json={"name": "legal", "service": "legal", "service_url": ""}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    with store._pool.connection() as conn:
        got = conn.execute(
            "SELECT DISTINCT v0, source, created_by FROM policy_rules WHERE v1 = 'legal' ORDER BY v0"
        ).fetchall()
    assert [(r["v0"], r["source"], r["created_by"]) for r in got] == [
        ("legal_agent", "dashboard", "admin@acme.com"),
        ("legal_head", "dashboard", "admin@acme.com"),
    ]
    # Mk15: granular rows (own resources + dept/user memory), never the old `*, *` wildcard
    assert store.has_row(("p", "legal_agent", "legal", "legal", "read"))
    assert store.has_row(("p", "legal_head", "legal", "memory_dept", "write"))
    assert not store.has_row(("p", "legal_agent", "legal", "*", "*"))
    listed = {d["name"] for d in client.get("/api/departments", headers=AUTH).json()}
    assert "legal" in listed
    # a toolkit grant goes to the same table
    dash._ensure_toolkit_policy_rows("slack", "admin@acme.com")
    assert store.has_row(("p", "hr_agent", "hr", "slack", "write"))
    assert store.has_row(("p", "hr_head", "hr", "slack", "write"))  # Mk15: heads mirror members
    assert store.has_row(("p", "legal_agent", "legal", "slack", "read"))  # registered depts too


def test_dashboard_skill_editor_round_trip_on_the_database(dashboard_on_pg):
    from fastapi.testclient import TestClient

    from tests.test_members_mgmt import AUTH

    dash, _store, reg = dashboard_on_pg
    client = TestClient(dash.app)
    created = client.post("/api/skills", json={"content": skill_doc("editor_skill")}, headers=AUTH)
    assert created.status_code == 200 and created.json()["path"] == db_path("editor_skill")
    assert client.post("/api/skills", json={"content": skill_doc("editor_skill")}, headers=AUTH).status_code == 409
    listed = client.get("/api/skills", headers=AUTH).json()
    row = next(s for s in listed["skills"] if s["name"] == "editor_skill")
    assert row["policy_row"] is False
    assert next(s for s in listed["skills"] if s["name"] == "launch_campaign")["policy_row"] is True
    assert client.put("/api/skills/editor_skill", json={"content": skill_doc("editor_skill", 3)}, headers=AUTH).status_code == 200
    assert "version: 3" in client.get("/api/skills/editor_skill", headers=AUTH).json()["content"]
    with reg._pool.connection() as conn:
        assert conn.execute("SELECT updated_by FROM skills WHERE name = 'editor_skill'").fetchone()["updated_by"] == "admin@acme.com"
    assert client.delete("/api/skills/editor_skill", headers=AUTH).status_code == 200
    assert client.get("/api/skills/editor_skill", headers=AUTH).status_code == 404


def test_agent_runs_a_skill_that_exists_only_in_the_database(pg, executor, trace_store, audit_log, skills_copy, tmp_path, monkeypatch):
    """End to end: the skill file is NOT on disk and its rule is NOT in policy.csv — both were
    added 'on another laptop'. Alice's run sees the skill, may use it and executes its steps;
    Bob (HR) never sees it."""
    import app.agent.runner as runner_mod
    from app.memory.approvals import ApprovalStore
    from app.memory.episodic import EpisodicStore
    from app.memory.store import SqliteBaseStore
    from tests.conftest import make_runner

    pool, _admin, schema = pg
    other = laptop(schema, "other")
    try:
        PgSkillRegistry(other, root=skills_copy).save(parse_skill_document(skill_doc("db_only_skill")))
        PgPolicyStore(other).add_rows([("p", "marketing_agent", "marketing", "skill_db_only_skill", "use")])
    finally:
        other.close()
    assert SkillRegistry(skills_copy).get("db_only_skill") is None  # not on disk
    reg = PgSkillRegistry(pool, root=skills_copy, version_ttl=0)
    monkeypatch.setattr(runner_mod.SkillRegistry, "open", staticmethod(lambda root=None: reg))
    enforcer = PolicyEnforcer(policy_store=PgPolicyStore(pool, version_ttl=0))
    mem = tmp_path / "memory.sqlite"

    def runner(provider):
        return make_runner(
            provider=provider, executor=executor, trace=trace_store, enforcer=enforcer, audit=audit_log,
            store=SqliteBaseStore(mem), episodic=EpisodicStore(mem), approvals=ApprovalStore(mem),
        )

    provider = MockLLM(sequence=[
        {"text": "go", "tool_calls": [{"name": "db_only_skill", "args": {"q": "acme"}}]},
        {"text": "done.", "tool_calls": []},
    ])
    result = runner(provider)(ALICE, "run it")
    assert result.reply == "done."
    assert [c["name"] for c in result.tool_calls] == ["db_only_skill"]
    assert result.tool_calls[0]["outcome"] == "allowed"
    tree = trace_store.get_run_tree(result.run_id)
    assert [e["payload"]["skill"] for e in tree if e["type"] == TraceEventType.SKILL_USE.value] == ["db_only_skill"]
    assert ("skill", "db_only_skill", "ALLOW") in [
        (a["resource"]["type"], a["resource"]["id"], a["decision"]) for a in audit_log.entries()
    ]

    quiet = MockLLM({"hi": {"text": "ok", "tool_calls": []}})
    runner(quiet)(BOB, "hi")
    assert "db_only_skill" not in {s["function"]["name"] for s in quiet.last_tools}
    runner(quiet)(ALICE, "hi")
    assert "db_only_skill" in {s["function"]["name"] for s in quiet.last_tools}
