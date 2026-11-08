---
type: Reference
title: BMC Credential Rotation Workflow
description: How the mprov_jobserver bmc-rotate module changes, checks, or recovers BMC credentials queued by the mPCC over Redfish or IPMI, including secret scrubbing, result reporting, concurrency, and timeouts.
tags: [mprov, jobserver, bmc, redfish, ipmi, ipmitool, credentials, security]
resource: src/mprov_jobserver/plugins/bmc_rotate.py
---

# BMC Credential Rotation (`bmc-rotate`)

`src/mprov_jobserver/plugins/bmc_rotate.py` (added in 0.7.0, #341 / mprov_control_center#486) changes, checks, or recovers BMC credentials on the mPCC's behalf. The mPCC queues one job per BMC, and only a job server with an address on that BMC's network can claim it (network-scoped claiming, see [Architecture Overview](../architecture/overview.md)). Unlike the other job modules it needs no artifact plugins — but it does talk to the mPCC with the job server's own certificate ([Identity and Certificate Enrollment](../architecture/identity.md)).

## Per-job flow

1. `GET bmcrotations/<id>/secret/` — the BMC address plus the credentials: `{kind: change|check|find, current, new, candidates}`. The response **never goes to a log**. A 409 means the request is already finished; the job is marked successful.
2. Talk to the BMC — **Redfish first** (HTTP basic auth over HTTPS); IPMI (`ipmitool`) only when the BMC has **no Redfish service**, never as a retry after Redfish refuses the credentials (a refusal is an answer).
   - **change**: log in with `current`, set the new password and log in with it; if the user name also changes, rename and log in again.
   - **check**: log in with `current`.
   - **find**: try each `candidates` pair in order; the first that logs in wins.
3. `POST bmcrotations/<id>/result/` with `{status, method, verified, tls_verified, error}`. `verified` names the pair that actually logged in (`current`, `password_only`, `new`, or a candidate's index) — the only pair the mPCC stores. The POST is retried (5 tries, 2s apart); if it still can't be delivered the job fails and the mPCC treats the change as unconfirmed.

## Secrets never leak

Every password is scrubbed (`scrub()`, replacing each secret with `***`) from any error before it is logged or reported. `ipmitool` never receives a password in argv or the environment: the login password goes via `-f <file>` and the new credentials through `ipmitool exec`, each from a 0600 file in a 0700 temp directory removed afterwards. Jobs are claimed with the normal `job-claim` protocol, and a per-job exception fails only that job.

## Config (`plugins/bmc-rotate.yaml`)

- `verifyBMC` (default off) — check BMC HTTPS certificates via Redfish; BMCs mostly ship self-signed ones, and the mPCC's report flags a change made without checking. `bmcCABundle` overrides the CA used.
- `parallel` (4) — BMCs worked on at once via a `ThreadPoolExecutor` inside one claimed batch.
- `ipmiChannel` (1), `ipmiInterface` (`lanplus`).
- `timeout` (60s, raised in #341) — BMCs can take half a minute to apply an account change; one slower than this is reported unconfirmed, and the mPCC's "find which credentials work" recovers it.

IPMI-vs-Redfish discrimination matters: `IPMI_REFUSED` matches RAKP/unauthorized/invalid-credential errors (credentials refused) as distinct from the BMC not answering at all (`Unreachable`), which decides whether the job fails or moves to the next method.

## Where to start when changing this area

- `tests/test_bmc_rotate.py` exercises the flow with stubbed Redfish/IPMI; the mPCC side (`bmcrotations/` API) lives in the separate `mprov_control_center` repo.
- Any change here must keep the scrubbing discipline: nothing secret may reach a log, argv, environment, or the result payload.
- The module relies on `js.mpcc_unreachable()` (#337) and `js.claim_job()` from the [Plugin Framework](../architecture/plugin-framework.md).
