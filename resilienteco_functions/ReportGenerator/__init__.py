"""
ReportGenerator Azure Function
-------------------------------
Generates detailed climate risk reports (JSON + PDF) for ResilientEco Guardian.
Triggered via HTTP POST from Django dashboard chat or direct API call.

Endpoint: POST /api/ReportGenerator
Body: {
    "report_type": "agricultural" | "meteorological" | "full",
    "locations": [{"name": "Kakamega", "lat": 0.2827, "lon": 34.7519}, ...],
    "org_name": "My Org",
    "org_type": "agriculture" | "meteorological",
    "format": "json" | "pdf" | "both",
    "request_id": "optional-idempotency-key"
}
"""

import azure.functions as func
import json
import logging
import os
import asyncio
import aiohttp
import base64
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_KEY      = os.environ.get("AZURE_OPENAI_KEY", "")
OPENAI_DEPLOYMENT     = os.environ.get("FOUNDRY_DEPLOYMENT", "gpt-4o")
OPENAI_API_VERSION    = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-08-01-preview")
WEATHER_API_KEY       = os.environ.get("VISUAL_CROSSING_KEY", "")


# ═══════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════

async def main(req: func.HttpRequest) -> func.HttpResponse:
    logger.info("ReportGenerator triggered")

    headers = {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, x-functions-key",
    }

    if req.method == "OPTIONS":
        return func.HttpResponse("", status_code=200, headers=headers)

    try:
        body = req.get_json()
    except Exception:
        return func.HttpResponse(
            json.dumps({"error": "Invalid JSON body"}),
            status_code=400, headers=headers
        )

    report_type = body.get("report_type", "full")
    locations   = body.get("locations", [])
    org_name    = body.get("org_name", "Your Organisation")
    org_type    = body.get("org_type", "agriculture")
    fmt         = body.get("format", "both")

    if not locations:
        return func.HttpResponse(
            json.dumps({"error": "locations array is required"}),
            status_code=400, headers=headers
        )

    weather_data = await fetch_weather_all(locations)
    report = await generate_report(report_type, locations, weather_data, org_name, org_type)

    pdf_b64 = None
    if fmt in ("pdf", "both"):
        pdf_b64 = generate_pdf_base64(report, org_name, org_type)

    response_body = {
        "success": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "org_name": org_name,
        "report_type": report_type,
        "report": report,
    }

    if pdf_b64:
        response_body["pdf_base64"] = pdf_b64
        response_body["pdf_filename"] = (
            f"climate_risk_report_{org_name.replace(' ', '_').lower()}_"
            f"{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
        )

    return func.HttpResponse(
        json.dumps(response_body),
        status_code=200,
        headers=headers
    )


# ═══════════════════════════════════════════════════════════════
# WEATHER FETCHING
# ═══════════════════════════════════════════════════════════════

