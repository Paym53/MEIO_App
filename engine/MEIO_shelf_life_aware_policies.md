# Shelf-life-aware (s,S) positions — design archive (not adopted)

> **Status (13.11., engine v18.1):** archived, not built (rule K22: inventory positions stay age-blind). Written on 10.11. under engine v17.2, when the fill-rate rule was still the chance rule (share of futures meeting the target ≥ chance level). Since v18.0 the rule is the expected fill rate per channel and week (H05) and the start policy is target-aware (K07); read the chance-level numbers below as of that date.

Status: **parked, not adopted (decision 10.11.2026).** The engine keeps age-blind inventory positions (K22) and relies on Seeker learning the age pattern through the weekly s,S values. This file keeps everything needed to build the age-aware variant later. Research report with all sources: `reports/Shelf life aware inventory policies.md`.

## 1. The problem in one paragraph
The simulation tracks every lot by remaining shelf life (F01–F06, FEFO per channel, H01–H03). The decision rule does not: the FG position (C01) and the raw-material echelon position (C03) count every lot that is usable this or next week, including stock that will expire unsold before an order placed now can be used. In the fast-moving test (FG shelf life 3, MOQ 6,000 ≈ 4.3 weeks of demand) 48–59% of the RM3 echelon position in the failing scenarios was such FG; RM3 was not reordered, RM3 on hand (≈ 2,000 kg) stayed below one lot's need (3,000 kg) and the DC ran empty in weeks 22–24.

## 2. Proposed definitions (would replace C01/C03, IDs F07/F08)
**F07 — shelf-life aware.** Every position an (s,S) rule is compared with counts a unit only if it is expected to be used before it expires.
- *Expire, FG:* remaining shelf life below the lowest channel requirement ρ_min (no channel can take it; the moment F05 scraps it).
- *Expire, RM k:* remaining shelf life below the production freshness rule φ_k (F03).
- *Expected use (F08):* deterministic FEFO projection at the review with information known then: FG — each week of the window, each channel in priority order (H02) takes its mean forecast demand from the lots it may take (H01); RM — consumption BOM_k × total mean forecast demand.
- *Window:* review week up to and including the arrival week of an order placed now: FG L_p,max; RM own stock L_k,max; FG part of the RM position L_k,max + L_p,max.
- *Pipeline:* counted in full (arrives fresh, FEFO sells older first). Limit: if the window exceeds the FG shelf life, pipeline FG could expire inside the window and is not counted.

**C01 (age-aware):** IP_FG = sellable DC stock + production pipeline − EW_FG(window L_p).
**C03 (age-aware):** IP_k = usable RM_k − EW_k(window L_k) + RM pipeline + BOM_k × (sellable FG + FG pipeline + this week's production − EW_FG(window L_k + L_p)).
One FG projection per week and scenario, read at each window; no double subtraction. Switch `SHELF_LIFE_AWARE_POSITIONS` (off = v17.1 exactly). Optional: switch on automatically only when the data shows short shelf life (lot's sellable quantity small vs lead time + lot size).

## 3. Which "expire" definition — evaluation
- **Loosest only** (pooled demand): matches the physics but counts D2C-only stock as cover for AMZ/B2B → strict channels short.
- **Strictest:** protects AMZ/B2B but treats stock D2C will still buy as lost → over-production, more waste; contradicts the physics.
- **Channel-aware (recommended):** expire = loosest; projection with channel-eligible demand. Equals loosest when all needs are equal, approaches strictest when lenient-channel demand is small.

Start policy, 2,000 fresh scenarios, NumPy, not re-optimized (rules met, cost; costs only comparable at equal rules, G04):

| Case | Today | Loosest | Strictest | Channel-aware |
|---|---|---|---|---|
| Slow template (FG 36; 24/16/24) | 89/90, 120.2k | 89/90, 120.2k | 89/90, 120.4k | 89/90, 120.2k |
| Fast MOQ 6,000 (all needs 0) | 60/69, 97.6k | 69/69, 144.0k | 69/69, 144.0k | 69/69, 144.0k |
| Mixed fast (FG 6; AMZ/B2B 3, D2C 0) | 63/69, 101k, waste 11.4k | 65/69, 102k, 12.8k | 69/69, 167k, 20.6k | 67/69, 118k, 14.4k |
| Mixed slow (FG 28; 24/16/24) | 59/90, 130k, 7.9k | 62/90, 131k, 7.7k | 84/90, 160k, 11.8k | 82/90, 152k, 10.8k |

FG and RM must be corrected **together**: FG only 57/69 (worse: earlier production uses RM3 that is still not reordered); RM only 63/69; both 69/69 (fast MOQ 6,000). Window vs full-life projection: identical results.

## 4. Literature (details and sources in the report)
- Haijema & Minner (2019) BSP-low-EW (base stock corrected for estimated waste): close to SDP-optimal, best in virtually all of 11,177 instances (abstract only; exact formula not verified).
- Haijema (2013) (s,S,q,Q): 4–25% better than (s,S) in many cases (structure, not age).
- After tuning, age-aware vs age-blind typically 1–4% cost, 10–20% less waste; large only with short shelf life vs lead time, high service targets, intermediate fixed costs.
- No source covers RM + FG shelf life with BOM, MOQ and per-week chance constraints; the echelon correction is our own design. To read before building: Haijema & Minner 2019 (doi 10.1016/j.ijpe.2018.03.008, section 3) and Management Science doi 10.1287/mnsc.2023.03801 (inventory position vs projected inventory level).

## 5. Cost and why it was parked
- Seeker Terms on the template: pooled +1,388 (+10.6%); channel-aware naive ≈ 4×, needs a running-sum formulation (gate ≤ +20%, fallback pooled).
- Effect on the template: none (nothing expected to expire). Benefit only for short shelf lives, and only measured on the start policy; the age-blind optimizer already reached 68/69 at chance level 0.95 on fast MOQ 2,000.

## 6. Gate to revisit
Re-optimize age-blind vs age-aware (NumPy step A/B, then Seeker) on the five data sets. Adopt only if it gives more rules met or ≥ about 2% lower cost at equal rules met, for products that will actually be planned. Trigger to look again: the v17.2 waste warning (L10) fires on a real product.

## 7. Deferred ideas
Batch-feasibility RM trigger (order when usable RM + pipeline < BOM × MOQ); strict-channel cover trigger (remaining gap in mixed cases); FG positions per channel freshness class; Haijema-style order cap Q.

## 8. Test assets (engine session)
`/tmp/audit2/defs.py` (projections and variants), `run_mixed.py`, `fgtest.py`; data `/tmp/fm/new.xlsx`, `/tmp/fm/moq2000.xlsx`, `/tmp/audit2/mixed_fast.xlsx`, `/tmp/audit2/mixed_slow.xlsx`; audit notes `research_notes/Shelf life aware inventory policies/engine_audit.md`.
