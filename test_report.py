import os
import asyncio

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "resilienteco.settings")

import django
django.setup()

from guardian.services.report_generator_client import call_report_generator, is_report_request

print("=== Detection test ===")
for phrase in ["give me a full report", "hello", "generate climate risk report"]:
    print(f"  '{phrase}' -> {is_report_request(phrase)}")

async def main():
    print("\n=== Function call test ===")
    result = await call_report_generator(
        locations=[{"name": "Nairobi", "lat": -1.2921, "lon": 36.8219}],
        org_name="Test Org",
        org_type="agriculture",
        report_type="agricultural",
        fmt="both"
    )
    print("Success:", result.get("success"))
    print("Alert level:", result.get("report", {}).get("overall_alert_level"))
    print("Risk score:", result.get("report", {}).get("overall_risk_score"))
    print("PDF present:", bool(result.get("pdf_base64")))

asyncio.run(main())