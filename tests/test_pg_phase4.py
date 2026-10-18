"""Mk13 Phase 4 — episodic recall on the shared Postgres, and the no-leakage guarantee.

Theorem 2 (research_objective/01_ztae_formal_spec.md): for every caller and every query, recall
returns nothing from a scope the caller may not read. This file tests that at four levels, on
BOTH the SQLite (FTS5) and the Postgres (tsvector) stores:

1. contract      — search semantics are the same on both back-ends (phrase, case, dedup, limit);
2. isolation     — randomized canary property, adversarial query strings, scope-name confusion;
3. fail-closed   — a store that returns foreign rows is caught by the store, and again by
                   `RecallService`'s independent policy re-check; the tool degrades to "unavailable";
4. end to end    — real chats in several departments write transcripts, real `recall` tool calls read them.

Every isolation test is paired with a *non-vacuity* assertion (the caller does find its own
data), so a test that passes because nothing is ever returned cannot hide a broken search.
"""

from __future__ import annotations

import random
import re
import threading
from types import SimpleNamespace

import pytest

from app.agent.runner import run_agent
from app.config import get_settings
from app.db import compat
from app.llm.mock import MockLLM
from app.memory.episodic import EpisodicStore, RecallIsolationError, check_in_scope, normalise_query
from app.memory.pg_episodic import MAX_TRANSCRIPT_CHARS, PgEpisodicStore
from app.memory.recall import RecallService
from tests.attack.helpers import ADMIN, ALICE, BOB, CAROL, DAVE
from tests.conftest import TEST_URL

SETTINGS = get_settings()


@pytest.fixture(params=["sqlite", "postgres"])
def episodic(request, tmp_path):
    if request.param == "postgres":
        if not TEST_URL:
            pytest.skip("set TEST_DATABASE_URL (make test-pg) for Postgres tests")
        pool, _admin, _schema = request.getfixturevalue("pg")
        store = PgEpisodicStore(pool)
        store.kind = "postgres"
        return store
    store = EpisodicStore(tmp_path / "episodic.sqlite")
    store.kind = "sqlite"
    return store


def add(store, scope, run_id, text, sub="someone@acme.com"):
    return store.append_transcript(scope, run_id, {"sub": sub}, text)


# ==================================================================== 1. contract (both back-ends)


def test_search_is_a_case_insensitive_phrase_over_alphanumeric_words(episodic):
    add(episodic, "dept/finance", "r1", "the quarterly Budget, Review with finance went well")
    hit = lambda q: [h["run_id"] for h in episodic.search(q, ["dept/finance"])]
    assert hit("budget review") == ["r1"]
    assert hit("BUDGET!!! review??") == ["r1"]  # case and punctuation are irrelevant
    assert hit("review budget") == []  # a phrase: the words must be adjacent AND in order
    assert hit("quarterly review") == []  # a word in between breaks the phrase
    assert hit("nothing like this") == []


def test_punctuation_splits_words_so_emails_and_urls_are_searchable_by_part(episodic):
    add(episodic, "dept/hr", "r1", "please contact bob@acme.com or see https://example.org/pay-roll?id=7")
    for q in ("acme", "bob acme com", "example org", "pay roll", "id 7"):
        assert [h["run_id"] for h in episodic.search(q, ["dept/hr"])] == ["r1"], q


def test_appending_the_same_run_twice_keeps_one_transcript_and_the_first_timestamp(episodic):
    first = add(episodic, "dept/hr", "r1", "original text")
    second = add(episodic, "dept/hr", "r1", "different text on the re-run after an approval")
    assert first == second
    assert [h["transcript"] for h in episodic.search("original", ["dept/hr"])] == ["original text"]
    assert episodic.search("different", ["dept/hr"]) == []
    assert add(episodic, "dept/finance", "r1", "same run id in another scope is a separate row")  # not a dup


