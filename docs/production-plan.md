# Productionizing Ada: first principles, then AWS's own rules

*Written 2026-09-15 after Pat dropped the hackathon constraint ("I need to
start selling this"). Read with `docs/agent-harness.md` (what the product
does) and `docs/harness-prior-art.md`. Everything below is checked against
the tree, not aspirational.*

## 1. What is actually wrong, from first principles

Amazon's working-backwards question is "what does the customer receive, and
what breaks first when a second customer arrives?" Ada's customer receives a
KiCad project, a case and a BOM, verified by KiCad and a receipt. What breaks
with a second customer:

| Problem | Where it is | Why it blocks selling |
|---|---|---|
| **One shared bearer token for the whole service** | `service/app.py:182-210` | there is no notion of *who* is calling: no per-customer keys, no revocation, no usage attribution |
| **No account** | `billing/accounts.py::SingleAccountResolver` | the ledger, holds and Stripe customers all hang off "the person at this machine" |
| **Ledger is SQLite on local disk** | `billing/sqlite_ledger.py` (its own docstring: not safe as two replicas) | money cannot be reconciled across instances; a redeploy loses the file unless a volume is mounted |
| **Run registry and fact cache are process memory / Firestore** | `service/runs.py`, `service/cache.py` | a second replica cannot see a run the first started; Firestore ties the deploy to GCP |
| **Artifacts are local files** | `service/steps.py` writes beside the project | nothing a customer can fetch later, share, or that survives the container |
| **Secrets are `.env` on a laptop** | `cli._load_dotenv`, `envfiles.apply_saved_env` | fine for a developer; a container needs them injected from a secrets store at start |
| **No structured logs, no request ids in logs, no metrics** | `service/app.py` prints | an incident cannot be reconstructed; usage cannot be billed from logs |
| **Deploy is an operator script to Cloud Run, currently down** | `scripts/deploy.sh`, README | no environment is reproducible from code; the recorded URL answers 500 |
| **The desktop app is unsigned and has no updater** | `docs/release.md` "What is not here", `tauri.conf.json` `createUpdaterArtifacts: false` | macOS shows "unidentified developer"; customers never get fixes |
| **The MCP endpoint is a Cloudflare quick tunnel** | `scripts/mcp_tunnel.sh` | URL changes per restart; not a product surface |

Things that are *not* wrong and stay: the engine's integer-nanometre kernel,
the verifier gate, the offline test discipline, the stdlib HTTP server (it
works in a container; a framework would add nothing a load balancer does not).

Cut list (delete rather than carry): the dead `desktop/` Tauri shell and
sidecar, the second and third `kicad-cli` finders, the two `Effort` enums'
overlap, the tunnel script once a fixed domain exists.

## 2. AWS Well-Architected, pillar by pillar, applied to Ada

Each row is a concrete change, not a principle.

### Operational excellence
- Infrastructure as code: `infra/` (AWS CDK, Python) defines VPC, ECS
  Fargate service, ALB, RDS Postgres, S3, Secrets Manager, CloudFront.
  Nothing is clicked in the console; `cdk diff` is the change review.
- One CI path builds the image, runs the suite, pushes to ECR on a tag, and
  `cdk deploy` promotes it. `scripts/deploy.sh` retires.
- Structured JSON logs with `run_id`, `account_id`, `request_id` on every
  line, shipped to CloudWatch; `/healthz` (process up) and `/readyz`
  (database reachable, `kicad-cli` present) already exist and become the
  ALB health checks.
- Runbooks in `docs/runbooks/`: a stuck run, a Stripe webhook backlog, a
  model provider outage (the failover ladder already handles the last).

### Security
- **Never root.** Create an IAM user (or Identity Center user) with an
  admin policy for you, and a *deployment role* CDK assumes. Root
  credentials go back in the safe with MFA. Today's CLI identity is root.
- Per-customer API keys: `service/auth.py` issues keys, stores a salted hash
  in Postgres, resolves them to an `AccountId`, and replaces the shared
  bearer. `X-Kaleo-Run-Id` stays an address, never an identity.
- Secrets in Secrets Manager, injected as ECS task environment; the
  container never reads a `.env`. Model keys, Stripe keys, webhook secrets.
- Least privilege for the task role: `bedrock:InvokeModel` on the two
  inference profiles, `s3:PutObject/GetObject` on one bucket prefix,
  `secretsmanager:GetSecretValue` on named secrets.
- The existing guards stay: HMAC before parse on every webhook, host
  allowlists on every transport, no secret in any log or error string.
- TLS at CloudFront and the ALB; the container speaks plain HTTP inside the
  VPC only.

### Reliability
- Postgres behind the existing storage seams: `PostgresLedger` overrides the
  six-method seam `sqlite_ledger.py` already isolates; `runs.py` gets a table
  with the same `RunRecord` shape; `cache.py` gets a table instead of
  Firestore. The 250-op ledger fuzz runs against Postgres in CI (a service
  container), so the invariants hold on the real store.
- Two Fargate tasks minimum behind the ALB; a run belongs to the task that
  started it, and `GET /runs/<id>` is answered from Postgres by any task.
