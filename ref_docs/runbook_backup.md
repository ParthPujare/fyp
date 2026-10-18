# Runbook — Backup, Retention & HMAC Rotation (Mk18 Phase H)

## 1. Scheduled retention (shared database)

| Task | Command | Cadence (suggested) |
|------|---------|---------------------|
| Preview | `python -m app.db prune --chats --traces-older-than 30 --episodic-older-than 90 --dry-run` | weekly review |
| Checkpoints | `python -m app.db prune --chats` | weekly (keeps latest per thread) |
| Old traces, scrubbed | `python -m app.db prune --traces-older-than 30 --redact` | monthly — payloads scrubbed, chains rehashed, `verify` stays green |
| Old traces, deleted | `python -m app.db prune --traces-older-than 90` (no `--redact`) | quarterly |
| Old transcripts | `python -m app.db prune --episodic-older-than 90` | monthly |
| Audit | **never pruned** | — |

Verify after every prune: `python -m app.db verify-audit` must print `chain intact`.

## 2. Backup / recovery

- **Neon (shared members/approvals/memory/traces/audit):** enable Neon PITR
  (point-in-time restore) on the project; note the restore window here:
  `____`. Before any migration, record `python -m app.db status`.
- **Local volume:** `docker volume inspect fyp_data`; snapshot via
  `docker run --rm -v fyp_data:/data -v $(pwd):/backup alpine tar czf
  /backup/fyp_data_$(date +%F).tgz /data`.
- **Destructive guard:** `docker compose down -v` wipes the local volume and
  reseeds demo accounts — confirm twice, never on a machine holding the only
  copy of local (non-Neon) chats.
- **Restore check:** after restore, `docker compose up -d --build`,
  `python -m app.db check` (0001..0014, no ahead/behind warning),
  `python -m app.db verify-audit`.

## 3. HMAC rotation (audit + trace, 24h dual window)

Both chains support retired keys: a line verifies under ANY configured era
key, so rotation never rewrites history.

1. Generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"` → `NEW`.
2. Set `SERVICES__AUDIT_HMAC_KEY=NEW`, append the old value to
   `SERVICES__AUDIT_HMAC_KEY_PREV` (comma-separated if several).
   Same for `TRACE__HMAC_KEY` / `TRACE__HMAC_KEY_PREV` if trace HMAC is on.
3. Restart/rebuild every PC (`docker compose up -d --build`) within 24h.
4. Verify on each PC: `python -m app.db verify-audit` → `chain intact`;
   trace spot-check passes.
5. Keep the retired key configured (lines from its era verify forever).
   Dropping it makes pre-rotation lines report tamper — that is expected,
   not an attack; re-add the key if it happens by accident.

### Enabling HMAC on an existing (plain) chain — no migration

Mk19: a chain written before any key was set keeps verifying after you set
one. Each line/run carries a hashed era flag (`keyed`); plain-era lines
verify under plain SHA-256, keyed lines under the key chain. Nothing is
rewritten and nothing is archived.

1. Set `SERVICES__AUDIT_HMAC_KEY` (and `SERVICES__TRACE_HMAC_KEY` for the
   trace chain); restart. Required when `ENV!=dev` — the app refuses to boot
   without them (Mk19 Phase 5).
2. `python -m app.db verify-audit` → `chain intact` (plain era + new keyed
   lines together).
3. One rule to remember: a **plain line after keyed lines** is reported as
   tampering (key stripped or chain rewritten). New lines are always keyed
   once the key exists, so a healthy chain never trips this.

## 4. Spend caps (abuse containment)

- Defaults: `LLM__DAILY_CALL_CAP=500`, `LLM__DAILY_COST_CENTS_CAP=500`
  (`$5`), `LLM__EST_COST_CENTS_PER_CALL=1`. Tune per deployment in `.env`.
- Over either cap: in-run turns degrade to Mock with a `spend_cap` trace
  event; the gateway answers **429** pre-run. Usage table `llm_usage`
  (per user, UTC day); inspect via
  `SELECT username, day, calls, cost_cents FROM llm_usage ORDER BY day DESC`.
- Caps count real providers only; MockLLM is never metered (offline/tests
  unaffected).