def test_search_only_looks_in_the_requested_scopes(episodic):
    add(episodic, "dept/hr", "r1", "shared phrase zzmarker in hr")
    add(episodic, "dept/finance", "r2", "shared phrase zzmarker in finance")
    add(episodic, "user/bob@acme.com", "r3", "shared phrase zzmarker private to bob")
    assert {h["scope"] for h in episodic.search("zzmarker", ["dept/hr"])} == {"dept/hr"}
    assert {h["scope"] for h in episodic.search("zzmarker", ["dept/hr", "dept/finance"])} == {"dept/hr", "dept/finance"}
    assert episodic.search("zzmarker", []) == []  # no scopes => nothing, never "everything"
    assert episodic.search("zzmarker", ["dept/legal"]) == []  # a scope with no rows
    assert episodic.search("", ["dept/hr"]) == [] and episodic.search("   ", ["dept/hr"]) == []
    assert episodic.search("!!! ???", ["dept/hr"]) == []  # nothing searchable left after normalising


def test_limit_and_transcripts_in(episodic):
    for i in range(7):
        add(episodic, "dept/hr", f"r{i}", f"weekly standup notes number {i}")
    add(episodic, "dept/finance", "other", "weekly standup notes of finance")
    assert len(episodic.search("weekly standup", ["dept/hr"], limit=3)) == 3
    assert len(episodic.search("weekly standup", ["dept/hr"])) == 5  # default limit
    recent = episodic.transcripts_in("dept/hr", limit=4)
    assert len(recent) == 4 and {t["scope"] for t in recent} == {"dept/hr"}
    assert [t["ts"] for t in recent] == sorted((t["ts"] for t in recent), reverse=True)  # newest first
    assert set(recent[0]) >= {"scope", "run_id", "ts", "actor", "transcript"}


def test_search_hits_carry_the_metadata_recall_needs(episodic):
    add(episodic, "dept/hr", "r1", "hello zzmeta world", sub="bob@acme.com")
    (hit,) = episodic.search("zzmeta", ["dept/hr"])
    assert hit["scope"] == "dept/hr" and hit["run_id"] == "r1" and hit["actor"] == {"sub": "bob@acme.com"}
    assert hit["transcript"] == "hello zzmeta world" and "score" in hit


# ==================================================================== 2. isolation (both back-ends)

DEPTS = ["marketing", "hr", "finance", "devops", "admin", "legal", "ops"]  # legal/ops have no demo user
USERS = {"alice": ALICE, "bob": BOB, "carol": CAROL, "dave": DAVE, "admin": ADMIN}
COMMON = ["budget", "review", "launch", "quarter", "customer"]


@pytest.fixture
def world(episodic, enforcer):
    """Every department has transcripts full of the SAME common words plus its own canary and a
    body marker; the caller-visible scopes come from the real policy engine."""
    rng = random.Random(4242)
    for dept in DEPTS:
        for i in range(8):
            words = rng.sample(COMMON, 3)
            add(episodic, f"dept/{dept}", f"{dept}-{i}", f"{' '.join(words)} zzcan{dept} zzunique{dept}{i} secretbody{dept}")
    add(episodic, "user/bob@acme.com", "bob-private", "zzcanuserbob secretbodyuserbob budget review")
    add(episodic, "central", "central-1", "zzcancentral secretbodycentral budget review")
    recall = RecallService(episodic, enforcer, SETTINGS.memory)
    return SimpleNamespace(store=episodic, recall=recall)


_MARKER_RE = re.compile(r"secretbody[a-z0-9]+")


def markers_in(text: str) -> set[str]:
    # Mk18 fences every recall line in <untrusted ...>...</untrusted>; the closing tag
    # glues onto the transcript's last word, so whitespace-splitting would see a fake
    # "secretbodyX</untrusted>" token and miscount it as a foreign marker. Extract the
    # marker tokens directly instead — a genuine foreign marker still matches, so the
    # leak assertions below keep failing loudly on a real cross-scope leak.
    return set(_MARKER_RE.findall(text))


def allowed_markers(scopes: list[str]) -> set[str]:
    out = set()
    for s in scopes:
        if s.startswith("dept/"):
            out.add("secretbody" + s.split("/", 1)[1])
        elif s == "central":
            out.add("secretbodycentral")
        elif s == "user/bob@acme.com":
            out.add("secretbodyuserbob")
    return out