async def fetch_weather_one(session: aiohttp.ClientSession, loc: dict) -> dict:
    lat  = loc.get("lat")
    lon  = loc.get("lon")
    name = loc.get("name", "Unknown")

    if WEATHER_API_KEY:
        try:
            url = (
                f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/"
                f"timeline/{lat},{lon}?unitGroup=metric&key={WEATHER_API_KEY}"
                f"&contentType=json&include=current,hours,days&elements="
                f"datetime,temp,humidity,precip,precipprob,conditions,description,"
                f"windspeed,winddir,cloudcover,uvindex,tempmax,tempmin"
            )
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as r:
                if r.status == 200:
                    d     = await r.json()
                    cur   = d.get("currentConditions", {})
                    today = d["days"][0] if d.get("days") else {}
                    tmrw  = d["days"][1] if len(d.get("days", [])) > 1 else {}
                    hours = today.get("hours", [])
                    return {
                        "name": name, "lat": lat, "lon": lon, "source": "Visual Crossing",
                        "temperature":  cur.get("temp"),
                        "humidity":     cur.get("humidity"),
                        "precipitation":cur.get("precip", 0),
                        "rain_24h":     today.get("precip", 0),
                        "wind_speed":   cur.get("windspeed"),
                        "conditions":   cur.get("conditions", ""),
                        "description":  today.get("description", ""),
                        "is_raining":   (cur.get("precip", 0) or 0) > 0,
                        "today": {
                            "temp_max":    today.get("tempmax"),
                            "temp_min":    today.get("tempmin"),
                            "precip_mm":   today.get("precip", 0),
                            "precip_prob": today.get("precipprob", 0),
                            "conditions":  today.get("conditions", ""),
                            "description": today.get("description", ""),
                            "wind_speed":  today.get("windspeed"),
                            "uv_index":    today.get("uvindex"),
                            "cloud_cover": today.get("cloudcover"),
                        },
                        "tomorrow": {
                            "temp_max":    tmrw.get("tempmax"),
                            "temp_min":    tmrw.get("tempmin"),
                            "precip_mm":   tmrw.get("precip", 0),
                            "precip_prob": tmrw.get("precipprob", 0),
                            "conditions":  tmrw.get("conditions", ""),
                            "description": tmrw.get("description", ""),
                        },
                        "hourly_today": [
                            {
                                "time":        h.get("datetime", ""),
                                "temp":        h.get("temp"),
                                "precip":      h.get("precip", 0),
                                "precip_prob": h.get("precipprob", 0),
                                "conditions":  h.get("conditions", ""),
                            }
                            for h in hours[::3]
                        ],
                    }
        except Exception as e:
            logger.warning(f"Visual Crossing failed for {name}: {e}")

    # Open-Meteo fallback
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
            f"&current=temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m"
            f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max"
            f"&hourly=temperature_2m,precipitation_probability,precipitation"
            f"&timezone=Africa%2FNairobi&forecast_days=2"
        )
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as r:
            if r.status == 200:
                d      = await r.json()
                cur    = d.get("current", {})
                daily  = d.get("daily", {})
                hourly = d.get("hourly", {})
                return {
                    "name": name, "lat": lat, "lon": lon, "source": "Open-Meteo",
                    "temperature":  cur.get("temperature_2m"),
                    "humidity":     cur.get("relative_humidity_2m"),
                    "precipitation":cur.get("precipitation", 0),
                    "rain_24h":     (daily.get("precipitation_sum") or [0])[0],
                    "wind_speed":   cur.get("wind_speed_10m"),
                    "conditions":   "Current conditions",
                    "description":  "",
                    "is_raining":   (cur.get("precipitation", 0) or 0) > 0,
                    "today": {
                        "temp_max":    (daily.get("temperature_2m_max") or [None])[0],
                        "temp_min":    (daily.get("temperature_2m_min") or [None])[0],
                        "precip_mm":   (daily.get("precipitation_sum") or [0])[0],
                        "precip_prob": (daily.get("precipitation_probability_max") or [0])[0],
                        "conditions":  "",
                        "description": "",
                        "wind_speed":  cur.get("wind_speed_10m"),
                        "uv_index":    None,
                        "cloud_cover": None,
                    },
                    "tomorrow": {
                        "temp_max":    (daily.get("temperature_2m_max") or [None, None])[1],
                        "temp_min":    (daily.get("temperature_2m_min") or [None, None])[1],
                        "precip_mm":   (daily.get("precipitation_sum") or [0, 0])[1],
                        "precip_prob": (daily.get("precipitation_probability_max") or [0, 0])[1],
                        "conditions":  "",
                        "description": "",
                    },
                    "hourly_today": [
                        {
                            "time":        (hourly.get("time") or [""] * (i+1))[i],
                            "temp":        (hourly.get("temperature_2m") or [None] * (i+1))[i],
                            "precip":      (hourly.get("precipitation") or [0] * (i+1))[i],
                            "precip_prob": (hourly.get("precipitation_probability") or [0] * (i+1))[i],
                            "conditions":  "",
                        }
                        for i in range(0, min(24, len(hourly.get("time", []))), 3)
                    ],
                }
    except Exception as e:
        logger.warning(f"Open-Meteo failed for {name}: {e}")

    return {
        "name": name, "lat": lat, "lon": lon, "source": "unavailable",
        "temperature": None, "humidity": None, "precipitation": 0, "rain_24h": 0,
        "wind_speed": None, "is_raining": False,
        "conditions": "Data unavailable", "description": "",
        "today": {}, "tomorrow": {}, "hourly_today": [],
    }


async def fetch_weather_all(locations: list) -> list:
    async with aiohttp.ClientSession() as session:
        return await asyncio.gather(*[fetch_weather_one(session, loc) for loc in locations])


