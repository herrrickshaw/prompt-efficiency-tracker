# Prompt / Token / Outcome Audit — 2026-09-09

Account-wide retro: does this account's token/prompt optimization infrastructure actually measure outcomes, or just volume? Scope: all 52 repos catalogued in `repo-traffic-analytics/repos.yaml`, the masaladeutsch blog, `token_usage.db`, Headroom, and qmd. Full interactive version: https://claude.ai/code/artifact/c73a5a51-c1f4-4acf-b155-ac73571f0254

## Headline numbers

| Metric | Value |
|---|---|
| Tracked spend, 2026-05-21 → 2026-08-10 | $28,207.98 |
| Turns logged | 58,171 |
| Rows with a real `status` other than `'success'` | **0** |
| Rows with `market`/repo attribution filled in | **0** |
| Days the tracker has been silently dark (as of audit) | ~30 |
| Headroom lifetime savings vs. tracked spend | $12.55 vs $28,208 (0.04%) |
| Commits in this repo mentioning "silently"/"never actually"/"nobody reads" | 116 / 970 (12%) |

## 1. The tracking infrastructure measures cost, not outcome

`~/.ruflo/data/token_usage.db` (58,171 rows) has a `status` column that is **always `'success'`** — nothing has ever written a failure to it — and `market`/`task_name` columns that are blank or generic (`"Claude Code turn"` / `"Claude Code subagent turn"`) on every row. It can tell you total spend and which model was used; it cannot tell you which repo the spend went to or whether the work succeeded. It also stopped being written to entirely after **2026-08-10**, unnoticed for a month, while this repo alone took 130 commits in that window.

Model cost split (of $28,207.98 tracked):

| Model | Turns | Tokens | Cost | Share |
|---|---:|---:|---:|---:|
| claude-opus-4-8 | 14,799 | 6.52B | $12,788.50 | 45.3% |
| claude-opus-5 | 7,295 | 3.36B | $6,171.00 | 21.9% |
| claude-fable-5 | 5,319 | 2.37B | $4,749.91 | 16.8% |
| claude-sonnet-5 | 24,217 | 11.41B | $4,275.10 | 15.2% |
| claude-haiku-4-5 | 5,903 | 732M | $142.74 | 0.5% |

Opus 4.8 is 25% of turns but 45% of spend — worth a deliberate check against the "Sonnet default, Opus on-demand" routing policy rather than assuming it's still holding. Input:output tokens run 484:1 — usage here is overwhelmingly reading tool output/context, which is the right target for a compression proxy.

## 2. Headroom: real, but not where you'd think

`headroom doctor`, run live in this Claude Desktop session: **"claude desktop: agent sessions bypass the proxy (Desktop overwrites ANTHROPIC_BASE_URL)."** This session type is not compressed at all. Where it is active, lifetime measured savings are $0.19 (compression) + $12.36 (cache-read) = $12.55 total against $28,208 tracked spend — real, but under 0.05%. The binary is also version-drifted (0.35.0 running vs. 0.37.0 installed).

## 3. The real outcome ledger is git history, not a database

Since the token DB can't answer "did it work," mined git history instead — `global-market-research-platform` (this repo), 970 commits: 388 `feat` (40.0%), 201 `fix` (20.7%), 157 `docs` (16.2%), 20 `revert` (2.1%).

**116 commits (12% of all history) contain "silently," "never actually," or "nobody reads."** That's the actual outcome signal: roughly one commit in eight corrects something that looked finished and wasn't. 102 commits mention "stale" data, 16 mention "fabricat[ed]" values. Examples:

```
fix(test): CCC test required a column nobody reads, and lied about the damage
fix(global): highlights were 58 days stale — real cause was a silent seed fallback
fix(edgar): lock the store — two collections at once silently lost rows
fix: #6 the per-market floors were FABRICATED — remove them, keep only chosen policy
fix: XBRL P/E bands work — the 93% error was MY pandas bug, not the data
fix: single-run lock on daily_pipeline — concurrent runs were silently possible
```

Every one of these names the true root cause, not just the symptom — that discipline is itself a functioning outcome audit trail. Revert rate is low (2.1%): most fixes land clean the first time.