def test_randomized_canary_property_no_query_ever_returns_a_foreign_scope(world):
    """5 callers x hundreds of queries built from every canary, common words and mutations."""
    rng = random.Random(99)
    canaries = [f"zzcan{d}" for d in DEPTS] + ["zzcanuserbob", "zzcancentral"]
    queries = list(canaries) + COMMON + [f"{a} {b}" for a in COMMON for b in COMMON]
    queries += [c.upper() for c in canaries] + [f"  {c} !!! " for c in canaries] + [f"{c} {rng.choice(COMMON)}" for c in canaries]
    queries += [f"{a} {c}" for c in canaries for a in COMMON]  # canary as the second word of a phrase
    total = leaks = own_hits = 0
    for claims in USERS.values():
        readable = world.recall.readable_scopes(claims)
        for q in queries:
            total += 1
            hits = world.store.search(q, readable)
            leaks += sum(1 for h in hits if h["scope"] not in readable)
            content = world.recall.recall(q, claims).content
            leaks += len(markers_in(content) - allowed_markers(readable))
            if q == f"zzcan{claims['dept']}":
                own_hits += 1 if markers_in(content) else 0
    assert total > 500
    assert leaks == 0, f"{leaks} leaked results over {total} queries"
    assert own_hits == len(USERS), "non-vacuity: every caller must still find its OWN department's canary"


def test_a_caller_asking_for_another_departments_canary_gets_nothing_of_it(world):
    for claims in USERS.values():
        readable = world.recall.readable_scopes(claims)
        for dept in DEPTS:
            result = world.recall.recall(f"zzcan{dept}", claims)
            assert result.ok
            if f"dept/{dept}" in readable:
                assert f"secretbody{dept}" in result.content  # own dept: found
            else:
                assert markers_in(result.content) == set() or markers_in(result.content) <= allowed_markers(readable)
                assert f"secretbody{dept}" not in result.content  # someone else's: never


ADVERSARIAL = [
    "zzcanhr | budget", "budget | zzcanhr", "zzcanhr & budget", "!zzcanmarketing", "zzcanhr:*", "zzcan*",
    "budget <-> zzcanhr", "budget <2> zzcanhr", "(zzcanhr)", "((zzcanhr) | (zzcanfinance))", "zzcanhr'", '"zzcanhr"',
    "'; DROP TABLE episodic; --", "\\", "%", "_", "%zzcanhr%", "zzcan%", "zzcan_r", "hr' OR '1'='1",
    "budget') OR scope LIKE '%", "zzcanhr\x00", "\x00", "budget\x00zzcanhr", "a" * 5000, "zzcanhr " * 500,
    "ＺＺＣＡＮＨＲ", "zzcanhr\u200b", "zzcanhr", "ZZCANHR", "zzCanHr", " zzcanhr ", "zzcanhr\n\tbudget",
    "dept/hr", "scope:dept/hr", "scope = 'dept/hr'", "dept/hr OR 1=1", "user/bob@acme.com", "bob@acme.com",
    "to_tsquery('zzcanhr')", "SELECT * FROM episodic", "zzcanhr:A", "zzcanhr:*B", "'zzcanhr':1",
]


def test_adversarial_query_strings_never_widen_the_search(world):
    """Operator characters, SQL fragments, wildcards, NULs, homoglyphs and giant inputs, asked by
    a MARKETING user while HR/finance/... data is present. None may raise; none may leak."""
    readable = world.recall.readable_scopes(ALICE)
    forbidden = {m for m in ("secretbodyhr", "secretbodyfinance", "secretbodydevops", "secretbodyadmin",
                             "secretbodylegal", "secretbodyops", "secretbodyuserbob")}
    for q in ADVERSARIAL:
        hits = world.store.search(q, readable)  # store level
        assert {h["scope"] for h in hits} <= set(readable), q
        result = world.recall.recall(q, ALICE)  # service level
        assert result.ok, q
        assert not (markers_in(result.content) & forbidden), f"leak for query {q!r}"
    # ...and the same battery must not have broken searching for real:
    assert "secretbodymarketing" in world.recall.recall("zzcanmarketing", ALICE).content


