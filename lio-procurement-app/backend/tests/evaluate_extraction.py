"""Evaluate extraction against the five case-study quotes.

This opt-in script calls the running platform API, so it is intentionally kept
outside the automated test suite. Quote PDFs stay local and are supplied via
``--quotes-dir``; they are not committed as test fixtures.

Example:
    LIO_REVIEW_PASSWORD=demo1234 python tests/evaluate_extraction.py \
        --quotes-dir ../quotes
"""

import argparse
import json
import os
import urllib.request
import uuid
from pathlib import Path


EXPECTED = {
    "QT-4120-Cust-14918.pdf": {
        "vendor": "greenmantle moss studio",
        "buyer": "lio technologies",
        "vat_id": "85-3252405",
        "currency": "USD",
        "total": 1686.14,
        "item_count": 2,
    },
    "QT-OF2312380-Cust-57692.pdf": {
        "vendor": "verdeform",
        "buyer": "lio technologies",
        "vat_id": None,
        "currency": "USD",
        "total": 1413.10,
        "item_count": 3,
    },
    "Quote_1__Lio_Technologies_Inc__1x_MBA___2214703918.pdf": {
        "vendor": "apple business team",
        "buyer": "lio technologies",
        "vat_id": "94-2404110",
        "currency": "USD",
        "total": 1534.59,
        "item_count": 1,
    },
    "Quote_Hartwell_Cleveland_ACME_Grinding_Machine.pdf": {
        "vendor": "hartwell industrial tools",
        "buyer": "acme corp",
        "vat_id": None,
        "currency": "USD",
        "total": 18402.50,
        "item_count": 1,
    },
    "QuoteA0492_23.Pdf": {
        "vendor": "hollis greenscapes",
        "buyer": "lio technologies",
        "vat_id": "94-1985704",
        "currency": "USD",
        "total": 2042.15,
        "item_count": 1,
    },
}


def _post_json(base_url: str, path: str, payload: dict, token: str) -> dict:
    """POST JSON to the local platform API and decode its response."""
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def _parse_pdf(base_url: str, pdf_path: Path, token: str) -> str:
    """Send one PDF through the same parsing endpoint used by the UI."""
    boundary = uuid.uuid4().hex.encode()
    content = pdf_path.read_bytes()
    body = b"--" + boundary + b"\r\n"
    body += (
        b'Content-Disposition: form-data; name="file"; filename="'
        + pdf_path.name.encode()
        + b'"\r\nContent-Type: application/pdf\r\n\r\n'
        + content
        + b"\r\n--"
        + boundary
        + b"--\r\n"
    )
    request = urllib.request.Request(
        f"{base_url}/pdf/parse",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary.decode()}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)["text"]


def evaluate(
    base_url: str,
    quotes_dir: Path,
    email: str,
    password: str,
    min_field_accuracy: float = 0.6,
    min_no_error_rate: float = 1.0,
) -> None:
    """Run the five quotes and fail when regression thresholds are missed."""
    token = _post_json(
        base_url,
        "/auth/signin",
        {"email": email, "password": password},
        token="",
    )["access_token"]
    results = []
    for filename, expected in EXPECTED.items():
        pdf_path = quotes_dir / filename
        text = _parse_pdf(base_url, pdf_path, token)
        result = _post_json(base_url, "/extraction", {"text": text}, token)
        data = result.get("data") or {}
        vendor = (data.get("vendorName") or "").casefold()
        buyer = (data.get("department") or "").casefold()
        vat_id = (data.get("vatId") or "").replace(" ", "").casefold()
        total = data.get("totalCost")
        expected_vat_id = expected["vat_id"]
        vat_id_ok = (
            not vat_id if expected_vat_id is None else expected_vat_id.replace(" ", "").casefold() in vat_id
        )
        results.append(
            {
                "quote": filename,
                "no_error": result.get("success") is True,
                "vendor": expected["vendor"] in vendor,
                "buyer": expected["buyer"] in buyer,
                "vat_id": vat_id_ok,
                "currency": (data.get("currency") or "").upper() == expected["currency"],
                "total": total is not None and abs(total - expected["total"]) <= 0.01,
                "item_count": len(data.get("orderLines") or []) == expected["item_count"],
                "warnings": len(result.get("warnings") or []),
            }
        )

    print(json.dumps(results, indent=2))
    no_error = sum(result["no_error"] for result in results)
    field_checks = [key for key in ("vendor", "buyer", "vat_id", "currency", "total", "item_count")]
    accurate = sum(
        sum(result[key] for key in field_checks) for result in results
    )
    print(f"No-error rate: {no_error}/{len(results)}")
    print(f"Field accuracy: {accurate}/{len(results) * len(field_checks)}")
    no_error_rate = no_error / len(results)
    field_accuracy = accurate / (len(results) * len(field_checks))
    if no_error_rate < min_no_error_rate or field_accuracy < min_field_accuracy:
        raise SystemExit(
            "Extraction regression thresholds failed: "
            f"no-error rate {no_error_rate:.1%} (minimum {min_no_error_rate:.1%}), "
            f"field accuracy {field_accuracy:.1%} (minimum {min_field_accuracy:.1%})."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--quotes-dir", type=Path, required=True)
    parser.add_argument("--base-url", default="http://localhost:7200")
    parser.add_argument("--email", default=os.environ.get("LIO_REVIEW_EMAIL", "admin@acme.com"))
    parser.add_argument("--password", default=os.environ.get("LIO_REVIEW_PASSWORD"))
    parser.add_argument("--min-field-accuracy", type=float, default=0.6)
    parser.add_argument("--min-no-error-rate", type=float, default=1.0)
    args = parser.parse_args()
    if not args.password:
        parser.error("Set LIO_REVIEW_PASSWORD or pass --password")
    evaluate(
        args.base_url,
        args.quotes_dir,
        args.email,
        args.password,
        args.min_field_accuracy,
        args.min_no_error_rate,
    )
