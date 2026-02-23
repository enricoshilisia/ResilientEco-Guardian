from dotenv import load_dotenv
load_dotenv()

import os
import unittest

class TestDeployments(unittest.TestCase):
    def test_azure_project_endpoint_configured(self):
        endpoint = os.getenv("AZURE_AI_PROJECT_ENDPOINT")
        self.assertIsNotNone(endpoint, "AZURE_AI_PROJECT_ENDPOINT must be set")
        self.assertTrue(endpoint.startswith("https://"), "Endpoint must be an HTTPS URL")

    def test_azure_openai_configured(self):
        self.assertIsNotNone(os.getenv("AZURE_OPENAI_ENDPOINT"), "AZURE_OPENAI_ENDPOINT must be set")
        self.assertIsNotNone(os.getenv("AZURE_OPENAI_KEY"), "AZURE_OPENAI_KEY must be set")

    def test_list_connections(self):
        endpoint = os.getenv("AZURE_AI_PROJECT_ENDPOINT")
        if not endpoint:
            self.skipTest("AZURE_AI_PROJECT_ENDPOINT not configured")

        from azure.identity import ClientSecretCredential, DefaultAzureCredential
        from azure.ai.projects import AIProjectClient

        tenant = os.getenv("AZURE_TENANT_ID")
        client_id = os.getenv("AZURE_CLIENT_ID")
        secret = os.getenv("AZURE_CLIENT_SECRET")

        if tenant and client_id and secret:
            cred = ClientSecretCredential(tenant, client_id, secret)
        else:
            cred = DefaultAzureCredential()

        project_client = AIProjectClient(endpoint=endpoint, credential=cred)
        connections = list(project_client.connections.list())
        self.assertIsInstance(connections, list)
        print(f"Found {len(connections)} connections:")
        for conn in connections:
            print(f"  - {conn.name}")

if __name__ == "__main__":
    unittest.main()