from dotenv import load_dotenv
load_dotenv()

import asyncio
from guardian.agents.core_agents import (
    monitor_agent, predict_agent, decision_agent, 
    action_agent, governance_agent, build_workflow
)

async def test():
    workflow = build_workflow()
    
    result = await workflow.run(
        message="Location: Nairobi (lat:-1.2921, lon:36.8219)\nQuery: flood risk in nairobi",
        stream=False
    )
    
    outputs = result.get_outputs()
    print(f"Total outputs: {len(outputs)}")
    for i, output in enumerate(outputs):
        print(f"\n--- Output {i} ---")
        print(output.text[:300] if hasattr(output, 'text') else str(output)[:300])

asyncio.run(test())