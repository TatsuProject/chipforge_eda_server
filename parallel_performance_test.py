"""
parallel_performance_test.py

Run parallel end-to-end tools checks against the Gateway to find optimal concurrency.
Tests 1-8 parallel requests and finds the sweet spot for minimum time per request.
"""

import os
import sys
import glob
import json
import requests
import time
import asyncio
import aiohttp
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime


API_URL = os.getenv("EDA_BASE_URL", "http://localhost:8080")
API_KEY = os.getenv("EDA_API_KEY")  # optional
TEST_DIR = os.getenv("EDA_TEST_DIR", "test")
RESULTS_FILE = "parallel_performance_results.csv"


def pick_test_files(test_dir: str):
    zips = sorted(glob.glob(os.path.join(test_dir, "*.zip")))
    if not zips:
        raise FileNotFoundError(f"No .zip files found in {test_dir}")

    # Prefer an evaluator zip explicitly
    eval_candidates = [z for z in zips if "eval" in os.path.basename(z).lower()]
    if not eval_candidates:
        # fallback: any name containing 'evaluator'
        eval_candidates = [z for z in zips if "evaluator" in os.path.basename(z).lower() or "testcases" in os.path.basename(z).lower()]
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


def evaluate_sync(api_url: str, design_zip: str, evaluator_zip: str, api_key: str | None, request_id: int):
    """Synchronous version for thread pool execution"""
    url = f"{api_url.rstrip('/')}/evaluate"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    start_time = time.time()
    
    with open(design_zip, "rb") as df, open(evaluator_zip, "rb") as ef:
        files = {
            "design_zip": (os.path.basename(design_zip), df, "application/zip"),
            "evaluator_zip": (os.path.basename(evaluator_zip), ef, "application/zip"),
        }
        resp = requests.post(url, files=files, headers=headers, timeout=6000)
    
    end_time = time.time()
    duration = end_time - start_time
    
    return {
        "request_id": request_id,
        "status_code": resp.status_code,
        "duration": duration,
        "success": resp.status_code == 200,
        "response_size": len(resp.content) if resp.content else 0
    }


def run_parallel_requests(num_parallel: int, design_zip: str, evaluator_zip: str):
    """Run multiple requests in parallel using ThreadPoolExecutor"""
    print(f"\n--- Testing {num_parallel} parallel request{'s' if num_parallel > 1 else ''} ---")
    
    start_time = time.time()
    
    with ThreadPoolExecutor(max_workers=num_parallel) as executor:
        futures = []
        for i in range(num_parallel):
            future = executor.submit(
                evaluate_sync, 
                API_URL, 
                design_zip, 
                evaluator_zip, 
                API_KEY, 
                i + 1
            )
            futures.append(future)
        
        # Wait for all requests to complete
        results = [future.result() for future in futures]
    
    end_time = time.time()
    total_time = end_time - start_time
    
    # Calculate statistics
    successful_requests = [r for r in results if r["success"]]
    failed_requests = [r for r in results if not r["success"]]
    
    avg_request_time = sum(r["duration"] for r in results) / len(results)
    min_request_time = min(r["duration"] for r in results)
    max_request_time = max(r["duration"] for r in results)
    
    time_per_request = total_time / num_parallel  # Wall clock time per request
    
    print(f"Total wall clock time: {total_time:.2f}s")
    print(f"Average request duration: {avg_request_time:.2f}s")
    print(f"Min request duration: {min_request_time:.2f}s")
    print(f"Max request duration: {max_request_time:.2f}s")
    print(f"Time per request (wall clock): {time_per_request:.2f}s")
    print(f"Successful requests: {len(successful_requests)}/{num_parallel}")
    print(f"Failed requests: {len(failed_requests)}")
    
    if failed_requests:
        print("Failed request status codes:", [r["status_code"] for r in failed_requests])
    
    return {
        "num_parallel": num_parallel,
        "total_time": total_time,
        "avg_request_time": avg_request_time,
        "min_request_time": min_request_time,
        "max_request_time": max_request_time,
        "time_per_request": time_per_request,
        "successful_requests": len(successful_requests),
        "failed_requests": len(failed_requests),
        "throughput": num_parallel / total_time  # requests per second
    }


