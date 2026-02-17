from dotenv import load_dotenv
load_dotenv()

import os
from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient

credential = DefaultAzureCredential()
project_client = AIProjectClient(
    endpoint=os.getenv('AZURE_AI_PROJECT_ENDPOINT'),
    credential=credential,
)

# List all deployments
connections = project_client.connections.list()
print("Available connections:")
for conn in connections:
    print(f"  - {conn.name}")