## 4. Fix : feat ratio, portfolio-wide (repos with real local git history)

| Repo | fix | feat | ratio |
|---|---:|---:|---:|
| mospi-dataset-analysis | 1 | 11 | 0.09 |
| digital-twin-for-ipa | 5 | 35 | 0.14 |
| BazaarTalks | 4 | 26 | 0.15 |
| india-trade-sector-policy-recommendations | 5 | 21 | 0.24 |
| global-stock-screener | 25 | 51 | 0.49 |
| **market-pipeline (this repo)** | **201** | **388** | **0.52** |
| Bazartalks_Py2Cplus | 9 | 17 | 0.53 |
| ship2shore_rag | 4 | 7 | 0.57 |

The highest ratios sit with the most architecturally ambitious, actively-changing work (a C++ migration, the flagship research repo) — not obviously a red flag on its own.

## 5. Coverage gaps, caught mid-audit

`repos.yaml` catalogues 52 repos. Roughly a third of the locally-matched directories live under `~/.graphify/gh-clones/` as **single-commit mirrors**, not full history — read naively they look dead; they're just unmirrored. **~18 of 52 catalogued repos have no local clone on this Mac at all** — this audit (and any local git-based audit) is blind to their activity.

## 6. Websites & other systems

- **masaladeutsch.blogspot.com** — 454 dated entries in `masaladeutsch-archive/CHANGELOG.md`, a real running per-post outcome log. Same discipline as the git pattern above, applied to a non-code deliverable.
- **qmd** — 150 files indexed, 809 vectors, but **344 orphaned embedding chunks (43%)** per `qmd status`; the `ruflo` collection is 40 days stale vs. a 2-day-fresh `memory` collection.
- **The memory system itself** — 90+ tiered `feedback_*`/`project_*` entries is already a running agile lessons-learned backlog; it just isn't framed as one.

## 7. Tool survey (self-hosted only, given private trading/financial repos)

| Tool | Fixes | Self-hosted | Effort |
|---|---|---|---|
| **Healthchecks.io** | Silent pipeline death | yes, BSD | trivial |
| **Langfuse** | Real success/failure + prompt versioning | yes, MIT | low–med |
| **Helicone** | Per-repo cost tagging | yes, OSS | low |
| **promptfoo** | Git-diffable prompt configs + CI tests | yes, MIT | low |
| **ccusage** | Free cross-check via Claude Code's own logs | yes | trivial |
| Arize Phoenix | Pure-OTel tracing/eval | yes | medium |
| LiteLLM Proxy | Hard per-project budget caps | yes (+Postgres) | medium |
| DSPy | Optimizes prompts against a metric | yes, local | med–high |
| LangSmith | Native Claude Agent SDK tracing | BYOC = enterprise | low, cloud |
| W&B Weave | Judge-scored evals | via W&B Server | medium |
| Braintrust | Polished eval workflow | enterprise-only | skip |

**Recommended, least new infra:** Healthchecks.io first (heartbeats on the token-logger + scheduled jobs), then promptfoo (git-diffable prompts + CI regression), then self-hosted Langfuse to replace what `token_usage.db` was trying and failing to do.

## 8. Sprint backlog

**Working:** root-cause commit messages · the memory system (already a retro log) · CHANGELOG discipline where it exists · repo governance (caught a real data exposure 2026-09-01) · low revert rate.

**Broken:** no outcome tracking · no per-repo attribution · tracker died silently for a month · Headroom inactive in Desktop sessions · ~18/52 repos uncloned locally.

**Next sprint:**
- P0 — Heartbeat the token-logger + scheduled jobs (Healthchecks.io)
- P0 — Fix `status`/`market` columns at source, or move to self-hosted Langfuse
- P1 — Pilot promptfoo on the two highest fix/feat repos
- P1 — `qmd cleanup` (344 orphaned chunks) + refresh the stale `ruflo` collection
- P2 — Reconcile `repos.yaml` against actual local clones

---
Sources: `~/.ruflo/data/token_usage.db` · `headroom doctor` (live) · `qmd status` · `repo-traffic-analytics/repos.yaml` · git log across 33 locally-cloned repos · web survey of 11 tools, Sept 2026.
