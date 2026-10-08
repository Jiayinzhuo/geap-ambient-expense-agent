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

"""Unit tests for the ambient FastAPI web service handling Pub/Sub triggers."""

import base64
import json
import pytest
from fastapi.testclient import TestClient

from app.fast_api_app import app, normalize_subscription


def test_normalize_subscription():
    """Verify fully-qualified subscription paths are normalized to short names."""
    assert (
        normalize_subscription("projects/my-gcp-project/subscriptions/my-expense-sub")
        == "my-expense-sub"
    )
    assert (
        normalize_subscription("projects/test-proj/subscriptions/regional-expenses-sub/")
        == "regional-expenses-sub"
    )
    assert normalize_subscription("short-sub") == "short-sub"
    assert normalize_subscription(None) == "pubsub-subscriber"
    assert normalize_subscription("") == "pubsub-subscriber"


def test_ambient_pubsub_auto_approve():
    """Test ambient Pub/Sub push trigger for under-$100 expense."""
    inner = {
        "amount": 75.0,
        "submitter": "alice@company.com",
        "category": "Office",
        "description": "Ergonomic mousepad",
        "date": "2026-10-08",
    }
    b64_data = base64.b64encode(json.dumps(inner).encode()).decode()
    pubsub_payload = {
        "message": {
            "data": b64_data,
            "messageId": "msg-auto-001",
        },
        "subscription": "projects/my-project/subscriptions/expense-approval-sub",
    }

    with TestClient(app) as client:
        response = client.post("/", json=pubsub_payload)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["subscription"] == "expense-approval-sub"
        assert data["messageId"] == "msg-auto-001"
        assert data["paused_for_approval"] is False
        assert data["outcome"]["status"] == "APPROVED"
        assert data["outcome"]["decision_type"] == "AUTO_APPROVED"


def test_ambient_pubsub_review_pause():
    """Test ambient Pub/Sub push trigger for >=$100 expense that pauses for human approval."""
    inner = {
        "amount": 250.0,
        "submitter": "bob@company.com",
        "category": "Travel",
        "description": "Conference admission ticket",
        "date": "2026-10-08",
    }
    b64_data = base64.b64encode(json.dumps(inner).encode()).decode()
    pubsub_payload = {
        "message": {
            "data": b64_data,
            "messageId": "msg-review-002",
        },
        "subscription": "projects/acme-corp/subscriptions/corporate-expenses-sub",
    }

    with TestClient(app) as client:
        response = client.post("/pubsub", json=pubsub_payload)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["subscription"] == "corporate-expenses-sub"
        assert data["paused_for_approval"] is True
