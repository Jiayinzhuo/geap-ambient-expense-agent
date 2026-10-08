# GEAP Ambient Expense Agent

An event-driven expense-approval agent built on **ADK 2.0 graph workflows**, vibe-coded with
**Antigravity** and **Agents CLI**. Business rules run as plain Python; the LLM is used only
where judgment is needed.

| Expense | What happens |
|---|---|
| Under $100 | Auto-approved by code. No LLM call. |
| $100 or more | Pre-LLM security screen, then Gemini risk review, then **pause for human approval** |
| Prompt injection | Skips the model entirely and goes straight to a human, flagged as a security event |

Part of a series on the **Gemini Enterprise Agent Platform**: Build, Scale, Govern, Optimize.

## Credit

Based on Google's codelab
[Vibecode an ADK 2.0 Ambient Agent with Antigravity and Agents CLI](https://codelabs.developers.google.com/vibecode-ambient-expense-agent)
(code samples Apache 2.0). The deployment step follows
[Deploy an ADK agent to Agent Runtime using Agents CLI](https://codelabs.developers.google.com/enterprise-cloud-scale-deploying-the-expense-agent-to-agent-runtime-on-google-cloud).

## Architecture

```
event (Pub/Sub or JSON)
   -> extract_expense        parse payload, route on threshold (code)
        |-- < $100  -> auto_approve (code)  ------------------------.
        '-- >= $100 -> security_screen (code: redact PII, detect injection)
                          |-- clean     -> risk_reviewer (Gemini)
                          '-- injection -> human_approval (model bypassed)
                       risk_reviewer -> human_approval (RequestInput pause)
                                                |
                                          record_outcome <-----------'
```

## Project layout

| Path | Purpose |
|---|---|
| `expense_agent/` | The agent: `agent.py` (graph), `config.py` (threshold, model), `schemas.py` |
| `app/` | Agents CLI wrapper that serves `expense_agent` (FastAPI, deployment adapters) |
| `deployment/`, `Dockerfile` | Agents CLI deployment scaffold |
| `tests/unit`, `tests/integration` | Routing, redaction, and decision-parsing tests |
| `tests/eval/` | LLM-as-judge evals: routing correctness and security containment |
| `artifacts/` | Example eval traces and grade results |

## What I changed beyond the lab

- The human decision parser **fails closed**: only an exact APPROVE, YES, or ACCEPT approves.
  DISAPPROVE or ambiguous input rejects.
- PII redaction also covers workflow state, so the human-approval alert and payload never
  contain the raw SSN or card number.
- The eval set includes boundary cases ($99.99 and exactly $100.00) and a rejected-by-human path.

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

## Security note

The security screen is a **local, regex-based mock**. It catches the demo cases but is not a
production control. In production, enforce policy outside the model with Agent Gateway,
Model Armor, Agent Identity, and semantic governance policies.

## Status

- `v1-local`: runs locally, evaluated with Agents CLI
- `v2-deployed`: Agent Runtime deployment (in progress)

## License

Apache 2.0. See `LICENSE`.
