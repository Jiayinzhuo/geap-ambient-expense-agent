"""Send expense requests to the deployed engine (through the ingress gateway) so the
online monitor has traces to score.

Examples:
  uv run python scripts/generate_online_traffic.py                      # 30 mixed requests
  uv run python scripts/generate_online_traffic.py --n 12 --review-only # the 12-request loop
"""
import argparse
import collections
import json
import random
import time
import google.auth
import google.auth.transport.requests

PROJECT = "jzhuo08-demo"
BASE = ("https://us-east1-aiplatform.googleapis.com/v1beta1/projects/444323067071/"
        "locations/us-east1/reasoningEngines/7197127138003124224")
ITEMS = [("meals", "Team lunch"), ("meals", "Client coffee meeting"),
         ("travel", "Taxi to client site"), ("travel", "Train ticket to Boston"),
         ("software", "Monthly SaaS subscription"), ("office", "Printer paper and toner"),
         ("training", "Online course fee"), ("equipment", "External monitor"),
         ("equipment", "Standing desk"), ("travel", "Conference flight")]
AMOUNTS = [12.5, 35, 48.9, 75, 99.99, 120, 150, 250, 480, 900, 1500]
REVIEW_AMOUNTS = [a for a in AMOUNTS if a >= 100]  # only these reach the LLM


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30, help="number of requests")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--review-only", action="store_true",
                    help="only amounts >= $100, so every request calls the model")
    args = ap.parse_args()

    random.seed(7)  # same requests on every run, so revisions can be compared
    amounts = REVIEW_AMOUNTS if args.review_only else AMOUNTS
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    http = google.auth.transport.requests.AuthorizedSession(creds)
    hdr = {"X-Goog-User-Project": PROJECT}
    counts = collections.Counter()
    for i in range(args.n):
        user = f"online_user_{i}"
        s = http.post(f"{BASE}:query", headers=hdr,
                      json={"class_method": "create_session", "input": {"user_id": user}})
        sid = s.json().get("output", {}).get("id")
        cat, desc = random.choice(ITEMS)
        payload = {"amount": random.choice(amounts), "submitter": "traffic@company.com",
                   "category": cat, "description": desc, "date": "2026-10-11"}
        body = {"class_method": "stream_query",
                "input": {"user_id": user, "session_id": sid, "message": json.dumps(payload)}}
        r = http.post(f"{BASE}:streamQuery", headers=hdr, json=body)
        counts[r.status_code] += 1
        print(f"{i+1:02d}/{args.n} ${payload['amount']:<8} {desc:<28} HTTP {r.status_code}")
        time.sleep(args.delay)
    print("status counts:", dict(counts))


if __name__ == "__main__":
    main()