- Stripe idempotency (already keyed by Checkout Session) survives restarts
  once the ledger is in Postgres, which is the case the SQLite docstring
  warns about.
- Provider failover (`resilience.py`) and per-provider cooldowns already
  exist; add Bedrock as a rung beside the API key so an Anthropic-side
  incident fails over to the AWS-side endpoint.

### Performance efficiency
- Task size from measurement: CP-SAT at `thorough` is 45 s of one core;
  OCCT and `kicad-cli` are single-threaded. Start at 2 vCPU / 4 GB and let
  `board_eval.py` timings on the task decide.
- Artifacts to S3 with presigned URLs; the desktop and Slack fetch by link
  instead of the service streaming files.
- Model calls are the long pole; the receipt already records tokens and
  seconds per run, which is the metric to watch.

### Cost optimization
- Usage-based billing already exists (KCU, metered engine seconds); connect
  `service/metering.py` to the per-account ledger and the receipt's model
  token counts so cost of goods per run is known.
- Bedrock and the Anthropic API are both wired; route by price per tier when
  both are configured. Cheap stages stay on the cheap tier (`effort.py`).
- Fargate Spot for non-interactive runs (Slack, Jira, meetings) later; not
  before the run registry is durable.

### Sustainability
- Nothing specific beyond right-sizing and scale-to-two; recorded so the
  pillar is not silently skipped.

## 3. What "selling" needs that the code does not have

1. **Identity**: API keys per account (above). Without it, nothing else can
   be metered or revoked.
2. **Checkout to key**: Stripe Checkout already grants KCU to "the account";
   the success page has to hand the customer their API key and the desktop
   app has to accept it in Settings. The Stripe CLI is installed
   (`stripe listen` forwards webhooks to the local service for testing).
3. **A signed, notarized desktop build with an updater endpoint**: Apple
   Developer ID certificate, `tauri signer` keypair, `createUpdaterArtifacts`
   on, the update manifest served from S3 behind CloudFront. Until then the
   download is the unsigned `.dmg` on GitHub Releases and users must
   right-click-open it.
4. **A website** with the demo, the download, and the checkout link
   (`site/`, static, hosted on S3 + CloudFront under the company domain).
5. **Terms, privacy, and what leaves the machine**: a page stating that a
   run sends the circuit request and datasheets to the model provider and
   nothing else, because a hardware team will ask.

## 4. Order

1. **Website** (`site/`): static, no build step, Ada's own copy, existing
   demo media, download link to the latest release, checkout link once
   Stripe is live. Shippable on S3 today.
2. **Identity + Postgres**: `service/auth.py` (API keys), `PostgresLedger`,
   Postgres run registry and fact cache, all behind the existing seams with
   the existing tests re-run against Postgres.
3. **Infrastructure as code**: `infra/` CDK stack; CI builds and pushes the
   image; first environment `staging`.
4. **Secrets and logging**: Secrets Manager injection, JSON logs, CloudWatch.
5. **Desktop release**: signing, notarization, updater, Settings for the
   hosted engine URL and API key.
6. **Retire**: Cloud Run script, Firestore, the tunnel, the dead
   `desktop/` shell.

## 5. Status

Done today: Claude on Bedrock (`agents/claude.py` `bedrock` backend, chosen
only by `SILKSCREEN_CLAUDE_BACKEND=bedrock`, wire id `global.anthropic.<id>`,
22 offline tests; one live call reached Bedrock and was refused with "your
account is currently being verified", the new-account hold), `.env` slots for
every key with nothing hardcoded, AWS CLI installed and authenticated (as
root, to be replaced by an IAM user before anything is deployed).

Done 2026-09-16:

- **Structured logs** (`service/logs.py`): one JSON line per event when
  `SILKSCREEN_LOG_FORMAT=json` (the Dockerfile sets it), AWS Powertools'
  key names (`level`, `message`, `timestamp`, `service`) plus `request_id`,
  `run_id`, `account_id`, `method`, `path`, `status`, `duration_ms`. Every
  response carries `X-Request-Id` (an inbound one is kept when sane, an
  ALB `X-Amzn-Trace-Id` root next). The 500 path logs the traceback as a
  field. Query strings and token-shaped path segments are never logged.
  Text mode is unchanged on a laptop. Tests: `service/tests/test_logs.py`.
- **Per-account API keys** (`service/auth.py`): `ada_<prefix>.<secret>`,
  SHA-512 at rest, prefix lookup, constant-time compare, revocation as a
  timestamp; `SqliteKeyStore` behind a five-method `KeyStore` seam so a
  Postgres store drops in. `SILKSCREEN_API_KEYS_DB` turns it on; the shared
  `SILKSCREEN_ACCESS_TOKEN` keeps working beside it. A valid key binds
  `account_id` onto every log line of its request. CLI:
  `python -m service.auth create|list|revoke`. Tests:
  `service/tests/test_api_keys.py`.

Not started: `infra/` (CDK), Postgres, Secrets Manager injection, the
account id reaching `service/metering.py`, the desktop's Settings field for a
key, signing and the updater. The website is written (`site/`) and has a
GitHub Pages workflow (`.github/workflows/site.yml`) that needs Pages enabled
once; it has never been deployed.
