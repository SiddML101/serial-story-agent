# Cost & latency report

All calls ran on the **free Gemini tier**: actual spend is **$0**. Prices below are list prices (what the same tokens would cost on a paid tier, from `story/config.py` PRICING), so the estimate is realistic.

## By step

| step | calls | failed attempts | tokens in | tokens out | list $ | avg latency s |
|---|---:|---:|---:|---:|---:|---:|
| draft | 17 | 39 | 98,046 | 55,322 | 0.1927 | 24.5 |
| revise | 14 | 24 | 105,434 | 42,887 | 0.1685 | 22.6 |
| plan_beats_act4 | 1 | 8 | 11,848 | 13,796 | 0.0808 | 107.5 |
| plan_beats_act5 | 1 | 2 | 4,184 | 13,084 | 0.0696 | 52.8 |
| plan_beats_act3 | 1 | 2 | 3,986 | 9,530 | 0.0516 | 46.5 |
| plan_foundation | 1 | 15 | 2,134 | 9,854 | 0.0514 | 44.0 |
| plan_beats_act2 | 1 | 8 | 4,097 | 7,787 | 0.0430 | 81.0 |
| plan_beats_act1 | 1 | 12 | 1,934 | 7,586 | 0.0399 | 33.2 |
| extract | 32 | 0 | 111,384 | 22,460 | 0.0201 | 3.4 |
| critic | 31 | 0 | 146,392 | 3,700 | 0.0161 | 1.9 |
| replan_arc | 1 | 13 | 2,974 | 4,902 | 0.0131 | 29.3 |
| feedback_router | 3 | 0 | 6,695 | 930 | 0.0010 | 2.2 |
| arc_audit | 1 | 0 | 8,371 | 233 | 0.0009 | 2.0 |
| arc_summary | 1 | 0 | 1,691 | 394 | 0.0003 | 2.7 |

## By model

| model | calls | failed attempts | tokens out | list $ |
|---|---:|---:|---:|---:|
| gemini-3.8-flash | 2 | 26 | 5,621 | 0.0233 |
| gemini-3.7-flash | 0 | 43 | 0 | 0.0000 |
| gemini-3.5-flash | 16 | 28 | 86,778 | 0.3582 |
| gemini-3.6-flash | 20 | 26 | 72,349 | 0.3292 |
| gemini-3.5-flash-lite | 68 | 0 | 27,717 | 0.0385 |

## Per episode

| ep | calls | failed attempts | tokens | list $ | model time s |
|---:|---:|---:|---:|---:|---:|
| 1 | 3 | 1 | 9,649 | 0.0075 | 25 |
| 2 | 3 | 1 | 11,434 | 0.0086 | 31 |
| 3 | 6 | 2 | 28,183 | 0.0247 | 57 |
| 4 | 3 | 1 | 13,859 | 0.0105 | 22 |
| 5 | 3 | 2 | 15,773 | 0.0139 | 28 |
| 6 | 9 | 1 | 54,315 | 0.0438 | 73 |
| 7 | 9 | 7 | 55,989 | 0.0375 | 95 |
| 8 | 3 | 4 | 19,170 | 0.0127 | 56 |
| 9 | 10 | 14 | 65,580 | 0.0380 | 123 |
| 10 | 3 | 5 | 20,281 | 0.0120 | 31 |
| 11 | 6 | 9 | 41,898 | 0.0276 | 56 |
| 12 | 9 | 13 | 63,705 | 0.0398 | 102 |
| 13 | 6 | 2 | 41,811 | 0.0286 | 44 |
| 14 | 15 | 1 | 103,520 | 0.0665 | 101 |
| 15 | 6 | 0 | 40,458 | 0.0257 | 55 |

## Projection to 200 episodes

Measured over 15 episodes.

```
total = per_episode × 200 + planning + arc_boundary × 20
      = $0.0265 × 200 + $0.3364 + $0.0144 × 20
      ≈ $5.92 at list price (actual: $0 on the free tier)
model time ≈ 60s × 200 + planning + boundaries ≈ 3.6 h (excluding human review and rate-limit waits)
```

- Tokens per episode: ~39,042; writer calls per episode: 2.07 (draft + revisions).
- **Free-tier bottleneck is requests, not dollars:** ~439 writer requests at 20/day/model × 4 models ≈ **5.5 days** of quota for all 200 episodes.

## Decisions logged

- human: 25
- context_built: 19
- check_failed: 18
- checks_passed: 8
- critic_unverified: 5
- checks_passed_after_1_revisions: 4
- feedback_routed: 3
- extract_sanitized: 1
- arc_audit: 1
- arc_boundary_replan_failed: 1
- replan_arc2: 1
- checks_passed_after_2_revisions: 1
