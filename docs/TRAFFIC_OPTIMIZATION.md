# Traffic reduction and console redesign — implementation contract

Approved 2026-10-07 (Asia/Shanghai). Preserve 11 enabled persistent exits, fail-closed
networking, distinct regional exits, random selection of at most five unfailed candidates,
rollback, Telegram confirmation and five-failure intervention alerts.

## Completion evidence

- [x] Persist traffic totals and per-layer accounting; handle restart/reset/long connections.
- [x] 5-minute lightweight health checks; full validation at connection/change/6 hours.
- [x] Independent endpoint fallback, bounded responses and whole-probe timeout.
- [x] 60-minute discovery, compressed streaming, coalescing and recovery refresh.
- [x] Disable healthy-route optimization by default; shared exclusions and task scheduling.
- [x] Persist recovery backoff (1/2/5/15/30 minutes), stagger and cap concurrency.
- [x] Measure tunnel broadcast overhead without payload capture; exclude noisy candidates.
- [x] Replace persistently noisy automatic exits with cooldown; notify for locked exits.
- [x] Clean temporary tunnels on cancellation, rollback and startup.
- [x] Soft budget controls optional work, never disables essential recovery.
- [x] API for policy, traffic (today/24h/7d), activity pagination and incremental health.
- [x] Rebuild desktop/mobile console: exits, traffic, activity, settings, light/dark.
- [x] Actual exit IP and recent 2-hour x/y success; hide disabled entries by default.
- [x] Confirm switches; show stages/errors/rollback; retain SOCKS/TG/backup/account.
- [x] Targeted event updates, background suspension, fallback polling and pagination.
- [x] Regression tests, desktop/mobile visual and interaction verification, design docs.
- [x] Release/deploy to JP-AWS with backup/rollback and real authenticated SOCKS tests.
- [ ] Collect full baseline and post-change 24h evidence; report measured reductions.

Baseline read-only inspection: 7,326 active health probes, 252 candidate probes and
143 discoveries in 24h. Feed sample 965,746 compressed bytes. DHCP capture 319,737
payload bytes / 20s across eleven tunnels (short sample, not a daily measurement).
Public client observed during sampling was confirmed to belong to the operator.

Targets for stable operation: 75–80% fewer health HTTP requests, ~80% less scheduled
feed traffic, no routine healthy-candidate tunnels, measured/estimated diagnostics
under 100 MiB/day where possible. VPS total reduction requires separate evidence;
wire totals, tunnel IP bytes, proxy payload and HTTP body bytes must not be summed.

Idle disconnection remains opt-in and is not part of the default persistent service.

Release validation (2026-10-07): `v0.1.12` is active on JP-AWS as
`20261007-065711-a086cb1`; the installer created a rollback backup, all four Gate
services are active, health live/ready checks return `200`, and all 11 enabled
authenticated SOCKS entrances returned the same IP recorded for their active exit.
The deployment backup is `/var/backups/gate/gate-20261007-065711-a086cb1-20261006-225751/`.
The 24-hour traffic comparison remains open until the passive sampler has a full
post-change window.
