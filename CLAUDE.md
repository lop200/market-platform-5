# CLAUDE.md — Build Instructions
# US Stock & Options Decision Intelligence Platform (Owner: Khaled)

You are building this project according to the approved specification in
`SRS_Market_Intelligence_Platform_v1.1.md` (in this folder). Read it fully before writing any code.
The SRS is the single source of truth. If code and SRS conflict, the SRS wins.
If something in the SRS is ambiguous, ask the owner in Arabic before implementing.

## Language with the owner
Always communicate with the owner in Arabic. Code, comments, and commit messages in English.
All user-facing report output must support Arabic (RTL-native) by default.

## Non-negotiable rules (violating any of these = wrong implementation)
1. NO LLM math. All indicators, Greeks, levels, volatility, liquidity, and scores are computed
   in pure Python (pandas/numpy/py_vollib). The LLM only converts a finished JSON into prose.
2. Every score comes from an explicit formula (SRS section 12). Formulas must be inspectable
   via the API response (`score_formulas_ref`) and the UI ("لماذا هذه الدرجة؟").
3. Cost Gate before every paid external call (SRS section 16). There must be NO code path that
   reaches a market-data or LLM call without passing through `core/cost_gate.py`.
   Defaults: $1/day, $20/month, manual + automatic kill-switch. 100% test coverage on this module.
4. Adapter pattern for BOTH data providers and LLM providers (SRS 5.3).
   `LLM_PROVIDER=anthropic|openai` switches with one env var. No provider lock-in.
5. Legal wording layer (SRS section 19): outputs are educational analysis. Never generate
   imperative trade language ("اشترِ", "ادخل", "توصية", "buy now"). Enforce with the banned-words
   post-filter + system prompt rules. Levels are "مستويات فنية ملحوظة", stop = "مستوى الإبطال الفني".
6. Every analysis is persisted with its audit targets (SRS 7.1, 15). Never skip DB writes.
7. Numeric validation after LLM generation (SRS 13.4): any number in the report not present in
   the source JSON (with rounding tolerance) → regenerate once → fallback to code-built template.
8. Cache-first (SRS 17): check cache before any paid call. Cached reports show data age.

## Tech stack (fixed)
Python 3.12, FastAPI, SQLAlchemy 2.x, PostgreSQL (SQLite locally), Pydantic v2,
pandas + numpy + pandas-ta, py_vollib for Greeks, APScheduler for the audit job.
Frontend: single-page, RTL-first, Tajawal font, minimal — server-rendered Jinja2 is acceptable
for MVP. Deployment target: Render (env vars for all secrets; never commit secrets).

## Data providers (initial)
- Stocks: Alpaca Market Data free tier (IEX real-time) as primary; yfinance ONLY as dev fallback,
  never in production paths.
- Options (phase M5/M2-B): 15-min delayed chain from the cheapest adequate provider
  (evaluate Tradier / Polygon basic at current prices; confirm choice with owner before subscribing).
- News: deferred; do not implement in MVP.

## Build order
- M0 (foundation, do first): project skeleton per SRS 5.2, full DB schema per SRS 7.1,
  data + LLM adapters, cost gate + ledger, cache. Unit tests green before proceeding.
- Then IN PARALLEL (owner's decision, SRS Annex C):
  - Track A — M1: deterministic engine (SRS 10) + SMC module (Annex C-3: RSI divergence,
    order blocks, accumulation/distribution) + scoring (SRS 12). Validate indicator values
    against TradingView on 5 symbols before calling M1 done.
  - Track A — M2: LLM report engine + devil's advocate + numeric validation + web UI + cost meter.
  - Track B — M2-B: option-contract screenshot module (Annex C-2): vision extraction →
    live underlying data → invalidation level, expected move, Delta/Gamma translation of stock
    levels to contract prices, daily Theta decay → exit/target levels report (legal wording).
- M3: activate the scheduled self-audit job (SRS 15) — tables and target extraction already
  exist from M0/M1; this milestone only turns on the scheduler + accuracy_stats updates.

## Definition of done for MVP
`POST /api/v1/analyze {"symbol":"NVDA","lang":"ar"}` returns a full Arabic report in ≤15s,
cost ≤ $0.05 (target ~$0.02), all scores formula-backed, analysis + audit targets saved,
cost meter accurate, kill-switch works, banned-words filter passes on all generated reports,
and the option-screenshot endpoint returns exit/target levels from an uploaded contract image.

## Testing (SRS 23)
pytest for every indicator vs hand-computed references; golden-file JSON snapshots;
cost-gate exhaustive cases; LLM validation with injected fake numbers; banned-words CI check.
Do not mark a milestone complete with failing tests.

## Working style
Work milestone by milestone. At the end of each session, summarize in Arabic: what was built,
what passed tests, current estimated cost per report, and what is next. Never invent scope
beyond the SRS without owner approval.