def test_scope_names_are_matched_exactly_never_as_patterns_or_prefixes(episodic):
    scopes = ["dept/hr", "dept/hr2", "dept/HR", "dept/hr%", "dept/h_", "dept/", "dept/hr/x", "user/hr", "hr", "Dept/hr", "dept/hr "]
    for i, scope in enumerate(scopes):
        add(episodic, scope, f"r{i}", f"needle zzscopetest row {i}")
    for wanted in scopes:
        got = {h["scope"] for h in episodic.search("zzscopetest", [wanted])}
        assert got == {wanted}, f"asking for {wanted!r} returned {got}"
    assert episodic.search("zzscopetest", [""]) == []
    assert episodic.search("zzscopetest", ["dept/%"]) == []  # no wildcard expansion
    assert episodic.search("zzscopetest", ["dept/h_"]) and {h["scope"] for h in episodic.search("zzscopetest", ["dept/h_"])} == {"dept/h_"}


def test_duplicate_and_reordered_scope_lists_change_nothing(episodic):
    add(episodic, "dept/hr", "r1", "zzdup phrase")
    assert len(episodic.search("zzdup", ["dept/hr", "dept/hr", "dept/hr"])) == 1


def test_a_departments_scope_is_isolated_from_a_user_named_like_it(world, member_store):
    """A member whose *username* looks like a department scope gains nothing: user scopes are
    self-scoped by the policy engine, department scopes by the caller's own dept."""
    claims = {"sub": "hr", "role": "marketing_agent", "dept": "marketing"}
    readable = world.recall.readable_scopes(claims)
    assert "dept/hr" not in readable
    assert "secretbodyhr" not in world.recall.recall("zzcanhr", claims).content


# ==================================================================== 3. fail closed


def test_check_in_scope_rejects_any_stray_row():
    rows = [{"scope": "dept/hr"}, {"scope": "dept/finance"}]
    assert check_in_scope(rows[:1], ["dept/hr"]) == rows[:1]
    with pytest.raises(RecallIsolationError, match="dept/finance"):
        check_in_scope(rows, ["dept/hr"])
    assert check_in_scope([], []) == []


def test_recall_service_drops_foreign_rows_even_if_a_store_returned_them(enforcer):
    """Layer 3: the policy engine is asked again about each hit's own scope."""

    class LeakyStore:
        def search(self, query, scopes, *, limit=5):  # ignores `scopes` entirely: the worst-case bug
            return [
                {"scope": "dept/hr", "run_id": "x", "ts": "t", "actor": {}, "transcript": "HR-ONLY layoffs", "score": 1},
                {"scope": "dept/marketing", "run_id": "y", "ts": "t", "actor": {}, "transcript": "marketing plan", "score": 1},
            ]

    result = RecallService(LeakyStore(), enforcer, SETTINGS.memory).recall("anything", ALICE)
    assert "marketing plan" in result.content and "layoffs" not in result.content and "HR-ONLY" not in result.content


def test_a_store_that_trips_its_fail_closed_check_makes_recall_unavailable_not_leaky(enforcer, caplog):
    class BrokenStore:
        def search(self, query, scopes, *, limit=5):
            raise RecallIsolationError("simulated widened query")

    with caplog.at_level("ERROR"):
        result = RecallService(BrokenStore(), enforcer, SETTINGS.memory).recall("zzsecretquery", ALICE)
    assert result.ok is False and result.content == "recall is temporarily unavailable"
    assert "isolation violation" in caplog.text and "zzsecretquery" not in caplog.text  # the query is not logged


