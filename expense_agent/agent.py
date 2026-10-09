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

"""Ambient expense-approval agent using the ADK 2.0 graph Workflow API."""

import base64
import json
import re
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.workflow import START, Workflow, node
from google.genai import types

from expense_agent.config import AUTO_APPROVE_THRESHOLD, MODEL_NAME
from expense_agent.schemas import ExpenseOutcome, ExpenseReport, RiskAssessment

# Security regex patterns for PII scrubbing
# Matches sequences of digits separated by optional spaces or dashes
PII_DIGIT_SEQUENCE_REGEX = re.compile(r"\b\d+(?:[-\s]+\d+)*\b")


def scrub_pii(text: str) -> tuple[str, list[str]]:
    """Scrub SSNs and credit card numbers from text and track redacted categories.

    - Credit card numbers: 13-19 digits (with optional spaces or dashes) -> [REDACTED_CARD]
    - SSNs: dashed or undashed 9-digit numbers and any run of 9+ digits (with optional spaces or dashes) -> [REDACTED_SSN]
    """
    redacted: list[str] = []

    def _replace_digit_sequence(match: re.Match) -> str:
        val = match.group(0)
        digits_only = re.sub(r"\D", "", val)
        digit_count = len(digits_only)

        # 13-19 digits: Credit card run (with spaces or dashes)
        if 13 <= digit_count <= 19:
            if "CARD" not in redacted:
                redacted.append("CARD")
            if "CREDIT_CARD" not in redacted:
                redacted.append("CREDIT_CARD")
            return "[REDACTED_CARD]"

        # 9+ digits: SSN (dashed, undashed, or run of 9+ consecutive/separated digits)
        if digit_count >= 9:
            if "SSN" not in redacted:
                redacted.append("SSN")
            return "[REDACTED_SSN]"

        return val

    # Match sequences of digits separated by single spaces or dashes
    cleaned = PII_DIGIT_SEQUENCE_REGEX.sub(_replace_digit_sequence, text)
    return cleaned, redacted


# Prompt injection heuristics: instruction overrides, jailbreaks, forced approval
PROMPT_INJECTION_PATTERNS = [
    r"ignore\s+(?:all\s+)?(?:previous|prior|above|other|system)\s+instructions",
    r"disregard\s+(?:all\s+)?(?:previous|prior|rules|guidelines)",
    r"system\s+(?:prompt|override|message|directive)",
    r"developer\s+mode",
    r"jailbreak",
    r"bypass\s+(?:all\s+)?(?:review|rules|approval|security|compliance|threshold)",
    r"(?:auto|force)\s*[-_]?\s*approve",
    r"override\s+(?:approval|rules|policy|threshold|verdict)",
    r"do\s+not\s+(?:flag|review|check|audit|inspect)",
    r"you\s+must\s+approve",
    r"always\s+approve",
    r"mark\s+as\s+approved",
    r"respond\s+only\s+with\s+(?:approve|approved)",
]
INJECTION_REGEX = re.compile("|".join(PROMPT_INJECTION_PATTERNS), re.IGNORECASE)


def parse_payload(raw_input: Any) -> dict[str, Any]:
    """Parse expense event payload from Pub/Sub base64 or plain JSON."""
    parsed: Any = None
    text: str | None = None

    if isinstance(raw_input, types.Content):
        text = "".join(part.text or "" for part in (raw_input.parts or []))
    elif isinstance(raw_input, str):
        text = raw_input.strip()
    elif isinstance(raw_input, dict):
        parsed = raw_input
    else:
        text = str(raw_input)

    if text is not None:
        text = text.strip()
        # Strip markdown json code block delimiters if present
        if text.startswith("```"):
            lines = text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        parsed = json.loads(text)

    # Unwrap Pub/Sub envelope {"message": {"data": "..."}} if present
    if isinstance(parsed, dict) and "message" in parsed and isinstance(parsed["message"], dict):
        parsed = parsed["message"]

    # Decode "data" field (handles base64 Pub/Sub data or nested dict for testing)
    if isinstance(parsed, dict) and "data" in parsed:
        data_val = parsed["data"]
        if isinstance(data_val, dict):
            parsed = data_val
        elif isinstance(data_val, str):
            try:
                decoded = base64.b64decode(data_val).decode("utf-8")
                parsed = json.loads(decoded)
            except Exception:
                # If not base64, attempt direct json parsing of string
                parsed = json.loads(data_val)

    if not isinstance(parsed, dict):
        raise ValueError(f"Unable to parse expense payload into dictionary: {raw_input}")

    return parsed


