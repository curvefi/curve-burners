# JobBoard (Hooker v2)

dss-cron-style keeper job registry for Curve DAO. One contract per chain that
natively serves searchers and doubles as the FeeCollector hooker — integration
is a single `fee_collector.set_hooker(job_board)`; buffer mechanics, weekly
approve and duty semantics are preserved (`duty_act` `0x8c88eb86`,
`buffer_amount` `0x69e15fcb`, ERC165 `0xe569b44d`).

## Modules

* `JobBoard.vy` — orchestration: searcher API, Hooker compatibility, admin.
* `job_registry.vy` — job storage: DAO-fixed `foreplay` prefix + keeper data
  suffix; complex jobs live in wrapper contracts that `target` points to.
* `payments.vy` — payout engine: rewards priced in target, paid in exactly one
  token per call (target by default, or a rate-whitelisted token from the
  contract's pool; fund the native pool by plain transfer).
* `timing.vy` — week math + dutch reward curve.
* `adapters/` — thin backup-liveness wrappers: Gelato resolver, Chainlink
  custom-logic upkeep, Keep3r job. Native searchers need no adapter.

## Keeper flow

1. `active_jobs()` — static registry, poll rarely.
2. `workable(job_id, b"")` — cheap per-block hint: is there work worth
   preparing a payload for (period elapsed / transmittable count / canExecute).
3. Build payload off-chain (proofs, batches), `workable(job_id, data, token)` —
   honest quote capped by actual budget; simulation matches execution.
4. `work(inputs, receiver, payout_token)` — permissionless; dutch auction
   inside weekly windows gives price discovery without gas wars.

## Safety properties

* Per-job weekly `used/limit` caps in target terms bound worst-case spend
  regardless of checker bugs; target pulls are additionally capped by
  FeeCollector's weekly buffer allowance.
* All jobs are checker-gated, duties included: a duty with genuinely no work
  is skipped, so a reverting duty target cannot brick `FeeCollector.forward()`.
* `rate` bakes decimals in: target base units per 1e18 token base units
  (USDC at $1 => 1e30). Conservative, owner-set; dutch growth absorbs staleness.
* `set_jobs` resets cooldowns — execute at week boundaries.
