import os
import asyncio
from dotenv import load_dotenv
load_dotenv()

from azure.identity import DefaultAzureCredential
from agent_framework import Agent, tool, WorkflowBuilder
from agent_framework_azure_ai import AzureAIClient
from guardian.services.weather_service import assess_flood_risk
from typing import Annotated
from pydantic import Field

credential = DefaultAzureCredential()
ai_client = AzureAIClient(
    project_endpoint=os.getenv('AZURE_AI_PROJECT_ENDPOINT'),
    credential=credential,
    model_deployment_name=os.getenv('FOUNDRY_DEPLOYMENT')
)

# ─── TOOLS ────────────────────────────────────────────────────────────────────

@tool
def fetch_weather(
    lat: Annotated[float, Field(description="Latitude of location")],
    lon: Annotated[float, Field(description="Longitude of location")]
) -> str:
    """Fetch real-time weather data for a location"""
    try:
        data = assess_flood_risk(lat, lon)
        if not data:
            return "Weather data unavailable"
        current = data.get('current', {})
        hourly = data.get('hourly', {})
        precip_history = [p for p in hourly.get('precipitation', [])[-24:] if p is not None]
        total_24h = round(sum(precip_history), 2)
        return (
            f"Temperature: {current.get('temperature_2m', 'N/A')}°C | "
            f"Precipitation: {current.get('precipitation', 0)}mm | "
            f"Rain: {current.get('rain', 0)}mm | "
            f"Humidity: {current.get('relative_humidity_2m', 'N/A')}% | "
            f"Total rain last 24h: {total_24h}mm"
        )
    except Exception as e:
        return f"Weather fetch error: {str(e)}"

@tool
def create_alert(
    risk_type: Annotated[str, Field(description="Type: flood/drought/heatwave")],
    risk_level: Annotated[int, Field(description="Risk level 0-100")],
    message: Annotated[str, Field(description="Alert message for communities")]
) -> str:
    """Create and save an alert to database"""
    try:
        from guardian.models import AlertLog
        AlertLog.objects.create(
            user=None,
            location=None,
            risk_type=risk_type,
            risk_level=risk_level,
            message=message,
            weather_data={'auto_generated': True}
        )
        return f"Alert saved: {risk_type} at {risk_level}% - {message}"
    except Exception as e:
        return f"Alert noted: {risk_type} at {risk_level}% - {message}"

# ─── AGENTS (no tools in workflow agents) ─────────────────────────────────────

monitor_agent = Agent(
    client=ai_client,
    name="MonitorAgent",
    instructions="""You are the Monitor Agent for ResilientEco Guardian Kenya.
Analyze the weather data provided and summarize:
- Current conditions (temp, rain, humidity)
- Last 24h rainfall total
- Any anomalies detected (heavy rain, drought, extreme heat)
Be factual and concise."""
)

predict_agent = Agent(
    client=ai_client,
    name="PredictAgent",
    instructions="""You are the Predict Agent for ResilientEco Guardian Kenya.
Based on monitor data output EXACTLY:
- Flood risk: X% (low/medium/high)
- Drought risk: X% (low/medium/high)
- Heatwave risk: X% (low/medium/high)
- Overall risk: low/medium/high
- Confidence: X%"""
)

decision_agent = Agent(
    client=ai_client,
    name="DecisionAgent",
    instructions="""You are the Decision Agent for ResilientEco Guardian Kenya.
Based on predictions output EXACTLY:
- Alert level: GREEN/YELLOW/ORANGE/RED
- Immediate actions: yes/no
- Recommended actions: (numbered list)
- Who to notify: (list)
- Priority: low/medium/high/critical"""
)

action_agent = Agent(
    client=ai_client,
    name="ActionAgent",
    instructions="""You are the Action Agent for ResilientEco Guardian Kenya.
Based on decisions output EXACTLY:
ALERT_MESSAGE: <clear community message>
SMS_MESSAGE: <under 160 chars>
RISK_TYPE: <flood or drought or heatwave>
RISK_LEVEL: <number 0-100>"""
)

governance_agent = Agent(
    client=ai_client,
    name="GovernanceAgent",
    instructions="""You are the Governance Agent for ResilientEco Guardian Kenya.
Review all previous outputs for accuracy, bias, responsible AI compliance.
Output EXACTLY:
- Approved: yes/no
- Issues: (list or none)
- Final recommendation: (one clear sentence for the user)"""
)

# ─── WORKFLOW ─────────────────────────────────────────────────────────────────

def build_workflow():
    builder = WorkflowBuilder(
        start_executor=monitor_agent,
        output_executors=[
            monitor_agent,
            predict_agent,
            decision_agent,
            action_agent,
            governance_agent
        ]
    )
    builder.add_edge(monitor_agent, predict_agent)
    builder.add_edge(predict_agent, decision_agent)
    builder.add_edge(decision_agent, action_agent)
    builder.add_edge(action_agent, governance_agent)
    return builder.build()

async def run_workflow_async(query: str, lat: float = -1.2921,
                              lon: float = 36.8219,
                              location_name: str = "Nairobi") -> dict:
    """Run 5-agent workflow - fetch weather first, then pass to workflow"""

    # Step 1: Fetch real weather data separately
    weather_info = fetch_weather(lat=lat, lon=lon)

    # Step 2: Build enriched query with weather data
    full_query = f"""Location: {location_name} (lat:{lat}, lon:{lon})
Query: {query}

REAL-TIME WEATHER DATA:
{weather_info}

Analyze this data and assess climate risks."""

    # Step 3: Run workflow (no tools needed - data already included)
    workflow = build_workflow()
    result = await workflow.run(message=full_query, stream=False)
    outputs = result.get_outputs()

    agent_names = ['monitor', 'predict', 'decision', 'action', 'governance']
    results = {}

    for i, output in enumerate(outputs):
        if i < len(agent_names):
            key = agent_names[i]
            results[key] = output.text if hasattr(output, 'text') else str(output)

    # Step 4: Save alert from action agent
    if 'action' in results:
        await save_action_alert(results['action'])

    return results if results else {'error': 'No outputs from workflow'}

async def save_action_alert(action_text: str):
    """Parse and save alert from action agent output"""
    import re
    try:
        risk_type_match = re.search(r'RISK_TYPE:\s*(\w+)', action_text)
        risk_level_match = re.search(r'RISK_LEVEL:\s*(\d+)', action_text)
        alert_msg_match = re.search(r'ALERT_MESSAGE:\s*(.+?)(?=SMS_MESSAGE:|$)', action_text, re.DOTALL)

        if risk_type_match and risk_level_match and alert_msg_match:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: create_alert(
                    risk_type=risk_type_match.group(1).strip(),
                    risk_level=int(risk_level_match.group(1)),
                    message=alert_msg_match.group(1).strip()
                )
            )
    except Exception as e:
        print(f"Alert save error: {e}")

def run_all_agents(query: str, lat: float = -1.2921,
                   lon: float = 36.8219,
                   location_name: str = "Nairobi") -> dict:
    """Sync wrapper for async workflow"""
    try:
        return asyncio.run(
            run_workflow_async(query, lat, lon, location_name)
        )
    except Exception as e:
        print(f"Workflow error: {e}")
        return {'error': str(e)}