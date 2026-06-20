# LCM Arm-B — Live Node-Served Long-Session Recovery

Model: claude-haiku-4-5 · Profile: aegis · N: 80
Gate-eligible (N>=180): False
**Verdict: FAIL**

## Gate summary
- Condensation fired (>=1 depth-1 node): 76/80
- Fact preserved in a depth>=1 node: 76/80
- Node-served recall: 69/80 (0.8625)
- Wilson 95% lower bound: 0.7703 (required >= 0.90)
- Confident-wrong: 0 (required 0)
- Duration: 13140.9s

## PRD-8.2 split gates (recovery-correctness vs condensation-reliability)
**Recovery verdict: FAIL** (binding Apollo gate — node formed, infra errors excluded)
- Recovery-eligible trials (node formed, no infra error): 76/80
- Recovery recall: 0.9079 (required >= 0.95)
- Recovery Wilson 95% LB: 0.8219 (required >= 0.90)
- Confident-wrong: 0 (required 0)

Trial outcome breakdown:
- recovered (correct): 69
- recovery_miss (node formed, fact preserved, not recovered — REAL gap): 7
- confident_wrong (asserted wrong owner): 0
- no_condensation (no node ever formed — condensation-trigger gap, NOT recovery): 4
- infra_error (transient 5xx/429/timeout — retryable, excluded): 0
- Condensation reliability (non-infra): 0.95

