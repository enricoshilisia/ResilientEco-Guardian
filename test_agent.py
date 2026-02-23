from dotenv import load_dotenv
load_dotenv()

import unittest
from guardian.agents.core_agents import MonitorAgent, AgentMessage

class TestMonitorAgent(unittest.TestCase):
    def test_monitor_agent_runs(self):
        msg = AgentMessage(
            session_id="test-001",
            location="Nairobi",
            lat=-1.2921,
            lon=36.8219,
            user_query="flood risk in nairobi",
            weather_data={"temperature": 19.5, "precipitation": 0.1},
        )
        agent = MonitorAgent()
        result = agent.run(msg)
        self.assertIsInstance(result, AgentMessage)
        self.assertIn("monitor_analysis", result.weather_data)

if __name__ == "__main__":
    unittest.main()