def extract_expense(ctx: Context, node_input: Any):
    """Step 1: Extract expense details, sanitize PII, and route based on dollar threshold.

    - If amount < AUTO_APPROVE_THRESHOLD ($100): Route to auto_approve (no LLM).
    - If amount >= AUTO_APPROVE_THRESHOLD ($100): Route to security_checkpoint.
    """
    raw_dict = parse_payload(node_input)

    # Ensure PII is scrubbed before any logging or downstream propagation
    raw_desc = raw_dict.get("description", "")
    clean_desc, redacted_categories = scrub_pii(raw_desc)
    raw_dict["description"] = clean_desc

    for k, v in list(raw_dict.items()):
        if isinstance(v, str) and k != "description":
            clean_v, v_redacted = scrub_pii(v)
            raw_dict[k] = clean_v
            for cat in v_redacted:
                if cat not in redacted_categories:
                    redacted_categories.append(cat)

    expense = ExpenseReport(**raw_dict)
    expense_data = expense.model_dump()

    if expense.amount < AUTO_APPROVE_THRESHOLD:
        route = "auto_approve"
        summary_msg = (
            f"📋 Received expense: ${expense.amount:.2f} by {expense.submitter} "
            f"for '{expense.description}' ({expense.category}).\n"
            f"⚡ Amount is under ${AUTO_APPROVE_THRESHOLD:.2f} → Auto-approving immediately without LLM."
        )
    else:
        route = "needs_review"
        summary_msg = (
            f"📋 Received expense: ${expense.amount:.2f} by {expense.submitter} "
            f"for '{expense.description}' ({expense.category}).\n"
            f"🛡️ Amount meets or exceeds ${AUTO_APPROVE_THRESHOLD:.2f} → Routing to security checkpoint."
        )

    # Emit human-readable content for the UI and route to the corresponding branch
    yield Event(
        content=types.Content(role="model", parts=[types.Part.from_text(text=summary_msg)]),
        output=expense_data,
        route=route,
        state={
            "expense": expense_data,
            "redacted_categories": redacted_categories,
            "raw_description_had_injection": bool(INJECTION_REGEX.search(raw_desc)),
        },
    )


def security_checkpoint(ctx: Context, node_input: dict):
    """Step 2: Security checkpoint to scrub PII and defend against prompt injection.

    - Scrubs SSNs and Credit Cards from description so they never reach LLM or logs.
    - Inspects description for prompt injection / instruction override attacks.
    - If prompt injection detected: Bypasses LLM reviewer completely, routes straight to
      human approval, and flags as a security event.
    - If clean: Routes to LLM risk reviewer.
    """
    expense_data = dict(ctx.state.get("expense", node_input))
    desc = expense_data.get("description", "")
    redacted_categories = list(ctx.state.get("redacted_categories", []))

    # Double check PII scrubbing
    clean_desc, new_redactions = scrub_pii(desc)
    expense_data["description"] = clean_desc
    for cat in new_redactions:
        if cat not in redacted_categories:
            redacted_categories.append(cat)

    # Check for prompt injection on both current and initial description
    had_injection_flag = ctx.state.get("raw_description_had_injection", False)
    is_injection = had_injection_flag or bool(INJECTION_REGEX.search(clean_desc))

    if is_injection:
        # Prompt injection detected: bypass model review and escalate to human
        risk = RiskAssessment(
            risk_level="HIGH",
            summary=(
                "SECURITY EVENT: Potential prompt injection / instruction override pattern detected "
                "in expense description. LLM review was blocked to prevent model manipulation."
            ),
            flags=["PROMPT_INJECTION_DETECTED", "SECURITY_ALERT"]
            + [f"REDACTED_{c}" for c in redacted_categories],
            recommendation="REJECT",
        )
        alert_msg = (
            f"🛡️ SECURITY CHECKPOINT ALERT: Prompt injection attempt detected in expense description.\n"
            f"🚫 Model review blocked. Escalating directly to human approval."
        )
        yield Event(
            content=types.Content(role="model", parts=[types.Part.from_text(text=alert_msg)]),
            output=risk.model_dump(),
            route="security_escalation",
            state={
                "expense": expense_data,
                "risk_assessment": risk.model_dump(),
                "redacted_categories": redacted_categories,
                "security_flagged": True,
            },
        )
    else:
        # Clean expense: continue to LLM reviewer
        clean_note = "🛡️ Security checkpoint: Expense description verified clean."
        if redacted_categories:
            clean_note += f" (Redacted PII: {', '.join(redacted_categories)})"
        yield Event(
            content=types.Content(role="model", parts=[types.Part.from_text(text=clean_note)]),
            output=expense_data,
            route="clean",
            state={
                "expense": expense_data,
                "redacted_categories": redacted_categories,
                "security_flagged": False,
            },
        )


