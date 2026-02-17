from dotenv import load_dotenv
load_dotenv()

import asyncio
from guardian.agents.core_agents import monitor_agent

async def test():
    result = await monitor_agent.run(
        "Location: Nairobi (lat:-1.2921, lon:36.8219)\nQuery: flood risk in nairobi"
    )
    print("Monitor result:", result.text)

asyncio.run(test())