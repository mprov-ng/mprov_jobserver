---
type: Reference
title: mPCC Identity and Certificate Enrollment
description: How the mprov_jobserver authenticates to the mPCC with its own TLS certificate from 0.7.0 — one-time-token enrollment, CA trust checks, automatic renewal, refused-certificate recovery, the deprecated shared API key fallback, a booted node's certificate, and the 2-hour image-build credential.
tags: [mprov, jobserver, identity, certificates, mTLS, enrollment, security, tls]
resource: src/mprov_jobserver/identity.py
---

# Identity: Certificates Replace the Shared API Key

Since 0.7.0 (mprov_control_center#381, README *"Enrolling with the mPCC"*) each job server has **its own identity** instead of the API key every agent shared: an EC P-256 key it generates itself — which never leaves the host, the mPCC only ever sees the CSR — and a client certificate the mPCC's CA signs, valid 90 days. `src/mprov_jobserver/identity.py` implements the credential mechanics; `JobServer.use_identity()` (`src/mprov_jobserver/app.py`) picks among them on every session start.

## How the job server authenticates, in order

`use_identity()` tries, in order: **this job server's certificate** → **a new one enrolled with a token** → **the shared API key** (deprecated; `MPROV_SHARED_KEY_MODE=off` on the mPCC refuses it, and 0.8.0 drops it). `self.auth` records the outcome (`'cert'` / `'apikey'`) and `self.tlsPaths` carries the `{cert, key, ca}` file paths that get handed to scripts (see [Script Execution](../workflows/script-execution.md)). A runner that doesn't register (`-d`, `-m`) never enrolls, but uses a certificate already present — including a **node's** certificate (below) — and sends no `Authorization` header at all when there is no key and no certificate, so unauthenticated endpoints still answer.

- **Enrollment (first start)**: `POST enroll/jobserver/ {token, csr, host_id, name}` over HTTPS with a one-time token from the mPCC (`manage.py mprov_enroll_token`, which prints the CA fingerprint). The token comes from global `enrollToken` / `MPROV_ENROLL_TOKEN`, else the first line of `enrollTokenFile` (default: `enroll_token` next to the config — the compose stack's mPCC writes one there). The token is a secret, so `enrollment_trust()` **never sends it without checking the mPCC's certificate first**: against `caBundle`, against the CA fetched from `enroll/ca.crt` and matched to global `caFingerprint` / `MPROV_CA_FINGERPRINT` (fetched unverified on purpose — the SHA-256 fingerprint is the check), or against the mPCC's `ca.crt` next to the config. Enrolling needs an `https://` `mprovURL` and a host id (see [Architecture Overview](overview.md) registration).
- **The credential** lives in `tlsDir` / `MPROV_TLS_DIR` (default `/var/lib/mprov/tls`): `jobserver.pem` (key+cert together, 0600, so renewal replaces both atomically via a temp-file rename) and `ca.crt`. Losing it means needing a new token; keep `tlsDir` on storage that survives container recreation.
- **Renewal**: checked at most once an hour (`maybe_renew()`), renewed with a **new key** in the certificate's last 29 days via `POST enroll/renew/` authenticated by the current certificate; a 409 "not due yet" is quietly ignored.
- **A refused certificate** (401 with `TLS-Client-Certificate` in `WWW-Authenticate`: revoked, expired, or its job server deleted) is dropped by fingerprint so it isn't retried, and the job server enrolls again with the next token it finds — in a compose stack the mPCC writes a fresh one at its next start. A 403 against the shared key triggers the same pivot, but at the next heartbeat instead of within the hour.
- **Without any of them** the daemon says why once (`identity_problem()`, not per retry) and keeps trying each heartbeat.

Global config keys: `tlsDir`, `enrollToken`, `enrollTokenFile`, `caFingerprint`, `nodeTlsDir`; the deprecated `apikey` remains only as the fallback. `cryptography` (≥42) is the new hard dependency for all of this (`requirements.txt`).

## Other identities the job server deals with

- **A booted node** (`node_credential()`): the mPCC's boot scripts enroll each node as it boots, keeping `node.key`/`node.crt`/`ca.crt` in `/etc/mprov/tls` (`nodeTlsDir`); the mPCC renews it, the job server only *uses* it. A runonce script runner on a node authenticates with it and switches its plain-HTTP boot URL to HTTPS on the same host (`https_url()`); `MProvScript` does the same in `script.py` (see [Script Execution](../workflows/script-execution.md)). Images built before 0.7.0 have no node certificate and still use their baked key.
- **An image build** (`build_credential()`): a job server authenticating by certificate never puts the fleet API key into a built image. Instead `image-update` requests a **2-hour credential scoped to read only that image** (`POST enroll/build/`), stores it in the image's `/tmp/mprov/tls` for the image-gen script runner, and removes it before packing — the build fails if the credential can't be obtained or can't be removed (see [Image & Repo Provisioning](../workflows/image-provisioning.md)).
- **iPXE can't do this at all**: NIC ROMs can't check the mPCC's CA, so dnsmasq's generated boot files point at the plain-HTTP form of the mPCC URL (`http_url()`; see [Network Services](../workflows/network-services.md)).

## Where to start when changing this area

- Auth-flow changes: `use_identity()` / `enroll()` / `auth_refused()` / `maybe_renew()` in `app.py`, tested by `tests/test_app_identity.py`; the credential mechanics (CSR, fingerprint normalisation, token reading, URL rewriting) are in `identity.py` with `tests/test_identity.py`.
- The mPCC endpoints are `enroll/jobserver/`, `enroll/renew/`, `enroll/build/`, `enroll/ca.crt` (mPCC-side implementation lives in the separate `mprov_control_center` repo; semantics here are inferred from client code).
- Never log token, key, or certificate contents; existing code logs fingerprints, expiry dates, and a non-secret token id (`token_id()`) only.