def auto_approve(ctx: Context, node_input: dict):
    """Step 2a: Automatically approve expenses under threshold without LLM."""
    expense_dict = ctx.state.get("expense", node_input)
    expense = ExpenseReport(**expense_dict)
    redacted_categories = ctx.state.get("redacted_categories", [])

    outcome = ExpenseOutcome(
        status="APPROVED",
        decision_type="AUTO_APPROVED",
        decision_by="SYSTEM",
        expense=expense,
        risk_assessment=None,
        redacted_categories=redacted_categories,
        security_flagged=False,
        notes=f"Auto-approved: amount ${expense.amount:.2f} is under ${AUTO_APPROVE_THRESHOLD:.2f} threshold.",
    )

    msg = (
        f"✅ AUTO-APPROVED: Expense of ${expense.amount:.2f} submitted by {expense.submitter} "
        f"for '{expense.description}' was instantly approved."
    )
    yield Event(
        content=types.Content(role="model", parts=[types.Part.from_text(text=msg)]),
        output=outcome.model_dump(),
    )


# Step 3a: LLM evaluates risk factors for clean expenses >= $100
risk_reviewer = LlmAgent(
    name="risk_reviewer",
    model=MODEL_NAME,
    instruction=(
        "You are an expert corporate expense auditor and risk compliance officer. "
        "Review the submitted expense report (amount, submitter, category, description, date). "
        "Analyze the report for financial risks, policy violations, reasonableness, and irregularities. "
        "Identify specific risk flags (e.g. unusually high amount, vague description, luxury items, weekend charges). "
        "Assign an overall risk level (LOW, MEDIUM, or HIGH), provide a concise summary of your judgment, "
        "and recommend whether to APPROVE, REJECT, or flag for FURTHER_INSPECTION."
    ),
    output_schema=RiskAssessment,
    output_key="risk_assessment",
)