## Trial records
| idx | session | node | sentinel_in_node | correct | leaves | condensed |
|---|---|---|---|---|---|---|
| 0 | 20260619_064245_ef6ef2 | 5 | True | True | 0 | 1 |
| 1 | 20260619_064245_ef6ef2 | 5 | True | True | 0 | 1 |
| 2 | 20260619_064841_3ca499 | 10 | True | True | 0 | 1 |
| 3 | 20260619_064841_3ca499 | 10 | True | True | 0 | 1 |
| 4 | 20260619_065424_4de254 | 15 | True | True | 0 | 1 |
| 5 | 20260619_065424_4de254 | 15 | True | True | 0 | 1 |
| 6 | 20260619_065917_c3873e | 20 | True | True | 0 | 1 |
| 7 | 20260619_065917_c3873e | 20 | True | True | 0 | 1 |
| 8 | 20260619_070429_db9be8 | 25 | True | True | 0 | 1 |
| 9 | 20260619_070429_db9be8 | 25 | True | True | 0 | 1 |
| 10 | 20260619_070922_9707eb | 30 | True | True | 0 | 1 |
| 11 | 20260619_070922_9707eb | 30 | True | True | 0 | 1 |
| 12 | 20260619_071510_10dd31 | 35 | True | True | 0 | 1 |
| 13 | 20260619_071510_10dd31 | 35 | True | True | 0 | 1 |
| 14 | 20260619_072043_8983bf | 41 | True | True | 0 | 1 |
| 15 | 20260619_072043_8983bf | 41 | True | True | 0 | 1 |
| 16 | 20260619_072607_e5530d | 46 | True | True | 0 | 1 |
| 17 | 20260619_072607_e5530d | 46 | True | True | 0 | 1 |
| 18 | 20260619_073121_227669 | 51 | True | True | 0 | 1 |
| 19 | 20260619_073121_227669 | 51 | True | True | 0 | 1 |
| 20 | 20260619_073657_3e773a | 56 | True | True | 0 | 1 |
| 21 | 20260619_073657_3e773a | 56 | True | True | 0 | 1 |
| 22 | 20260619_074304_f6e486 | 61 | True | True | 0 | 1 |
| 23 | 20260619_074304_f6e486 | 61 | True | True | 0 | 1 |
| 24 | 20260619_074853_fb51a0 | 66 | True | True | 0 | 1 |
| 25 | 20260619_074853_fb51a0 | 66 | True | True | 0 | 1 |
| 26 | 20260619_075401_2884e7 | 71 | True | True | 0 | 1 |
| 27 | 20260619_075401_2884e7 | 71 | True | True | 0 | 1 |
| 28 | 20260619_074304_f6e486 | 61 | True | False | 0 | 1 |
| 29 | 20260619_074304_f6e486 | 61 | True | False | 0 | 1 |
| 30 | 20260619_080722_886966 | 82 | True | True | 0 | 1 |
| 31 | 20260619_080722_886966 | 82 | True | True | 0 | 1 |
| 32 | 20260619_081341_e35673 | 87 | True | True | 0 | 1 |
| 33 | 20260619_081341_e35673 | 87 | True | True | 0 | 1 |
| 34 | 20260619_081914_44fbc5 | 92 | True | True | 0 | 1 |
| 35 | 20260619_081914_44fbc5 | 92 | True | True | 0 | 1 |
| 36 | 20260619_082458_ca1ab2 | 97 | True | True | 0 | 1 |
| 37 | 20260619_082458_ca1ab2 | 97 | True | True | 0 | 1 |
| 38 | 20260619_083018_cfefd1 | 102 | True | True | 0 | 1 |
| 39 | 20260619_083018_cfefd1 | 102 | True | True | 0 | 1 |
| 40 | None | None | False | False | 0 | 0 |
| 41 | None | None | False | False | 0 | 0 |
| 42 | None | None | False | False | 0 | 0 |
| 43 | None | None | False | False | 0 | 0 |
| 44 | 20260619_083822_267d82 | 107 | True | True | 0 | 1 |
| 45 | 20260619_083822_267d82 | 107 | True | True | 0 | 1 |
| 46 | 20260619_084549_a714cb | 112 | True | True | 0 | 1 |
| 47 | 20260619_084549_a714cb | 112 | True | True | 0 | 1 |
| 48 | 20260619_085141_1b5206 | 118 | True | True | 0 | 1 |
| 49 | 20260619_085141_1b5206 | 118 | True | True | 0 | 1 |
| 50 | 20260619_084549_a714cb | 112 | True | False | 0 | 1 |
| 51 | 20260619_084549_a714cb | 112 | True | False | 0 | 1 |
| 52 | 20260619_090308_86a752 | 130 | True | True | 0 | 1 |
| 53 | 20260619_090308_86a752 | 130 | True | True | 0 | 1 |
| 54 | 20260619_090745_afb80b | 135 | True | True | 0 | 1 |
| 55 | 20260619_090745_afb80b | 135 | True | True | 0 | 1 |
| 56 | 20260619_091316_805fb0 | 140 | True | True | 0 | 1 |
| 57 | 20260619_091316_805fb0 | 140 | True | True | 0 | 1 |
| 58 | 20260619_091801_535622 | 145 | True | True | 0 | 1 |
| 59 | 20260619_091801_535622 | 145 | True | True | 0 | 1 |
| 60 | 20260619_085141_1b5206 | 118 | True | True | 0 | 1 |
| 61 | 20260619_085141_1b5206 | 118 | True | False | 0 | 1 |
| 62 | 20260619_092803_0890aa | 155 | True | True | 0 | 1 |
| 63 | 20260619_092803_0890aa | 155 | True | True | 0 | 1 |
| 64 | 20260619_093403_268b60 | 160 | True | True | 0 | 1 |
| 65 | 20260619_093403_268b60 | 160 | True | True | 0 | 1 |
| 66 | 20260619_084549_a714cb | 112 | True | True | 0 | 1 |
| 67 | 20260619_084549_a714cb | 112 | True | True | 0 | 1 |
| 68 | 20260619_094616_bcc0f8 | 172 | True | True | 0 | 1 |
| 69 | 20260619_094616_bcc0f8 | 172 | True | True | 0 | 1 |
| 70 | 20260619_095136_01cb2e | 177 | True | True | 0 | 1 |
| 71 | 20260619_095136_01cb2e | 177 | True | True | 0 | 1 |
| 72 | 20260619_095727_7a5107 | 182 | True | True | 0 | 1 |
| 73 | 20260619_095727_7a5107 | 182 | True | True | 0 | 1 |
| 74 | 20260619_092803_0890aa | 155 | True | False | 0 | 1 |
| 75 | 20260619_084549_a714cb | 112 | True | True | 0 | 1 |
| 76 | 20260619_093403_268b60 | 160 | True | False | 0 | 1 |
| 77 | 20260619_093403_268b60 | 160 | True | True | 0 | 1 |
| 78 | 20260619_101612_c956a1 | 198 | True | True | 0 | 1 |
| 79 | 20260619_101612_c956a1 | 198 | True | True | 0 | 1 |
