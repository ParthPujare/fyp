# Session 035 — Close the two pre-Mk19 baseline test failures

## Metadata
- Date: 2026-10-18
- Plan / Phase: maintenance fix (cross-plan test hygiene; not a phase of a registered plan)
- Plan step: clears the 2 baseline failures recorded in Mk19 Phase 0 (`ref_docs/plans/plan_mk19_security_remediation.md`)
- Scope: make `make test` fully green by repairing two test expectations that Mk18 broke but never updated
- Status: complete
- State at start: git main @ `b314324`; `make test` = 666-capable but showed `2 failed (pre-existing: test_pg_phase4 canary, test_pg_phase6 policy_store_contract[file]), 664 passed`; `make lint` clean

## Objective
Explain and fix the 2 failing tests the user sees on `make test`, and confirm no real security regression hides behind them.

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| diagnose | reproduce | Ran the 2 tests in isolation: canary = `112 leaked results over 555 queries`; policy = `assert ['admin','admin','*','*'] in rules['p']` failed |
| diagnose | canary | `EpisodicStore.search` filters by scope in SQL AND calls `check_in_scope`, and `RecallService` re-checks per hit — so a real leak was impossible. The 112 came from the `markers_in()` helper, not from the store |
| diagnose | canary | Reproduced with a scripted world: every "leak" was the caller's OWN dept marker glued to Mk18's new `</untrusted>` fence, e.g. `secretbodymarketing</untrusted>`. Whitespace-splitting cannot see past the tag |
| diagnose | policy | `git log` on policy.csv + the CSV's own comment: Mk18 `6d7133e` removed `p, admin, admin, *, *` on purpose and replaced it with scoped grants (crm/devops/finance/...). The contract test still demanded the wildcard |
| decide | scope | Both are STALE TEST EXPECTATIONS, not product bugs. Product behaviour (scope fencing + deny-by-default admin) is correct and deliberate. Fix the tests, never the policy |
| decide | detector | The canary fix must NOT gut the detector. Use a `secretbody[a-z0-9]+` regex so a genuine foreign marker still matches and still trips lines 170/187/213 |
| fix | implement | Added `import re` + `_MARKER_RE`; rewrote `markers_in()` to `set(_MARKER_RE.findall(text))`. Replaced the wildcard assert with "wildcard gone AND scoped `crm, read` present" |
| verify | negative | Planted a real foreign marker (`secretbodyhr`) in fenced content → still detected; clean own-scope content → zero false positives. Detector integrity confirmed |
| verify | gates | `make lint` clean; `make test` = **666 passed, 0 failed, 156 skipped** — first fully green run |

## What was done
- `tests/test_pg_phase4.py`: added `import re`, `_MARKER_RE = re.compile(r"secretbody[a-z0-9]+")`, rewrote `markers_in()` to extract marker tokens by regex (tag-proof), with a comment explaining the Mk18 fence false-positive
- `tests/test_pg_phase6.py::test_policy_store_contract`: assert `p,admin,admin,*,*` is ABSENT (Mk18 deny-by-default) and `p,admin,admin,crm,read` is PRESENT (scoped grant)
- Commit `389d805`

## Decisions locked in this session
- The two failures were test debt from Mk18 `6d7133e` ("security loopholes fixed"), which changed product output/policy without updating these expectations. They were never Mk19 regressions (correctly recorded as the Mk19 Phase 0 baseline)
- Fix lives in the tests only — no product code and no policy.csv change, because the product behaviour is the intended security posture
- The canary leak detector must stay sensitive to genuine cross-scope markers; the regex keeps that property (proven by negative check)

## Verification performed
- `uv run pytest "tests/test_pg_phase4.py::test_randomized_canary_property_no_query_ever_returns_a_foreign_scope[sqlite]" "tests/test_pg_phase6.py::test_policy_store_contract[file]" -q` → 2 passed
- Negative check (scripted): planted `secretbodyhr` in fenced recall content → `markers_in` detected it; own-scope fenced content → no false positives
- `make lint` → All checks passed
- `make test` → **666 passed, 156 skipped, 0 failed** (was 664 passed / 2 failed)

## Blocker / open questions
- `make test-pg` still unrun (Docker down) — the `[postgres]` param of the policy contract test shares the same fixed code path and skips without `TEST_DATABASE_URL`

## Next step (for the next session)
- Mk19 closed and the suite is fully green. Open follow-ups remain from session 034: user-side `.env` rotation, prod HMAC keys, `make test-pg` when Docker is up, and the pyjwt 2.13.0 → ≥2.15.0 bump (4 CVEs, needs a scope decision)

## Handoff notes
- `markers_in()` is now regex-based; if a marker prefix ever changes from `secretbody`, update `_MARKER_RE`
- The `<untrusted>` fence from Mk18 glues onto a transcript's last word — any future test that tokenizes recall output must strip tags first, or it will reproduce this false positive