@node(rerun_on_resume=True)
async def human_approval(ctx: Context, node_input: dict):
    """Step 4: Raise risk/security alert and pause workflow for human decision via RequestInput.

    On first execution: Yields RequestInput interrupt.
    On resume: Reads human decision and constructs final outcome.
    """
    expense_dict = ctx.state.get("expense", {})
    expense = ExpenseReport(**expense_dict)

    # node_input is the structured RiskAssessment (from risk_reviewer or security_checkpoint)
    risk_dict = node_input if isinstance(node_input, dict) else ctx.state.get("risk_assessment", {})
    risk = RiskAssessment(**risk_dict)

    redacted_categories = list(ctx.state.get("redacted_categories", []))
    security_flagged = bool(ctx.state.get("security_flagged", False))

    # Check if we have received the human's response
    if not ctx.resume_inputs or "human_approval" not in ctx.resume_inputs:
        flag_list = "\n  - " + "\n  - ".join(risk.flags) if risk.flags else " None"
        redacted_info = f"\n• Redacted Sensitive Data: {', '.join(redacted_categories)}" if redacted_categories else ""

        if security_flagged:
            header = "🛡️ SECURITY ESCALATION REVIEW ALERT (Prompt Injection Detected)"
            review_details = (
                f"⚠️ The expense description contained instructions attempting to bypass review or force auto-approval.\n"
                f"🚫 LLM review was blocked. Direct human decision required."
            )
        else:
            header = f"🚨 EXPENSE REVIEW ALERT (Amount: ${expense.amount:.2f} >= ${AUTO_APPROVE_THRESHOLD:.2f})"
            review_details = f"📊 LLM Risk Assessment ({MODEL_NAME}):\n• Summary: {risk.summary}"

        alert_msg = (
            f"{header}\n"
            f"• Submitter: {expense.submitter}\n"
            f"• Category: {expense.category}\n"
            f"• Description (Scrubbed): {expense.description}\n"
            f"• Date: {expense.date}{redacted_info}\n\n"
            f"{review_details}\n"
            f"• Risk Level: {risk.risk_level}\n"
            f"• Recommendation: {risk.recommendation}\n"
            f"• Risk Flags:{flag_list}\n\n"
            f"⏸️ Workflow paused. Please review and reply with your decision (e.g. 'APPROVE' or 'REJECT')."
        )

        yield Event(
            content=types.Content(role="model", parts=[types.Part.from_text(text=alert_msg)])
        )
        yield RequestInput(
            interrupt_id="human_approval",
            message=alert_msg,
            payload={
                "expense": expense.model_dump(),
                "risk": risk.model_dump(),
                "redacted_categories": redacted_categories,
                "security_flagged": security_flagged,
            },
        )
        return

    # Resumed: parse human approval input
    user_input = ctx.resume_inputs["human_approval"]
    if isinstance(user_input, dict):
        decision_raw = str(user_input.get("result", user_input.get("decision", user_input)))
    else:
        decision_raw = str(user_input)

    decision_clean = decision_raw.strip()
    decision_upper = decision_clean.upper()
    # Fail-closed approval: strictly require exact match on APPROVE, YES, or ACCEPT
    is_approved = decision_upper in {"APPROVE", "YES", "ACCEPT"}

    final_status = "APPROVED" if is_approved else "REJECTED"

    outcome = ExpenseOutcome(
        status=final_status,
        decision_type="HUMAN_DECISION",
        decision_by="HUMAN_APPROVER",
        expense=expense,
        risk_assessment=risk,
        redacted_categories=redacted_categories,
        security_flagged=security_flagged,
        notes=f"Human approver decision: {decision_clean}",
    )

    status_icon = "✅" if is_approved else "❌"
    result_msg = (
        f"{status_icon} Decision recorded by human approver: Expense {final_status}.\n"
        f"Submitter: {expense.submitter} | Amount: ${expense.amount:.2f} | Notes: {decision_clean}"
    )

    yield Event(
        content=types.Content(role="model", parts=[types.Part.from_text(text=result_msg)]),
        output=outcome.model_dump(),
    )


def record_outcome(ctx: Context, node_input: dict) -> dict:
    """Step 5: Final sink node to audit and record the final outcome."""
    # node_input is the ExpenseOutcome dictionary
    return node_input


# Define the ADK 2.0 graph Workflow edges
edges = [
    # 1. Entry point into expense extraction
    (START, extract_expense),
    # 2. Rule evaluation: Under threshold vs needs review
    (
        extract_expense,
        {
            "auto_approve": auto_approve,
            "needs_review": security_checkpoint,
        },
    ),
    # 3. Security checkpoint: Clean vs prompt injection security escalation
    (
        security_checkpoint,
        {
            "clean": risk_reviewer,
            "security_escalation": human_approval,
        },
    ),
    # 4. LLM risk review leads to human approval
    (risk_reviewer, human_approval),
    # 5. All branches converge to record the outcome
    (auto_approve, record_outcome),
    (human_approval, record_outcome),
]

root_agent = Workflow(
    name="ambient_expense_agent",
    edges=edges,
    description="Ambient expense approval workflow with threshold routing, security checkpoint, LLM risk review, and human-in-the-loop approval.",
)

app = App(
    name="ambient_expense_agent",
    root_agent=root_agent,
)
