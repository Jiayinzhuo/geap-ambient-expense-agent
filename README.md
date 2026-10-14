# GEAP Ambient Expense Agent

An event-driven expense-approval agent built on **ADK 2.0 graph workflows**, vibe-coded with
**Antigravity** and **Agents CLI**, and deployed to **Agent Runtime** on the Gemini Enterprise
Agent Platform. Business rules run as plain Python; the LLM is used only where judgment is needed.

| Expense | What happens |
|---|---|
| Under $100 | Auto-approved by code. No LLM call. |
| $100 or more | Security checkpoint, then Gemini risk review, then **pause for human approval** |
| Prompt injection | Skips the model entirely and goes straight to a human, flagged as a security event |

Part of a series on the Gemini Enterprise Agent Platform: Build, Scale, Govern, Optimize.

| Stage | What is in this repo | Guide |
|---|---|---|
| Build | ADK 2.0 graph, fail-closed human approval, PII scrub, unit tests | This README |
| Scale | Deployed to Agent Runtime; resume a paused session from a script | This README |
| Govern | Agent Identity, ingress Agent Gateway with a Model Armor template, `failOpen: false`, tested with requests that must be blocked | [docs/governance-setup.md](docs/governance-setup.md) |
| Optimize | Pre-deploy eval gate with a deterministic PII check; online monitor scoring live traces; one prompt change measured before and after | [docs/observability-and-optimize.md](docs/observability-and-optimize.md) |

## Credit

