"""End-to-end demo against a RUNNING API server.

    uvicorn app.main:app                  # terminal 1
    python scripts/demo_flow.py           # terminal 2

Flow: upload v1 -> select 3.2 + 4.2 from v1 -> upload v2 -> show that the selection still
resolves to v1 text, that both sections changed, and exactly what changed.
Each run uses its own document name, so it can be repeated without resetting the database.
"""
import argparse
import os
import sys
import time

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
V1 = os.path.join(ROOT, "data", "ct200_manual.pdf")
V2 = os.path.join(ROOT, "data", "ct200_manual_v2.pdf")


def step(title: str) -> None:
    print(f"\n=== {title}")


def check(resp: httpx.Response, expected: int) -> dict:
    if resp.status_code != expected:
        print(f"  !! expected HTTP {expected}, got {resp.status_code}: {resp.text[:300]}")
        sys.exit(1)
    return resp.json()


def upload(c: httpx.Client, path: str, doc: str) -> dict:
    with open(path, "rb") as f:
        return check(c.post("/documents/ingest", files={"file": (os.path.basename(path), f, "application/pdf")},
                            params={"document": doc}), 201)


def find(c: httpx.Client, number: str, version: int, doc: str) -> int:
    hits = check(c.get("/search", params={"q": number, "field": "title", "version": version, "document": doc}), 200)
    return next(h["node"]["id"] for h in hits["hits"] if h["node"]["number"] == number)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = ap.parse_args()
    doc = f"CT-200 demo {time.strftime('%Y%m%d-%H%M%S')}"

    with httpx.Client(base_url=args.base_url, timeout=60) as c:
        try:
            c.get("/health")
        except httpx.ConnectError:
            sys.exit(f"API not reachable at {args.base_url}. Start it with: uvicorn app.main:app")
        print(f"document name for this run: {doc!r}")

        step("1. Upload v1")
        v1 = upload(c, V1, doc)["version"]
        print(f"  version {v1['version']}: {v1['node_count']} nodes, warnings: {len(v1['parse_warnings'])}")
        for w in v1["parse_warnings"]:
            print(f"    - {w}")

        step("2. Select 3.2 (cuff inflation) and 4.2 (error codes) from v1")
        ids = [find(c, "3.2", 1, doc), find(c, "4.2", 1, doc)]
        sel = check(c.post("/selections", json={"name": f"{doc} / inflation+errors", "node_ids": ids}), 201)
        print(f"  selection {sel['id']} pinned to v{sel['pinned_version']}, nodes {ids}")
        print(f"  fingerprint {sel['fingerprint'][:16]}…")

        step("3. Upload v2 of the manual")
        v2 = upload(c, V2, doc)["version"]
        s = v2["diff_summary"]
        print(f"  version {v2['version']} vs {s['previous_version']}: {s['counts']}")
        print(f"  modified: {s['modified']}   added: {s['added']}")

        step("4. Re-read the selection: still pinned to v1 text")
        again = check(c.get(f"/selections/{sel['id']}"), 200)
        assert again["fingerprint"] == sel["fingerprint"], "pinned text changed!"
        for item in again["items"]:
            print(f"  {item['number']:5} {item['title']:28} status in latest: {item['status_in_latest']}")
        print("  pinned text still says '40 mmHg increments':", "40 mmHg increments" in again["text"])

        step("5. What changed in each pinned section (diff to latest)")
        for node in ids:
            r = check(c.get(f"/nodes/{node}/changes"), 200)
            d = r["diff_to_latest"]
            print(f"  node {node} (v{r['version']}) -> latest node {r['latest_node_id']} (v{r['latest_version']}): "
                  f"{r['status_vs_latest']}, similarity {d['similarity']}")
            print(f"    numbers removed {d['numbers_removed']}, added {d['numbers_added']}")
            for ch in d["changes"]:
                print(f"    {ch['op']:8} '{ch['old'][:50]}' -> '{ch['new'][:70]}'")

        step("6. Re-uploading v2 is a no-op")
        with open(V2, "rb") as f:
            r = c.post("/documents/ingest", files={"file": ("ct200_manual_v2.pdf", f, "application/pdf")},
                       params={"document": doc})
        print(f"  HTTP {r.status_code}, created={r.json()['created']}")

    print("\nDone. Browse interactively at", args.base_url + "/docs")


if __name__ == "__main__":
    main()