# ═══════════════════════════════════════════════════════════════
# AI REPORT GENERATION
# ═══════════════════════════════════════════════════════════════

def build_weather_summary(weather_data: list) -> str:
    lines = []
    for w in weather_data:
        h_lines = ", ".join(
            f"{h['time']}: {h['temp']}°C {h['precip']}mm ({h['precip_prob']}%)"
            for h in w.get("hourly_today", [])
        )
        lines.append(
            f"\nZone: {w['name']} ({w.get('lat')}, {w.get('lon')}) — Source: {w.get('source')}\n"
            f"  Now: {w.get('temperature')}°C, Humidity {w.get('humidity')}%, "
            f"Precip now {w.get('precipitation')}mm, Wind {w.get('wind_speed')} km/h\n"
            f"  Conditions: {w.get('conditions')} — {w.get('description','')}\n"
            f"  Rain 24h: {w.get('rain_24h')}mm, Raining now: {w.get('is_raining')}\n"
            f"  Today: Max {w.get('today',{}).get('temp_max')}°C / Min {w.get('today',{}).get('temp_min')}°C, "
            f"Precip {w.get('today',{}).get('precip_mm')}mm at {w.get('today',{}).get('precip_prob')}% prob, "
            f"UV {w.get('today',{}).get('uv_index')}, Cloud {w.get('today',{}).get('cloud_cover')}%\n"
            f"  Tomorrow: Max {w.get('tomorrow',{}).get('temp_max')}°C / Min {w.get('tomorrow',{}).get('temp_min')}°C, "
            f"Precip {w.get('tomorrow',{}).get('precip_mm')}mm at {w.get('tomorrow',{}).get('precip_prob')}% prob\n"
            f"  Hourly (3h): {h_lines or 'unavailable'}"
        )
    return "\n".join(lines)


