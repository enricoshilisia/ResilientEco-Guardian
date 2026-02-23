"""
ResilientEco Guardian - Azure AI Foundry Client
Uses azure-ai-projects 2.0.0b3 SDK — the official Microsoft Foundry entry point.
Falls back to direct Azure OpenAI if Foundry auth fails.
"""

import os
import time
import logging

logger = logging.getLogger(__name__)


def _clean_endpoint(endpoint: str) -> str:
    """Strip /openai/v1 or /openai suffixes — AzureOpenAI SDK adds these itself."""
    endpoint = endpoint.rstrip("/")
    for suffix in ("/openai/v1", "/openai"):
        if endpoint.endswith(suffix):
            endpoint = endpoint[: -len(suffix)]
    return endpoint


def _make_client():
    """
    Build the best available OpenAI-compatible client. Priority:
      1. Azure AI Foundry SDK  (AIProjectClient.get_openai_client)  ← real Foundry
      2. Azure OpenAI direct   (AzureOpenAI with key)
      3. Standard OpenAI       (OpenAI with OPENAI_API_KEY)
    Env vars are read here, at call time, so load_dotenv() always runs first.
    """

    # ── 1. Azure AI Foundry SDK ──────────────────────────────────────────────
    project_endpoint = os.getenv("AZURE_AI_PROJECT_ENDPOINT", "").strip()
    if project_endpoint:
        try:
            from azure.ai.projects import AIProjectClient
            from azure.identity import DefaultAzureCredential, ClientSecretCredential

            tenant    = os.getenv("AZURE_TENANT_ID", "").strip()
            client_id = os.getenv("AZURE_CLIENT_ID", "").strip()
            secret    = os.getenv("AZURE_CLIENT_SECRET", "").strip()

            if tenant and client_id and secret:
                cred = ClientSecretCredential(tenant, client_id, secret)
            else:
                # DefaultAzureCredential works when logged in via `az login`
                cred = DefaultAzureCredential()

            project_client = AIProjectClient(
                endpoint=project_endpoint,
                credential=cred,
            )

            # v2.0.0b3: let the SDK set its own api_version (2025-05-15-preview)
            openai_client = project_client.get_openai_client()
            logger.info("✅ Azure AI Foundry SDK — AIProjectClient.get_openai_client()")
            return openai_client, "azure_foundry"

        except Exception as e:
            logger.warning(f"Foundry SDK failed: {e} — falling back to Azure OpenAI direct")

    # ── 2. Azure OpenAI direct ───────────────────────────────────────────────
    azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip()
    azure_key      = os.getenv("AZURE_OPENAI_KEY", "").strip()

    if azure_endpoint and azure_key:
        from openai import AzureOpenAI
        clean  = _clean_endpoint(azure_endpoint)
        client = AzureOpenAI(
            azure_endpoint=clean,
            api_key=azure_key,
            api_version="2024-08-01-preview",
        )
        logger.info(f"✅ Azure OpenAI direct → {clean}")
        return client, "azure_openai"

    # ── 3. Standard OpenAI ───────────────────────────────────────────────────
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if openai_key:
        from openai import OpenAI
        client = OpenAI(api_key=openai_key)
        logger.info("✅ OpenAI direct")
        return client, "openai_direct"

    raise RuntimeError(
        "No AI client available. Set AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_KEY "
        "(or AZURE_AI_PROJECT_ENDPOINT with az login) in your .env file."
    )


class FoundryClient:
    """
    Singleton wrapper. Client is built on first .complete() call (not import time)
    so .env variables are always loaded before we try to authenticate.
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._client = None
            cls._instance._source = None
        return cls._instance

    def _ensure_client(self):
        if self._client is None:
            self._client, self._source = _make_client()

    def complete(
        self,
        agent_type: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.4,
        max_tokens: int = 1024,
    ) -> dict:
        self._ensure_client()

        deployment = (
            os.getenv("FOUNDRY_DEPLOYMENT")
            or os.getenv("AZURE_OPENAI_DEPLOYMENT")
            or "gpt-4o-mini"
        )

        start    = time.time()
        response = self._client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        latency_ms = int((time.time() - start) * 1000)
        text       = response.choices[0].message.content

        logger.info(
            f"[Foundry] agent={agent_type} model={deployment} "
            f"source={self._source} latency={latency_ms}ms "
            f"tokens={getattr(response.usage, 'total_tokens', '?')}"
        )

        return {
            "text":       text,
            "model":      deployment,
            "latency_ms": latency_ms,
            "source":     self._source,
            "tokens_used":getattr(response.usage, "total_tokens", 0),
            "agent_type": agent_type,
        }


# Lazy singleton — initialized on first .complete() call, never at import time
foundry = FoundryClient()