Based on Google's codelab
[Vibecode an ADK 2.0 Ambient Agent with Antigravity and Agents CLI](https://codelabs.developers.google.com/vibecode-ambient-expense-agent)
(code samples Apache 2.0). The deployment follows
[Deploy an ADK agent to Agent Runtime using Agents CLI](https://codelabs.developers.google.com/enterprise-cloud-scale-deploying-the-expense-agent-to-agent-runtime-on-google-cloud).

## Architecture

```
event (Pub/Sub or JSON)
   -> extract_expense        parse payload, redact PII, route on threshold (code)
        |-- < $100  -> auto_approve (code)  ------------------------.
        '-- >= $100 -> security_checkpoint (code: detect injection)  |
                          |-- clean     -> risk_reviewer (Gemini)    |
                          '-- injection -> human_approval (model bypassed)
                       risk_reviewer -> human_approval (RequestInput pause)
                                                |                    |
                                          record_outcome <-----------'
```

## Project layout

| Path | Purpose |
|---|---|
| `expense_agent/` | The agent: `agent.py` (graph), `config.py` (threshold, model), `schemas.py` |
| `app/` | Agents CLI wrapper that serves `expense_agent` (FastAPI, deployment adapters) |
| `deployment/`, `Dockerfile` | Agents CLI deployment scaffold |
| `scripts/test_deployed_engine.py` | Verifies a deployed engine, including resuming a paused session |
| `scripts/test_gateway_block.py` | Sends benign, jailbreak and SSN/card requests through the gateway; expects 200, 403, 403 |
| `scripts/generate_online_traffic.py` | Sends repeatable traffic for the online monitor (`--n`, `--review-only`) |
| `scripts/governance/` | Model Armor template script, gateway and fail-closed extension definitions |
| `docs/` | Setup guides for Govern and Optimize, and the eval-gate reports |
| `tests/unit`, `tests/integration` | Routing, redaction, and decision-parsing tests |
| `tests/eval/` | LLM-as-judge evals: routing correctness and security containment |
| `artifacts/` | Eval traces, grade results, gate demo, governance snapshots and test output |

## What I changed beyond the lab

- The human decision parser **fails closed**: only an exact APPROVE, YES, or ACCEPT approves.
  DISAPPROVE or ambiguous input rejects.
- PII redaction also covers workflow state, so the human-approval alert and payload never
  contain the raw SSN or card number.
- The eval set includes boundary cases ($99.99 and exactly $100.00) and a rejected-by-human path.
- The container image copies `expense_agent/` as well as `app/`. The scaffold's Dockerfile
  copied only `app/`, which would have failed on import at startup.

## Run it locally

```bash
cp .env.example .env        # set GOOGLE_CLOUD_PROJECT, or use an AI Studio key
agents-cli install
make playground             # ADK Playground / ambient server on port 8080
```

Trigger the ambient endpoint by simulating a Pub/Sub push:

```bash
curl -s http://localhost:8080/apps/expense_agent/trigger/pubsub \
  -H "Content-Type: application/json" \
  -d "{\"message\":{\"data\":\"$(printf '%s' '{"amount":45,"submitter":"bob@company.com","category":"meals","description":"Team lunch","date":"2026-04-12"}' | base64)\"},\"subscription\":\"test-sub\"}"
```

The app name in the URL must match your app. Check the Dev UI dropdown if you get a 404.

## Evaluate

```bash
make generate-traces && make grade
```

Two LLM-as-judge metrics score each trace from 1 to 5: routing correctness and security
containment. Results land in `artifacts/grade_results/`.

## Deploy to Agent Runtime

```bash
gcloud config set project <your-project-id>
agents-cli deploy --dry-run
agents-cli deploy --project <your-project-id> --region us-east1 --no-wait
agents-cli deploy --status
```

Verified on the live engine:

- $50 auto-approves with no model call.
- $150 pauses for a human after a Gemini risk review.
- A $1M injection with an SSN is redacted, skips the model, and escalates to a human.
- Resuming with APPROVE approves; resuming with DISAPPROVE rejects (fail-closed).

The Console Playground (preview) shows the pause but does not resume it; sending "APPROVE"
there starts a new run and fails to parse. Resume by sending a `function_response` to the
engine, as `scripts/test_deployed_engine.py` does.

## Evaluation gate

`make predeploy` runs the unit tests, regenerates traces, grades them, and
fails if any metric is below its threshold (`tests/eval/eval_config.yaml`).
It includes a deterministic metric, `raw_pii_absent`, that fails if any run of
9+ digits appears in agent events or state.

Example: on the pre-fix build the gate fails (`raw_pii_absent` 0.8333) while
the LLM judges still pass on average. On the fixed build it passes. See
`artifacts/gate_demo/`.

## Govern: gateway, Model Armor, Agent Identity

The engine runs as `AGENT_IDENTITY` and is bound to a Client-to-Agent ingress Agent Gateway
that calls a Model Armor template (prompt-injection and jailbreak filter, basic Sensitive Data
Protection). The gateway's authorization extension is set to `failOpen: false`; the Console
default is `true`, which lets traffic through unscreened if Model Armor cannot be called.

Verified through the gateway (`scripts/test_gateway_block.py`), and confirmed in Model Armor's
own log (client `AGENT_GATEWAY`, ALLOW then BLOCK):

| Request | Result |
|---|---|
| Benign ("Team lunch") | HTTP 200 |
| Jailbreak text | HTTP 403 |
| Valid-format SSN and card number | HTTP 403 |

Full setup, IAM grants and gotchas: [docs/governance-setup.md](docs/governance-setup.md).

## Optimize: online monitor and a measured change

An online monitor scores live traces with four judges (response quality, tool use,
groundedness, safety). It needed span export, `enable_tracing=True`, content capture with the
upload hook off, and a tool on the LLM agent before it could score anything.

One prompt change (the risk reviewer, which already called the policy tool, must now quote only facts), measured
on the same 12 requests by the same monitor:

| Metric (higher is better) | Before (rev 17) | After (rev 18) |
|---|---|---|
| Groundedness, average | 0.70 | 0.96 |
| Groundedness, worst request | 0.00 | 0.67 |
| Final response quality, average | 0.92 | 0.97 |
| Tool use, safety | 1.0 | 1.0 |
| Verdicts (reject : approve) | 5 : 7 | 5 : 7 |

12 requests, one run each, LLM-judged: evidence, not proof. Same model on both revisions.
Final response quality is graded against rubrics generated from each revision's own prompt, so
groundedness is the like-for-like number.
Details and how to rerun: [docs/observability-and-optimize.md](docs/observability-and-optimize.md).

## Security note and open gaps

- The in-graph security checkpoint is a **local, regex-based mock** used for routing. The real
  enforcement is the gateway and Model Armor above.
- **Not governed:** the Pub/Sub trigger path (runs in-process) and methods other than `query`
  and `streamQuery`.
- **SDK path:** `scripts/test_deployed_engine.py` (Vertex SDK) passed a jailbreak that REST
  blocked with a 403. Cause not verified; treat that path as unscreened.
- **Response path:** one Model Armor response-path span reported "Fail-Open Configured" while
  the extension is `failOpen: false`. Cause unknown.
- **Content capture is on** so the monitor can score. Traces contain prompts and responses.
- Not tested: rollback of the identity and gateway binding, organization-level features
  (this project has no organization), semantic governance, anomaly detection.

## Status

| Tag | Stage |
|---|---|
| `v1-local` | Runs locally, evaluated with Agents CLI |
| `v2-deployed` | Deployed to Agent Runtime and verified |
| `v2.1-deployed` | Redaction fix redeployed; raw PII check passes on the live engine |
| `v3-eval-gate` | `make predeploy` gate with the deterministic `raw_pii_absent` check |
| `v4-governed` | Agent Identity, ingress Agent Gateway and Model Armor; block tests pass |
| `v5-observable` | Online monitor scores live traces |
| `v6-optimized` | Grounded risk summaries, measured by the online monitor |

Compare any two stages with `git diff <tag>..<tag>`.

## License

Apache 2.0. See `LICENSE`.
