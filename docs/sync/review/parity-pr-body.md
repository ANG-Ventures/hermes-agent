## Parity sync

- target SHA: 26350357d76e4508c8df9304a3374bdc5a6f6220
- merge-base: 1e5b50744094959db5536eca9df3881d13fd28d8
- bucket stats: `{"arch_split": 6, "files": 184, "hunks": 1725, "mechanical": 7, "semantic": 171}`
- force reason: local suite corpus cannot go green in the shared venv: 230/230 residual reds triaged = env-staleness (venv predates upstream dep floors: acp, nemo-relay>=0.7.1, mcp 2.0 dev extra; 1,605+229 of them re-proven PASS under uv run with CI extras) + 3 real kanban-wake reds fixed (delivery_mode gate) + 1 contract test updated with phantom-safety assertions upgraded. All 4 ladder gates PASS; D10 0 lost; canaries 92/92 on staged tree. Binding suite verdict = heavy-ci on the PR (real locked env).

## Behavior changes


## Validation

- `python3.11 -m hermes_parity gates --resume`
