from dotenv import load_dotenv
load_dotenv()

import asyncio, os
from azure.identity import DefaultAzureCredential
from agent_framework import Agent, tool, WorkflowBuilder
from agent_framework_azure_ai import AzureAIClient
from typing import Annotated
from pydantic import Field

credential = DefaultAzureCredential()
ai_client = AzureAIClient(
    project_endpoint=os.getenv('AZURE_AI_PROJECT_ENDPOINT'),
    credential=credential,
    model_deployment_name=os.getenv('FOUNDRY_DEPLOYMENT')
)

@tool
def get_weather(
    location: Annotated[str, Field(description="Location name")]
) -> str:
    """Get weather for a location"""
    return f"Sunny in {location}, 25C, no rain"

agent1 = Agent(
    client=ai_client,
    name="MonitorAgent",
    instructions="Use get_weather tool. Summarize weather conditions.",
    tools=[get_weather]
)

agent2 = Agent(
    client=ai_client,
    name="PredictAgent",
    instructions="Based on weather summary, predict flood risk as low/medium/high."
)

workflow = WorkflowBuilder(
    start_executor=agent1,
    output_executors=[agent1, agent2]
)
workflow.add_edge(agent1, agent2)
wf = workflow.build()

async def test():
    result = await wf.run(message="Check weather in Nairobi", stream=False)
    outputs = result.get_outputs()
    print(f"Number of outputs: {len(outputs)}")
    for i, output in enumerate(outputs):
        print(f"\nOutput {i}: type={type(output)}")
        print(f"Dir: {[x for x in dir(output) if not x.startswith('_')]}")
        print(f"Str: {str(output)[:200]}")

asyncio.run(test())