def save_results_to_csv(all_results: list):
    """Save results to CSV file"""
    with open(RESULTS_FILE, 'w', newline='') as csvfile:
        fieldnames = [
            'num_parallel', 'total_time', 'avg_request_time', 'min_request_time', 
            'max_request_time', 'time_per_request', 'successful_requests', 
            'failed_requests', 'throughput'
        ]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_results)
    
    print(f"\nResults saved to {RESULTS_FILE}")


def find_sweet_spot(all_results: list):
    """Find the optimal number of parallel requests"""
    # Filter out results with failed requests for fair comparison
    valid_results = [r for r in all_results if r["failed_requests"] == 0]
    
    if not valid_results:
        print("\nWarning: All test configurations had failed requests!")
        valid_results = all_results
    
    # Find minimum time per request
    best_result = min(valid_results, key=lambda x: x["time_per_request"])
    
    print(f"\n=== SWEET SPOT ANALYSIS ===")
    print(f"Optimal concurrency: {best_result['num_parallel']} parallel requests")
    print(f"Best time per request: {best_result['time_per_request']:.2f}s")
    print(f"Throughput at sweet spot: {best_result['throughput']:.2f} requests/second")
    
    # Also find best throughput
    best_throughput = max(valid_results, key=lambda x: x["throughput"])
    if best_throughput != best_result:
        print(f"\nBest throughput: {best_throughput['num_parallel']} parallel requests")
        print(f"Throughput: {best_throughput['throughput']:.2f} requests/second")
        print(f"Time per request: {best_throughput['time_per_request']:.2f}s")
    
    return best_result


def print_summary_table(all_results: list):
    """Print a summary table of all results"""
    print(f"\n=== PERFORMANCE SUMMARY ===")
    print("Parallel | Total Time | Time/Req | Throughput | Success | Failed")
    print("---------|------------|----------|------------|---------|--------")
    
    for result in all_results:
        print(f"{result['num_parallel']:8d} | "
              f"{result['total_time']:9.2f}s | "
              f"{result['time_per_request']:7.2f}s | "
              f"{result['throughput']:9.2f}/s | "
              f"{result['successful_requests']:7d} | "
              f"{result['failed_requests']:6d}")


if __name__ == "__main__":
    print("=== ChipForge EDA Tools - Parallel Performance Tester ===")
    print(f"API URL: {API_URL}")
    print(f"Test Directory: {TEST_DIR}")
    print(f"Results will be saved to: {RESULTS_FILE}")
    print(f"Test started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    try:
        design_zip, evaluator_zip = pick_test_files(TEST_DIR)
        print(f"Design ZIP: {design_zip}")
        print(f"Evaluator ZIP: {evaluator_zip}")
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    all_results = []
    
    # Test from 1 to 8 parallel requests
    for num_parallel in range(1, 9):
        try:
            result = run_parallel_requests(num_parallel, design_zip, evaluator_zip)
            all_results.append(result)
            
            # Add a small delay between test rounds to avoid overwhelming the server
            if num_parallel < 8:
                print("Waiting 5 seconds before next test...")
                time.sleep(5)
                
        except Exception as e:
            print(f"[ERROR] Failed to run {num_parallel} parallel requests: {e}")
            # Continue with other tests even if one fails
            continue
    
    if all_results:
        print_summary_table(all_results)
        find_sweet_spot(all_results)
        save_results_to_csv(all_results)
    else:
        print("[ERROR] No successful tests completed!")
        sys.exit(1)
    
    print(f"\nTest completed at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("Performance testing finished!")