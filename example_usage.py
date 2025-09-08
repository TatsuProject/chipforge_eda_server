"""
example_usage.py

This script demonstrates how to run the EDA validators from the terminal.
It can be used for quick validation and testing of Verilog designs using the server's API or local Docker containers.
"""
import os
import zipfile
import requests

API_URL = os.getenv("EDA_BASE_URL", "http://localhost:8080")
API_KEY = os.getenv("EDA_API_KEY", "<your_api_key_here>")  # Default empty key if not set
TEST_DESIGN = "test_designs/adder.zip"


def validate_design(design_zip_path):
    """Send a design ZIP to the validator API and print the result."""
    if not os.path.exists(design_zip_path):
        print(f"Design file not found: {design_zip_path}")
        return
    with open(design_zip_path, "rb") as f:
        files = {"design_zip": (os.path.basename(design_zip_path), f, "application/zip")}
        headers = {"Authorization": f"Bearer {API_KEY}"}
        print(f"Uploading {design_zip_path} to {API_URL}/evaluate ...")
        resp = requests.post(f"{API_URL}/evaluate", files=files, headers=headers)
        if resp.ok:
            print("Validation result:")
            print(resp.json())
        else:
            print(f"Error: {resp.status_code}")
            print(resp.text)


if __name__ == "__main__":
    print("--- EDA Validator Example Usage ---")
    validate_design(TEST_DESIGN)
    print("--- Done ---")
