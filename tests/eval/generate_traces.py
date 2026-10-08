# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Evaluation trace generator for the ambient expense approval agent.

Executes test scenarios through the local ADK 2.0 Workflow runner, intercepts
Human-in-the-Loop (HITL) checkpoints to automate decisions (approvals, rejections,
or custom test decisions like 'DISAPPROVE'), and outputs populated traces to
artifacts/traces/generated_traces.json.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Load local environment (.env)
project_root = Path(__file__).resolve().parent.parent.parent
load_dotenv(project_root / ".env")

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from expense_agent.agent import root_agent

DATASET_PATH = project_root / "tests" / "eval" / "datasets" / "basic-dataset.json"
OUTPUT_PATH = project_root / "artifacts" / "traces" / "generated_traces.json"


async def run_scenario(runner: Runner, case: dict[str, Any], attempt: int = 1) -> dict[str, Any]:
    """Run a single evaluation case through the ADK workflow runner."""
    case_id = case["eval_case_id"]
    prompt_obj = case["prompt"]
    prompt_text = prompt_obj["parts"][0]["text"]
    automated_decision = case.get("automated_human_decision")

    user_id = "eval_user"
    session_id = f"session-eval-{case_id}-{attempt}"

    # Ensure a fresh session
    await runner.session_service.create_session(
        app_name="app",
        user_id=user_id,
        session_id=session_id,
    )

    turns: list[dict[str, Any]] = []
    turn_0_events: list[dict[str, Any]] = []

    # Seed initial user event
    turn_0_events.append({
        "author": "user",
        "content": {
            "role": "user",
            "parts": [{"text": prompt_text}],
        },
    })

    initial_msg = types.Content(
        role="user",
        parts=[types.Part.from_text(text=prompt_text)],
    )

    paused_for_hitl = False
    last_invocation_id = None
    final_outcome: dict[str, Any] | None = None
    security_flagged_in_step = False

    # Execute first pass
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=initial_msg,
    ):
        if getattr(event, "invocation_id", None):
            last_invocation_id = event.invocation_id

        # Track event text content if available
        if event.content and event.content.parts:
            text = "".join(part.text or "" for part in event.content.parts).strip()
            if text:
                turn_0_events.append({
                    "author": event.author or "ambient_expense_agent",
                    "content": {
                        "role": event.content.role or "model",
                        "parts": [{"text": text}],
                    },
                })
                if "SECURITY CHECKPOINT ALERT" in text or "PROMPT_INJECTION_DETECTED" in text:
                    security_flagged_in_step = True
                if "Workflow paused. Please review and reply with your decision" in text:
                    paused_for_hitl = True

        if (
            getattr(event, "output", None)
            and isinstance(event.output, dict)
            and "decision_type" in event.output
        ):
            final_outcome = event.output

    turns.append({
        "turn_index": 0,
        "turn_id": "turn_0",
        "events": turn_0_events,
    })

    # If paused for human review, automate human decision
    if paused_for_hitl and (final_outcome is None or "decision_type" not in final_outcome):
        if automated_decision is not None:
            decision_to_send = automated_decision
        elif security_flagged_in_step:
            decision_to_send = "REJECT"
        else:
            decision_to_send = "APPROVE"

        turn_1_events: list[dict[str, Any]] = []
        turn_1_events.append({
            "author": "user",
            "content": {
                "role": "user",
                "parts": [{"text": f"Human Decision: {decision_to_send}"}],
            },
        })

        resume_part = types.Part(
            function_response=types.FunctionResponse(
                name="human_approval",
                id="human_approval",
                response={"result": decision_to_send},
            )
        )
        resume_msg = types.Content(role="user", parts=[resume_part])

        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=resume_msg,
            invocation_id=last_invocation_id,
        ):
            if event.content and event.content.parts:
                text = "".join(part.text or "" for part in event.content.parts).strip()
                if text:
                    turn_1_events.append({
                        "author": event.author or "ambient_expense_agent",
                        "content": {
                            "role": event.content.role or "model",
                            "parts": [{"text": text}],
                        },
                    })
            if getattr(event, "output", None) and isinstance(event.output, dict):
                final_outcome = event.output

        turns.append({
            "turn_index": 1,
            "turn_id": "turn_1",
            "events": turn_1_events,
        })

    # Prepare candidate final response representation
    response_text = json.dumps(final_outcome or {}, indent=2)

    return {
        "eval_case_id": case_id,
        "prompt": prompt_obj,
        "responses": [
            {
                "response": {
                    "role": "model",
                    "parts": [{"text": response_text}],
                }
            }
        ],
        "agent_data": {
            "turns": turns,
        },
        "metadata": {
            "expected_routing": case.get("expected_routing"),
            "expected_outcome": case.get("expected_outcome"),
            "automated_human_decision": automated_decision,
            "final_outcome": final_outcome,
        },
    }


async def main():
    print(f"Loading evaluation dataset from {DATASET_PATH}...")
    if not DATASET_PATH.exists():
        print(f"Error: Dataset not found at {DATASET_PATH}", file=sys.stderr)
        sys.exit(1)

    with open(DATASET_PATH, encoding="utf-8") as f:
        data = json.load(f)

    eval_cases_input = data.get("eval_cases", [])
    print(f"Found {len(eval_cases_input)} evaluation scenarios to execute.")

    # Initialize runner with fresh in-memory session service
    session_service = InMemorySessionService()
    runner = Runner(agent=root_agent, session_service=session_service, app_name="app")

    populated_cases = []
    for idx, case in enumerate(eval_cases_input, start=1):
        case_id = case["eval_case_id"]
        print(f"\n[{idx}/{len(eval_cases_input)}] Generating trace for '{case_id}'...")

        max_attempts = 5
        res_case = None
        for attempt in range(1, max_attempts + 1):
            try:
                res_case = await run_scenario(runner, case, attempt=attempt)
                break
            except Exception as e:
                if attempt == max_attempts:
                    print(f"    ❌ Failed after {max_attempts} attempts: {e}", file=sys.stderr)
                    raise
                wait_secs = attempt * 6
                print(f"    ⚠️ Encountered rate limit or transient error ({e}). Waiting {wait_secs}s before retry {attempt+1}/{max_attempts}...")
                await asyncio.sleep(wait_secs)

        assert res_case is not None
        outcome = res_case["metadata"]["final_outcome"] or {}
        print(f"    Status: {outcome.get('status')} | Decision: {outcome.get('decision_type')} | By: {outcome.get('decision_by')}")
        if outcome.get("security_flagged"):
            print("    🛡️ Security Flagged: True (Prompt Injection Intercepted)")
        if outcome.get("redacted_categories"):
            print(f"    🔒 Redacted Sensitive Categories: {outcome.get('redacted_categories')}")
        populated_cases.append(res_case)

        # Brief delay to respect Vertex AI quotas
        if idx < len(eval_cases_input):
            await asyncio.sleep(3)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    traces_payload = {"eval_cases": populated_cases}

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(traces_payload, f, indent=2)

    print(f"\n✅ All {len(populated_cases)} traces successfully saved to {OUTPUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