async def generate_report(
    report_type: str,
    locations: list,
    weather_data: list,
    org_name: str,
    org_type: str,
) -> dict:

    weather_summary = build_weather_summary(weather_data)

    if org_type == "meteorological":
        system = (
            "You are a senior meteorologist producing official meteorological risk assessments "
            "for national agencies. Reports must be technically precise, data-driven, and include "
            "specific numerical values, confidence intervals, and professional recommendations."
        )
    else:
        system = (
            "You are an expert agricultural climatologist producing detailed climate risk assessments "
            "for farmers and agricultural organisations. Reports must name specific crops, precise "
            "irrigation schedules, planting windows, and concrete actionable steps with timeframes."
        )

    user = f"""
Organisation: {org_name}
Report Type: {report_type.upper()}
Generated: {datetime.now(timezone.utc).strftime('%A, %d %B %Y at %H:%M UTC')}
Zones analysed: {len(locations)}

LIVE WEATHER DATA:
{weather_summary}

Generate a COMPREHENSIVE CLIMATE RISK REPORT.

Rules:
- Use the ACTUAL numbers from the weather data above — do not invent values
- Every risk score must be a specific integer between 0-100
- Every recommendation must be specific (e.g. "Irrigate at 06:00-08:00 using drip irrigation" not "irrigate regularly")
- The report_narrative must be 400-600 words of professional prose suitable for printing
- Hourly forecast must use the actual hourly data provided
- Crop risk scores must be calculated from actual flood/drought/heat values using crop sensitivity

Respond ONLY with valid JSON. No markdown, no backticks, no preamble.

{{
  "executive_summary": "3-4 sentence summary of overall risk status",
  "overall_alert_level": "GREEN|YELLOW|ORANGE|RED",
  "overall_risk_score": 0-100,
  "confidence": 0-100,
  "zones": [
    {{
      "name": "string",
      "alert_level": "GREEN|YELLOW|ORANGE|RED",
      "flood_risk": 0-100,
      "drought_risk": 0-100,
      "heatwave_risk": 0-100,
      "confidence": 0-100,
      "current_conditions": {{
        "temperature": number,
        "humidity": number,
        "rain_24h": number,
        "wind_speed": number,
        "conditions": "string",
        "uv_index": number_or_null
      }},
      "forecast_48h": "detailed 48h narrative with specific values",
      "hourly_forecast": [
        {{"time": "HH:MM", "temp": number, "precip_mm": number, "precip_prob": number, "conditions": "string"}}
      ],
      "key_risks": ["specific risk with numbers"],
      "immediate_actions": ["specific action with timing"]
    }}
  ],
  "crop_risk_matrix": [
    {{
      "zone": "string",
      "crop": "string",
      "flood_risk": 0-100,
      "drought_risk": 0-100,
      "heat_risk": 0-100,
      "overall_risk": 0-100,
      "action": "specific action"
    }}
  ],
  "irrigation_recommendations": ["specific recommendation per zone"],
  "planting_window": {{
    "current_season": "Long Rains (MAM)|Short Rains (OND)|Dry Season",
    "season_notes": "string",
    "recommended_crops": ["crop — reason"],
    "crops_to_delay": ["crop — reason"],
    "crops_to_avoid": ["crop — reason"]
  }},
  "action_items_24h": ["numbered specific action"],
  "action_items_7days": ["numbered specific action"],
  "outlook_7day": "detailed 7-day narrative",
  "sdg_alignment": ["SDG X: description"],
  "data_sources": ["source name"],
  "report_narrative": "400-600 words of professional prose covering all key findings"
}}
"""

    try:
        async with aiohttp.ClientSession() as session:
            url = (
                f"{AZURE_OPENAI_ENDPOINT.rstrip('/')}/openai/deployments/"
                f"{OPENAI_DEPLOYMENT}/chat/completions?api-version={OPENAI_API_VERSION}"
            )
            payload = {
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                "max_tokens": 4000,
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            }
            async with session.post(
                url,
                json=payload,
                headers={"api-key": AZURE_OPENAI_KEY, "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status == 200:
                    data    = await resp.json()
                    content = data["choices"][0]["message"]["content"]
                    return json.loads(content)
                else:
                    err = await resp.text()
                    logger.error(f"OpenAI {resp.status}: {err}")
                    return _fallback_report(locations, weather_data, org_name)
    except Exception as e:
        logger.error(f"OpenAI call failed: {e}")
        return _fallback_report(locations, weather_data, org_name)


def _fallback_report(locations, weather_data, org_name):
    zones = []
    for i, loc in enumerate(locations):
        w    = weather_data[i] if i < len(weather_data) else {}
        rain = float(w.get("rain_24h") or 0)
        temp = float(w.get("temperature") or 25)
        f    = min(100, int(rain * 4))
        d    = max(0, 40 - int(rain * 3))
        h    = max(0, int((temp - 28) * 5)) if temp > 28 else 0
        zones.append({
            "name": loc.get("name"), "alert_level": "YELLOW",
            "flood_risk": f, "drought_risk": d, "heatwave_risk": h, "confidence": 55,
            "current_conditions": {"temperature": temp, "humidity": w.get("humidity"), "rain_24h": rain, "conditions": w.get("conditions","")},
            "forecast_48h": "AI service temporarily unavailable. Retry in a few minutes.",
            "hourly_forecast": [], "key_risks": [f"Rain: {rain}mm", f"Temp: {temp}°C"],
            "immediate_actions": ["Monitor conditions manually", "Retry AI analysis shortly"],
        })
    return {
        "executive_summary": f"Fallback report for {org_name}. AI analysis temporarily unavailable.",
        "overall_alert_level": "YELLOW", "overall_risk_score": 40, "confidence": 40,
        "zones": zones, "crop_risk_matrix": [], "irrigation_recommendations": [],
        "planting_window": {"current_season": "Pending", "recommended_crops": [], "crops_to_delay": [], "crops_to_avoid": []},
        "action_items_24h": ["Retry full AI analysis"], "action_items_7days": [],
        "outlook_7day": "Unavailable", "sdg_alignment": [], "data_sources": ["fallback"],
        "report_narrative": f"Automated report for {org_name} is in fallback mode. Please retry.",
    }


# ═══════════════════════════════════════════════════════════════
# PDF GENERATION
# ═══════════════════════════════════════════════════════════════

def generate_pdf_base64(report: dict, org_name: str, org_type: str) -> Optional[str]:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.lib.colors import HexColor, white
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
        from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
        import io

        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4,
                                rightMargin=2*cm, leftMargin=2*cm,
                                topMargin=2*cm,   bottomMargin=2*cm)

        PRIMARY   = HexColor("#1e3a8a") if org_type == "meteorological" else HexColor("#14532d")
        SECONDARY = HexColor("#1d4ed8") if org_type == "meteorological" else HexColor("#15803d")
        ACCENT    = HexColor("#06b6d4") if org_type == "meteorological" else HexColor("#d97706")
        LIGHT     = HexColor("#f8fafc")
        BORDER    = HexColor("#e2e8f0")
        ALERT_C   = {"RED": HexColor("#dc2626"), "ORANGE": HexColor("#ea580c"),
                     "YELLOW": HexColor("#d97706"), "GREEN": HexColor("#16a34a")}

        base   = getSampleStyleSheet()
        def S(name, **kw):
            return ParagraphStyle(name, parent=base["Normal"], **kw)

        Styles = {
            "H1":    S("H1",    fontSize=14, textColor=PRIMARY,   fontName="Helvetica-Bold", spaceBefore=12, spaceAfter=4),
            "H2":    S("H2",    fontSize=11, textColor=SECONDARY, fontName="Helvetica-Bold", spaceBefore=8,  spaceAfter=3),
            "body":  S("body",  fontSize=9,  textColor=HexColor("#334155"), leading=14, spaceAfter=3),
            "small": S("small", fontSize=7,  textColor=HexColor("#64748b"), leading=11),
            "white": S("white", fontSize=11, textColor=white, fontName="Helvetica-Bold", alignment=TA_CENTER),
            "whiteS":S("whiteS",fontSize=9,  textColor=HexColor("#cbd5e1"), alignment=TA_CENTER),
            "mono":  S("mono",  fontSize=8,  fontName="Courier", textColor=HexColor("#475569"), leading=11),
            "label": S("label", fontSize=7,  textColor=HexColor("#64748b"), fontName="Helvetica-Bold"),
            "big":   S("big",   fontSize=18, fontName="Helvetica-Bold", alignment=TA_CENTER),
        }

        story = []

        # ── Cover ────────────────────────────────────────────────
        icon = "📡" if org_type == "meteorological" else "🌿"
        kind = "Meteorological" if org_type == "meteorological" else "Agricultural"
        hdr  = Table([[Paragraph(f"{icon} {kind} Climate Risk Report", Styles["white"]),
                       Paragraph(f"{org_name}  ·  {datetime.now().strftime('%d %B %Y  %H:%M UTC')}", Styles["whiteS"])]],
                     colWidths=[17*cm])
        hdr.setStyle(TableStyle([
            ("BACKGROUND",    (0,0),(-1,-1), PRIMARY),
            ("TOPPADDING",    (0,0),(-1,-1), 14),
            ("BOTTOMPADDING", (0,0),(-1,-1), 12),
            ("LEFTPADDING",   (0,0),(-1,-1), 14),
            ("RIGHTPADDING",  (0,0),(-1,-1), 14),
        ]))
        story.append(hdr)
        story.append(Spacer(1, 10))

        # ── Overall status ───────────────────────────────────────
        overall = report.get("overall_alert_level", "GREEN")
        ocol    = ALERT_C.get(overall, ALERT_C["GREEN"])
        stat    = Table(
            [["ALERT LEVEL", "RISK SCORE", "CONFIDENCE", "ZONES"],
             [overall, f"{report.get('overall_risk_score',0)}%",
              f"{report.get('confidence',0)}%", str(len(report.get("zones",[])))]]
        , colWidths=[4.25*cm]*4)
        stat.setStyle(TableStyle([
            ("BACKGROUND",    (0,0),(-1,0), HexColor("#f1f5f9")),
            ("BACKGROUND",    (0,1),(0,1),  ocol),
            ("BACKGROUND",    (1,1),(-1,-1),LIGHT),
            ("BOX",           (0,0),(-1,-1),0.5, BORDER),
            ("INNERGRID",     (0,0),(-1,-1),0.5, BORDER),
            ("ALIGN",         (0,0),(-1,-1),"CENTER"),
            ("VALIGN",        (0,0),(-1,-1),"MIDDLE"),
            ("FONTNAME",      (0,0),(-1,0), "Helvetica-Bold"),
            ("FONTSIZE",      (0,0),(-1,0), 7),
            ("FONTNAME",      (0,1),(-1,-1),"Helvetica-Bold"),
            ("FONTSIZE",      (0,1),(-1,-1),16),
            ("TEXTCOLOR",     (0,1),(0,1),  white),
            ("TEXTCOLOR",     (1,1),(-1,-1),PRIMARY),
            ("TOPPADDING",    (0,0),(-1,-1),8),
            ("BOTTOMPADDING", (0,0),(-1,-1),8),
        ]))
        story.append(stat)
        story.append(Spacer(1, 10))

        # ── Executive summary ────────────────────────────────────
        story.append(Paragraph("Executive Summary", Styles["H1"]))
        story.append(HRFlowable(width="100%", thickness=1.5, color=PRIMARY))
        story.append(Spacer(1, 4))
        story.append(Paragraph(report.get("executive_summary", ""), Styles["body"]))
        story.append(Spacer(1, 8))

        # ── Narrative ────────────────────────────────────────────
        narrative = report.get("report_narrative", "")
        if narrative:
            story.append(Paragraph("Detailed Analysis", Styles["H1"]))
            story.append(HRFlowable(width="100%", thickness=1.5, color=PRIMARY))
            story.append(Spacer(1, 4))
            for para in narrative.split("\n\n"):
                if para.strip():
                    story.append(Paragraph(para.strip(), Styles["body"]))
                    story.append(Spacer(1, 3))
            story.append(Spacer(1, 8))

        # ── Zone analysis ────────────────────────────────────────
        story.append(Paragraph("Zone-by-Zone Analysis", Styles["H1"]))
        story.append(HRFlowable(width="100%", thickness=1.5, color=PRIMARY))
        story.append(Spacer(1, 6))

        for z in report.get("zones", []):
            lvl  = z.get("alert_level", "GREEN")
            zcol = ALERT_C.get(lvl, ALERT_C["GREEN"])
            cur  = z.get("current_conditions", {})

            zh = Table([[Paragraph(f"📍  {z.get('name','')}", S("zh", fontSize=11, textColor=white, fontName="Helvetica-Bold")),
                         Paragraph(lvl, S("zl", fontSize=10, textColor=white, fontName="Helvetica-Bold", alignment=TA_RIGHT))]],
                       colWidths=[13*cm, 4*cm])
            zh.setStyle(TableStyle([
                ("BACKGROUND",    (0,0),(-1,-1),zcol),
                ("TOPPADDING",    (0,0),(-1,-1),7),
                ("BOTTOMPADDING", (0,0),(-1,-1),7),
                ("LEFTPADDING",   (0,0),(-1,-1),10),
                ("RIGHTPADDING",  (0,0),(-1,-1),10),
                ("VALIGN",        (0,0),(-1,-1),"MIDDLE"),
            ]))
            story.append(zh)

            def rc(v):
                if v >= 70: return HexColor("#dc2626")
                if v >= 50: return HexColor("#ea580c")
                if v >= 30: return HexColor("#d97706")
                return HexColor("#16a34a")

            rt = Table(
                [["FLOOD RISK","DROUGHT RISK","HEATWAVE RISK","CONFIDENCE"],
                 [f"{z.get('flood_risk',0)}%", f"{z.get('drought_risk',0)}%",
                  f"{z.get('heatwave_risk',0)}%", f"{z.get('confidence',0)}%"]],
                colWidths=[4.25*cm]*4
            )
            rt.setStyle(TableStyle([
                ("BACKGROUND",    (0,0),(-1,0),HexColor("#f1f5f9")),
                ("BACKGROUND",    (0,1),(-1,-1),LIGHT),
                ("BOX",           (0,0),(-1,-1),0.5,BORDER),
                ("INNERGRID",     (0,0),(-1,-1),0.5,BORDER),
                ("ALIGN",         (0,0),(-1,-1),"CENTER"),
                ("VALIGN",        (0,0),(-1,-1),"MIDDLE"),
                ("FONTNAME",      (0,0),(-1,0),"Helvetica-Bold"),
                ("FONTSIZE",      (0,0),(-1,0),7),
                ("FONTNAME",      (0,1),(-1,-1),"Helvetica-Bold"),
                ("FONTSIZE",      (0,1),(-1,-1),14),
                ("TEXTCOLOR",     (0,1),(0,1),rc(z.get("flood_risk",0))),
                ("TEXTCOLOR",     (1,1),(1,1),rc(z.get("drought_risk",0))),
                ("TEXTCOLOR",     (2,1),(2,1),rc(z.get("heatwave_risk",0))),
                ("TEXTCOLOR",     (3,1),(3,1),SECONDARY),
                ("TOPPADDING",    (0,0),(-1,-1),6),
                ("BOTTOMPADDING", (0,0),(-1,-1),6),
            ]))
            story.append(rt)

            if cur:
                story.append(Paragraph(
                    f"<b>Now:</b> {cur.get('temperature')}°C · Humidity {cur.get('humidity')}% · "
                    f"Rain 24h {cur.get('rain_24h')}mm · Wind {cur.get('wind_speed')} km/h · {cur.get('conditions','')}",
                    S("cond", fontSize=9, textColor=HexColor("#334155"), leftIndent=4, spaceBefore=4)
                ))

            if z.get("forecast_48h"):
                story.append(Paragraph(
                    f"<b>48h Forecast:</b> {z['forecast_48h']}",
                    S("fc", fontSize=9, textColor=HexColor("#475569"), leftIndent=4, spaceAfter=3)
                ))

            hourly = z.get("hourly_forecast", [])
            if hourly:
                story.append(Paragraph("<b>Hourly Forecast:</b>", S("hl", fontSize=9, fontName="Helvetica-Bold", leftIndent=4)))
                hdr_row = ["Time", "Temp °C", "Precip mm", "Prob %", "Conditions"]
                hrows   = [hdr_row] + [
                    [h.get("time",""), str(h.get("temp","")), str(h.get("precip_mm","")),
                     str(h.get("precip_prob","")), h.get("conditions","")]
                    for h in hourly[:8]
                ]
                ht = Table(hrows, colWidths=[2*cm, 2.2*cm, 2.4*cm, 2*cm, 8.4*cm])
                ht.setStyle(TableStyle([
                    ("BACKGROUND",  (0,0),(-1,0),PRIMARY),
                    ("TEXTCOLOR",   (0,0),(-1,0),white),
                    ("FONTNAME",    (0,0),(-1,0),"Helvetica-Bold"),
                    ("FONTSIZE",    (0,0),(-1,-1),7),
                    ("ROWBACKGROUNDS",(0,1),(-1,-1),[white,LIGHT]),
                    ("BOX",         (0,0),(-1,-1),0.5,BORDER),
                    ("INNERGRID",   (0,0),(-1,-1),0.3,BORDER),
                    ("ALIGN",       (1,0),(3,-1),"CENTER"),
                    ("TOPPADDING",  (0,0),(-1,-1),3),
                    ("BOTTOMPADDING",(0,0),(-1,-1),3),
                    ("LEFTPADDING", (0,0),(-1,-1),4),
                ]))
                story.append(ht)

            actions = z.get("immediate_actions", [])
            if actions:
                story.append(Paragraph("<b>Immediate Actions:</b>",
                    S("ia", fontSize=9, fontName="Helvetica-Bold", leftIndent=4, spaceBefore=3)))
                for a in actions[:6]:
                    story.append(Paragraph(f"• {a}",
                        S("ab", fontSize=8, textColor=HexColor("#334155"), leftIndent=12, spaceAfter=1)))

            story.append(Spacer(1, 10))

        # ── Crop risk matrix ─────────────────────────────────────
        crops = report.get("crop_risk_matrix", [])
        if crops:
            story.append(Paragraph("Crop Risk Matrix", Styles["H1"]))
            story.append(HRFlowable(width="100%", thickness=1.5, color=PRIMARY))
            story.append(Spacer(1, 4))
            rows = [["Zone","Crop","Flood %","Drought %","Heat %","Overall %","Recommended Action"]]
            for r in crops:
                rows.append([r.get("zone",""), r.get("crop",""),
                              str(r.get("flood_risk",0)), str(r.get("drought_risk",0)),
                              str(r.get("heat_risk",0)), str(r.get("overall_risk",0)),
                              Paragraph(r.get("action",""), S("ca", fontSize=7, leading=10))])
            ct = Table(rows, colWidths=[2.2*cm,2*cm,1.5*cm,1.8*cm,1.5*cm,1.8*cm,6.2*cm])
            ct.setStyle(TableStyle([
                ("BACKGROUND",    (0,0),(-1,0),PRIMARY),
                ("TEXTCOLOR",     (0,0),(-1,0),white),
                ("FONTNAME",      (0,0),(-1,0),"Helvetica-Bold"),
                ("FONTSIZE",      (0,0),(-1,-1),8),
                ("ROWBACKGROUNDS",(0,1),(-1,-1),[white,LIGHT]),
                ("BOX",           (0,0),(-1,-1),0.5,BORDER),
                ("INNERGRID",     (0,0),(-1,-1),0.3,BORDER),
                ("ALIGN",         (2,0),(5,-1),"CENTER"),
                ("VALIGN",        (0,0),(-1,-1),"MIDDLE"),
                ("TOPPADDING",    (0,0),(-1,-1),4),
                ("BOTTOMPADDING", (0,0),(-1,-1),4),
                ("LEFTPADDING",   (0,0),(-1,-1),4),
            ]))
            story.append(ct)
            story.append(Spacer(1, 10))

        # ── Actions ──────────────────────────────────────────────
        a24  = report.get("action_items_24h",  [])
        a7   = report.get("action_items_7days", [])
        if a24 or a7:
            story.append(Paragraph("Action Items", Styles["H1"]))
            story.append(HRFlowable(width="100%", thickness=1.5, color=PRIMARY))
            story.append(Spacer(1, 4))
            if a24:
                story.append(Paragraph("Next 24 Hours", Styles["H2"]))
                for i,a in enumerate(a24,1):
                    story.append(Paragraph(f"{i}. {a}", Styles["body"]))
            if a7:
                story.append(Spacer(1,4))
                story.append(Paragraph("Next 7 Days", Styles["H2"]))
                for i,a in enumerate(a7,1):
                    story.append(Paragraph(f"{i}. {a}", Styles["body"]))
            story.append(Spacer(1, 8))

        # ── Outlook ──────────────────────────────────────────────
        outlook = report.get("outlook_7day","")
        if outlook:
            story.append(Paragraph("7-Day Outlook", Styles["H1"]))
            story.append(HRFlowable(width="100%", thickness=1.5, color=PRIMARY))
            story.append(Spacer(1, 4))
            story.append(Paragraph(outlook, Styles["body"]))
            story.append(Spacer(1, 8))

        # ── Irrigation ───────────────────────────────────────────
        irr = report.get("irrigation_recommendations", [])
        if irr:
            story.append(Paragraph("Irrigation Recommendations", Styles["H1"]))
            story.append(HRFlowable(width="100%", thickness=1.5, color=PRIMARY))
            story.append(Spacer(1, 4))
            for rec in irr:
                story.append(Paragraph(f"• {rec}", Styles["body"]))
            story.append(Spacer(1, 8))

        # ── Planting window ──────────────────────────────────────
        pw = report.get("planting_window", {})
        if pw.get("current_season"):
            story.append(Paragraph("Planting Window", Styles["H1"]))
            story.append(HRFlowable(width="100%", thickness=1.5, color=PRIMARY))
            story.append(Spacer(1, 4))
            story.append(Paragraph(f"<b>Season:</b> {pw.get('current_season','')} — {pw.get('season_notes','')}", Styles["body"]))
            if pw.get("recommended_crops"):
                story.append(Paragraph(f"<b>Plant now:</b> {', '.join(pw['recommended_crops'])}", Styles["body"]))
            if pw.get("crops_to_delay"):
                story.append(Paragraph(f"<b>Delay:</b> {', '.join(pw['crops_to_delay'])}", Styles["body"]))
            if pw.get("crops_to_avoid"):
                story.append(Paragraph(f"<b>Avoid:</b> {', '.join(pw['crops_to_avoid'])}", Styles["body"]))
            story.append(Spacer(1, 8))

        # ── Footer ───────────────────────────────────────────────
        story.append(HRFlowable(width="100%", thickness=0.5, color=BORDER))
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            f"ResilientEco Guardian · Azure AI Foundry · "
            f"{datetime.now().strftime('%d %B %Y %H:%M UTC')} · "
            f"Sources: {', '.join(report.get('data_sources', ['Weather API']))}",
            Styles["small"]
        ))

        doc.build(story)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("utf-8")

    except Exception as e:
        logger.error(f"PDF generation failed: {e}")
        return None