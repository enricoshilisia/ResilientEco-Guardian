from dotenv import load_dotenv
load_dotenv()

import unittest
from guardian.agents.core_agents import MonitorAgent, AgentMessage

class TestMonitor(unittest.TestCase):
    def test_monitor_returns_agent_message(self):
        msg = AgentMessage(
            session_id="test-002",
            location="Nairobi",
            lat=-1.2921,
            lon=36.8219,
            user_query="is it raining in nairobi",
            weather_data={},
        )
        agent = MonitorAgent()
        result = agent.run(msg)
        self.assertIsInstance(result, AgentMessage)
        self.assertTrue(len(result.agent_chain) > 0)
        self.assertEqual(result.agent_chain[0]["agent"], "monitor")

if __name__ == "__main__":
    unittest.main()