import collections
import json
import random
import time
import google.auth
import google.auth.transport.requests

PROJECT = "jzhuo08-demo"
BASE = ("https://us-east1-aiplatform.googleapis.com/v1beta1/projects/444323067071/"
        "locations/us-east1/reasoningEngines/7197127138003124224")
random.seed(7)
ITEMS = [("meals", "Team lunch"), ("meals", "Client coffee meeting"),
         ("travel", "Taxi to client site"), ("travel", "Train ticket to Boston"),
         ("software", "Monthly SaaS subscription"), ("office", "Printer paper and toner"),
         ("training", "Online course fee"), ("equipment", "External monitor"),
         ("equipment", "Standing desk"), ("travel", "Conference flight")]
AMOUNTS = [12.5, 35, 48.9, 75, 99.99, 120, 150, 250, 480, 900, 1500]
N = 30

def main():
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    http = google.auth.transport.requests.AuthorizedSession(creds)
    hdr = {"X-Goog-User-Project": PROJECT}
    counts = collections.Counter()
    for i in range(N):
        user = f"online_user_{i}"
        s = http.post(f"{BASE}:query", headers=hdr,
                      json={"class_method": "create_session", "input": {"user_id": user}})
        sid = s.json().get("output", {}).get("id")
        cat, desc = random.choice(ITEMS)
        payload = {"amount": random.choice(AMOUNTS), "submitter": "traffic@company.com",
                   "category": cat, "description": desc, "date": "2026-10-11"}
        body = {"class_method": "stream_query",
                "input": {"user_id": user, "session_id": sid, "message": json.dumps(payload)}}
        r = http.post(f"{BASE}:streamQuery", headers=hdr, json=body)
        counts[r.status_code] += 1
        print(f"{i+1:02d}/{N} ${payload['amount']:<8} {desc:<28} HTTP {r.status_code}")
        time.sleep(1)
    print("status counts:", dict(counts))

if __name__ == "__main__":
    main()