def test_a_widened_postgres_query_is_caught_by_the_stores_own_check(pg, monkeypatch):
    """Simulate the classic bug — the scope predicate goes missing — and prove the store refuses to answer."""
    pool, _admin, _schema = pg
    store = PgEpisodicStore(pool)
    add(store, "dept/hr", "r1", "zzwiden secret")
    add(store, "dept/marketing", "r2", "zzwiden public")
    original = store._db.execute

    def widened(sql, params=()):
        return original(sql.replace("e.scope = ANY(?::text[])", "?::text[] IS NOT NULL"), params)

    monkeypatch.setattr(store._db, "execute", widened)
    with pytest.raises(RecallIsolationError):
        store.search("zzwiden", ["dept/marketing"])  # would have returned the HR row


# ==================================================================== postgres-only behaviour


def test_query_normalisation_strips_every_search_operator():
    assert normalise_query("a | b & !c :* (d) <-> e <2> f 'g' \"h\"") == "a b c d e 2 f g h"
    assert normalise_query("x\x00y") == "x y"  # a NUL is a separator, not glue
    assert normalise_query("!!!") == "" and len(normalise_query("a" * 5000)) <= 1000


def test_nul_bytes_and_giant_transcripts_do_not_break_a_write(pg):
    pool, _admin, _schema = pg
    store = PgEpisodicStore(pool)
    add(store, "dept/hr", "nul", "before\x00after zznulcheck")  # Postgres text cannot hold NUL: must not crash reflect
    assert [h["run_id"] for h in store.search("zznulcheck", ["dept/hr"])] == ["nul"]
    add(store, "dept/hr", "big", "zzbigcheck " + ("word " * 100_000))  # ~500 KB
    (hit,) = store.search("zzbigcheck", ["dept/hr"])
    assert len(hit["transcript"]) == MAX_TRANSCRIPT_CHARS  # truncated, still searchable


def test_concurrent_appends_of_one_run_produce_one_row_and_one_timestamp(pg):
    pool, _admin, _schema = pg
    store = PgEpisodicStore(pool)
    stamps: list[str] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=10)
            stamps.append(add(store, "dept/hr", "same-run", f"racing writer {i} zzrace"))
        except BaseException as exc:  # noqa: BLE001 - collected and asserted
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert not errors, errors[:2]
    assert len(set(stamps)) == 1 and len(store.search("zzrace", ["dept/hr"])) == 1


def test_another_machine_sees_a_transcript_as_soon_as_it_is_written(pg):
    pool, _admin, _schema = pg
    laptop_a, laptop_b = PgEpisodicStore(pool), PgEpisodicStore(pool)
    add(laptop_a, "dept/hr", "r1", "written on laptop A zzshared")
    assert [h["run_id"] for h in laptop_b.search("zzshared", ["dept/hr"])] == ["r1"]


