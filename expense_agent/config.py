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

"""Configuration for the ambient expense approval workflow."""

import os
from dotenv import load_dotenv

load_dotenv()

# Dollar threshold below which expenses are automatically approved without LLM review.
AUTO_APPROVE_THRESHOLD: float = float(
    os.getenv("EXPENSE_AUTO_APPROVE_THRESHOLD", "100.0")
)

# Gemini model used exclusively for risk evaluation on expenses >= AUTO_APPROVE_THRESHOLD.
MODEL_NAME: str = os.getenv("EXPENSE_MODEL_NAME", "gemini-3.1-flash-lite")
