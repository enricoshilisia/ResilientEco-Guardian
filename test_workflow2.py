from dotenv import load_dotenv
load_dotenv()

import unittest
from guardian.agents.core_agents import run_all_agents

class TestFullWorkflow(unittest.TestCase):
    def test_run_all_agents(self):
        results = run_all_agents(
            user_query="flood risk in nairobi",
            lat=-1.2921,
            lon=36.8219,
            city_name="Nairobi",
        )
        self.assertIsInstance(results, dict)
        self.assertIn("monitor", results)
        self.assertIn("predict", results)
        self.assertIn("decision", results)
        self.assertIn("action", results)
        self.assertIn("governance", results)
        self.assertIn("session_id", results)

if __name__ == "__main__":
    unittest.main()