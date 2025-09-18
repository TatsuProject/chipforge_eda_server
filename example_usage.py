"""
example_usage.py

Run a local end-to-end tools check against the Gateway.
Looks for two zip files in ./test:
  - <design>.zip
  - <something>evaluator.zip
Sends them to POST /evaluate and prints a PASS/FAIL summary and raw JSON.
"""

import os
import sys
import glob
import json
import requests

API_URL = os.getenv("EDA_BASE_URL", "http://localhost:8080")
API_KEY = os.getenv("EDA_API_KEY")  # optional
TEST_DIR = os.getenv("EDA_TEST_DIR", "test")


def pick_test_files(test_dir: str):
    zips = sorted(glob.glob(os.path.join(test_dir, "*.zip")))
    if not zips:
        raise FileNotFoundError(f"No .zip files found in {test_dir}")

    # Prefer an evaluator zip explicitly
    eval_candidates = [z for z in zips if "eval" in os.path.basename(z).lower()]
    if not eval_candidates:
        # fallback: any name containing 'evaluator'
        eval_candidates = [z for z in zips if "evaluator" in os.path.basename(z).lower()]
    if not eval_candidates:
        raise FileNotFoundError(
            f"No evaluator zip found in {test_dir}. Expected a file like '*evaluator*.zip'."
        )

    evaluator_zip = eval_candidates[0]

    # Design zip = any other zip that is not the chosen evaluator
    design_candidates = [z for z in zips if z != evaluator_zip]
    if not design_candidates:
        raise FileNotFoundError(
            f"No design zip found in {test_dir} besides evaluator '{os.path.basename(evaluator_zip)}'."
        )

    # Prefer a file literally named adder.zip if present; else the first remaining
    preferred = [z for z in design_candidates if os.path.basename(z) == "adder.zip"]
    design_zip = preferred[0] if preferred else design_candidates[0]

    return design_zip, evaluator_zip


def evaluate(api_url: str, design_zip: str, evaluator_zip: str, api_key: str | None):
    url = f"{api_url.rstrip('/')}/evaluate"
    headers = {}
    if api_key:  # only send header if provided
        headers["Authorization"] = f"Bearer {api_key}"

    with open(design_zip, "rb") as df, open(evaluator_zip, "rb") as ef:
        files = {
            "design_zip": (os.path.basename(design_zip), df, "application/zip"),
            "evaluator_zip": (os.path.basename(evaluator_zip), ef, "application/zip"),
        }
        print(f"→ POST {url}")
        print(f"   design_zip   = {design_zip}")
        print(f"   evaluator_zip= {evaluator_zip}")
        resp = requests.post(url, files=files, headers=headers, timeout=600)

    return resp


def summarize_result(resp_json: dict) -> bool:
    """Return True if tools test is considered PASS, else False."""
    if not resp_json.get("success"):
        return False

    v = resp_json.get("verilator_results", {})
    o = resp_json.get("openlane_results", {})

    v_ok = bool(v.get("success"))
    # OpenLane may be skipped (weights.performance/area == 0) or succeed
    o_ok = bool(o.get("success") or o.get("skipped") is True)

    return v_ok and o_ok


if __name__ == "__main__":
    print("=== ChipForge EDA Tools – Terminal Runner ===")
    try:
        design_zip, evaluator_zip = pick_test_files(TEST_DIR)
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    try:
        resp = evaluate(API_URL, design_zip, evaluator_zip, API_KEY)
    except Exception as e:
        print(f"[ERROR] Request failed: {e}")
        sys.exit(2)

    print(f"HTTP {resp.status_code}")
    try:
        data = resp.json()
    except Exception:
        print(resp.text)
        print("TOOLS TEST: FAIL (invalid JSON)")
        sys.exit(3)

    # Pretty print JSON
    print("\n--- Raw Result JSON ---")
    print(json.dumps(data, indent=2))

    passed = summarize_result(data)
    print("\n--- Tools Test Summary ---")
    print("TOOLS TEST:", "PASS ✅" if passed else "FAIL ❌")

    # Always exit 0 so miners can still see results even if logic failed
    sys.exit(0)