def test_each_operation_is_one_round_trip_and_the_index_exists(pg, monkeypatch):
    pool, _admin, _schema = pg
    store = PgEpisodicStore(pool)
    seen: list[str] = []
    original = compat.PgSession.execute
    monkeypatch.setattr(compat.PgSession, "execute", lambda self, sql, params=(): (seen.append(sql[:30]), original(self, sql, params))[1])
    add(store, "dept/hr", "r1", "zzone trip")
    assert len(seen) == 1
    store.search("zzone", ["dept/hr"])
    assert len(seen) == 2
    with pool.connection() as conn:
        idx = {r["indexname"] for r in conn.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'episodic'")}
    assert {"idx_episodic_tsv", "idx_episodic_scope_ts", "episodic_scope_run"} <= idx


# ==================================================================== 4. end to end (real chats, real recall tool)


@pytest.fixture
def team(pg, executor, enforcer, audit_log, member_store):
    from langgraph.checkpoint.memory import InMemorySaver

    from app.memory.pg_stores import PgApprovalStore, PgBaseStore
    from app.ops.pg_trace import PgTraceStore

    pool, _admin, _schema = pg
    episodic = PgEpisodicStore(pool)
    comps = {
        "store": PgBaseStore(pool), "approvals": PgApprovalStore(pool), "trace": PgTraceStore(pool),
        "episodic": episodic, "recall": RecallService(episodic, enforcer, SETTINGS.memory),
        "checkpointer": InMemorySaver(), "executor": executor, "enforcer": enforcer, "audit": audit_log,
    }
    member_store.create("mia@acme.com", "pw-mia", "marketing_agent", "marketing", actor="admin@acme.com")
    mia = member_store.claims_for("mia@acme.com")
    return SimpleNamespace(comps=comps, episodic=episodic, mia=mia)


def chat(team, claims, text, thread):
    return run_agent(claims, text, provider=MockLLM(sequence=[]), thread_id=thread, **team.comps)


def recall_via_agent(team, claims, query, thread):
    provider = MockLLM(sequence=[{"text": "", "tool_calls": [{"name": "recall", "args": {"query": query}}]}, {"text": "done"}])
    result = run_agent(claims, f"what do we know about {query}?", provider=provider, thread_id=thread, **team.comps)
    return next(c for c in result.tool_calls if c["name"] == "recall")


def test_real_chats_are_recalled_by_the_same_department_and_never_by_another(team):
    chat(team, BOB, "note: the zzcanaryhrchat reorganisation memo mentions layoffs in Q4", "user:bob@acme.com:c1")
    chat(team, ALICE, "note: the zzcanarymktchat launch plan has budget 5000", "user:alice@acme.com:c1")

    # another department asks for HR's chat: nothing of it comes back
    stolen = recall_via_agent(team, ALICE, "zzcanaryhrchat reorganisation", "user:alice@acme.com:c2")
    assert stolen["outcome"] == "allowed"
    assert "layoffs" not in stolen["content"] and "Q4" not in stolen["content"]

    # team recall inside the department still works (the point of shared memory)...
    found = recall_via_agent(team, team.mia, "zzcanarymktchat launch", "user:mia@acme.com:c1")
    assert "budget 5000" in found["content"] and "[dept/marketing]" in found["content"]
    # ...and the reverse direction is closed too: HR cannot read marketing's chat
    closed = recall_via_agent(team, BOB, "zzcanarymktchat launch", "user:bob@acme.com:c2")
    assert "budget 5000" not in closed["content"]


def test_recalled_transcripts_are_still_pii_masked(team):
    chat(team, ALICE, "note: zzcanarypii employee ssn 123-45-6789 mail hr@acme.com", "user:alice@acme.com:pii")
    found = recall_via_agent(team, team.mia, "zzcanarypii employee", "user:mia@acme.com:pii")
    assert "123-45-6789" not in found["content"] and "***" in found["content"]  # marketing may not read SSNs


def test_a_resumed_run_does_not_duplicate_its_transcript(team):
    """`reflect` runs again when a run resumes after an approval; recall must show ONE transcript."""
    res = chat(team, ALICE, "note: zzcanaryonce appears once", "user:alice@acme.com:once")
    team.episodic.append_transcript("dept/marketing", res.run_id, {"sub": "alice@acme.com"}, "a re-run of reflect")
    assert len(team.episodic.search("zzcanaryonce", ["dept/marketing"])) == 1


def test_prune_forgets_only_episodic_transcripts_older_than_the_cutoff(pg, monkeypatch):
    import app.db.__main__ as cli

    pool, _admin, _schema = pg
    store = PgEpisodicStore(pool)
    add(store, "dept/hr", "old", "an old conversation zzold")
    add(store, "dept/hr", "new", "a fresh conversation zznew")
    with pool.connection() as conn:
        conn.execute("UPDATE episodic SET ts = '2020-01-01T00:00:00+00:00' WHERE run_id = 'old'")
    monkeypatch.setattr(cli, "is_enabled", lambda: True)
    monkeypatch.setattr(cli, "get_pool", lambda: pool)
    monkeypatch.setattr(cli, "close_pool", lambda: None)
    assert cli.main(["prune", "--episodic-older-than", "30", "--dry-run"]) == 0
    assert len(store.search("zzold", ["dept/hr"])) == 1  # dry run changes nothing
    assert cli.main(["prune", "--episodic-older-than", "30"]) == 0
    assert store.search("zzold", ["dept/hr"]) == [] and len(store.search("zznew", ["dept/hr"])) == 1
