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

"""Ambient web service accepting Pub/Sub push trigger messages for expense approval."""

import contextlib
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

from a2a.server.tasks import InMemoryTaskStore
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.runners import Runner
from google.genai import types

from app.app_utils import services
from app.app_utils.a2a import attach_a2a_routes
from app.app_utils.reasoning_engine_adapter import (
    attach_reasoning_engine_routes,
)

load_dotenv()

# Standard Python console logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ambient_expense_agent")

allow_origins = (
    os.getenv("ALLOW_ORIGINS", "").split(",") if os.getenv("ALLOW_ORIGINS") else None
)

# Developer checklist: Set otel_to_cloud=False
otel_to_cloud = False

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def normalize_subscription(subscription: str | None, default: str = "pubsub-subscriber") -> str:
    """Normalize a fully-qualified Pub/Sub subscription path down to a short name.

    Google Cloud Pub/Sub sends: 'projects/{project}/subscriptions/{subscription_name}'.
    Normalized: '{subscription_name}' to keep session records and logs readable.
    """
    if not subscription:
        return default
    clean = subscription.strip().rstrip("/")
    short_name = clean.split("/")[-1]
    return short_name or default


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.agent import app as adk_app
    from app.agent import root_agent

    runner = Runner(
        app=adk_app,
        session_service=services.get_session_service(),
        artifact_service=services.get_artifact_service(),
        auto_create_session=True,
    )
    app.state.runner = runner
    app.state.agent_app_name = adk_app.name

    await attach_a2a_routes(
        app,
        agent=root_agent,
        runner=runner,
        task_store=InMemoryTaskStore(),
        rpc_path=f"/a2a/{adk_app.name}",
    )
    logger.info("Ambient Expense Agent runner initialized and ready for Pub/Sub events.")
    yield


app: FastAPI = get_fast_api_app(
    agents_dir=AGENT_DIR,
    web=True,
    artifact_service_uri=services.ARTIFACT_SERVICE_URI,
    allow_origins=allow_origins,
    session_service_uri=services.SESSION_SERVICE_URI,
    otel_to_cloud=otel_to_cloud,
    lifespan=lifespan,
)
app.title = "ambient-expense-agent"
app.description = "Ambient event-driven API accepting Pub/Sub push trigger messages"

attach_reasoning_engine_routes(app)


async def handle_pubsub_event(request: Request) -> dict[str, Any]:
    """Core handler that accepts a Pub/Sub trigger message and drives the workflow."""
    try:
        body = await request.json()
    except Exception as e:
        logger.error("Failed to parse incoming request JSON: %s", e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid JSON payload: {e}",
        ) from e

    # 1. Normalize fully-qualified subscription path down to a readable short name
    raw_sub = body.get("subscription")
    normalized_sub = normalize_subscription(raw_sub)

    # 2. Extract message identifier
    message_obj = body.get("message", body) if isinstance(body, dict) else {}
    message_id = (
        message_obj.get("messageId")
        or message_obj.get("message_id")
        or f"msg-{uuid.uuid4().hex[:8]}"
    )

    logger.info(
        "📥 Received ambient Pub/Sub event: subscription='%s' (normalized: '%s'), messageId='%s'",
        raw_sub,
        normalized_sub,
        message_id,
    )

    runner: Runner = request.app.state.runner
    adk_app_name = getattr(request.app.state, "agent_app_name", "ambient_expense_agent")

    # 3. Create a clean, readable session ID using normalized subscription name
    session_id = f"session-{normalized_sub}-{message_id}"

    try:
        session = await runner.session_service.get_session(
            app_name=adk_app_name, user_id=normalized_sub, session_id=session_id
        )
    except Exception:
        session = None

    if session is None:
        session = await runner.session_service.create_session(
            app_name=adk_app_name, user_id=normalized_sub, session_id=session_id
        )
        logger.info("Created ambient session '%s' for subscriber '%s'", session.id, normalized_sub)

    # 4. Feed the Pub/Sub payload directly into the ADK 2.0 workflow
    raw_payload_text = json.dumps(body)
    new_message = types.Content(
        role="user",
        parts=[types.Part.from_text(text=raw_payload_text)],
    )

    final_outcome: dict[str, Any] | None = None
    paused_for_approval = False
    interrupt_message: str | None = None

    async for event in runner.run_async(
        user_id=normalized_sub,
        session_id=session.id,
        new_message=new_message,
    ):
        if event.content and event.content.parts:
            for p in event.content.parts:
                if p.text:
                    logger.info("⚙️ [Workflow Step]: %s", p.text.strip())
                if p.function_call and p.function_call.name == "adk_request_input":
                    paused_for_approval = True
                    interrupt_message = p.function_call.args.get("message")
                    logger.warning("⏸️ [HITL Pause]: Approval required: %s", interrupt_message)

        if event.output and isinstance(event.output, dict) and "status" in event.output:
            final_outcome = event.output
            logger.info(
                "🏁 [Workflow Decision]: Status=%s, Type=%s, Submitter=%s, Amount=$%.2f",
                final_outcome.get("status"),
                final_outcome.get("decision_type"),
                final_outcome.get("expense", {}).get("submitter"),
                final_outcome.get("expense", {}).get("amount", 0.0),
            )

    # 5. Acknowledge message with HTTP 200 so Pub/Sub does not retry
    return {
        "status": "success",
        "subscription": normalized_sub,
        "messageId": message_id,
        "sessionId": session.id,
        "paused_for_approval": paused_for_approval,
        "outcome": final_outcome,
    }


# Health check endpoint
@app.get("/health", tags=["Ambient"])
@app.get("/", tags=["Ambient"])
async def health_check():
    """Health check endpoint for container orchestrators and monitoring."""
    return {
        "status": "healthy",
        "service": "ambient-expense-agent",
        "mode": "ambient-pubsub-consumer",
    }


# Pub/Sub Push Triggers: mounted at root POST / (Cloud Run default) and /pubsub
@app.post("/", tags=["Ambient Trigger"])
@app.post("/pubsub", tags=["Ambient Trigger"])
@app.post("/apps/{app_name}/trigger/pubsub", tags=["Ambient Trigger"])
async def pubsub_trigger(request: Request):
    """Ambient Pub/Sub push trigger endpoint.

    Consumes messages pushed by Google Cloud Pub/Sub subscriptions, normalizes the
    subscription name, feeds each into the graph workflow, and returns HTTP 200.
    """
    result = await handle_pubsub_event(request)
    return JSONResponse(status_code=status.HTTP_200_OK, content=result)


# Main execution serving on port 8080
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("app.fast_api_app:app", host="0.0.0.0", port=port, reload=False)
