"""Probe whether this eBay app is entitled to the Marketplace Insights API.

Marketplace Insights is the only official source of eBay SOLD transactions. It
is a Limited Release requiring business approval, so entitlement must be proven
before any comps work is designed around it.

This asks eBay's OAuth server for a token carrying the insights scope. A refusal
is a definitive "not entitled" answer and costs nothing - no sales data is
requested and no Marketplace Insights query is issued.

Usage:  EBAY_APP_ID=... EBAY_CERT_ID=... python check_sold_source.py
"""

import base64
import os
import sys

import requests

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
INSIGHTS_SCOPE = "https://api.ebay.com/oauth/api_scope/buy.marketplace.insights"
BASE_SCOPE = "https://api.ebay.com/oauth/api_scope"


def try_scope(app_id, cert_id, scope):
    basic = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
    resp = requests.post(
        TOKEN_URL,
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": scope},
        timeout=30,
    )
    return resp.status_code, resp.text[:400]


def main():
    app_id, cert_id = os.environ.get("EBAY_APP_ID"), os.environ.get("EBAY_CERT_ID")
    if not app_id or not cert_id:
        sys.exit("error: set EBAY_APP_ID and EBAY_CERT_ID")

    print("Probing eBay OAuth scopes (no sales data is requested)\n")
    for label, scope in (("baseline Browse scope", BASE_SCOPE),
                         ("Marketplace Insights scope", INSIGHTS_SCOPE)):
        status, body = try_scope(app_id, cert_id, scope)
        ok = status == 200
        print(f"  {label:28} HTTP {status}  {'GRANTED' if ok else 'DENIED'}")
        if not ok:
            print(f"      {body}")

    print("\nInterpretation:")
    print("  baseline GRANTED + insights GRANTED -> sold comps are viable; proceed")
    print("  baseline GRANTED + insights DENIED   -> no official sold source;")
    print("                                          the comps pilot is BLOCKED")


if __name__ == "__main__":
    main()
