import os

import uvicorn

from valueroute.api.app import create_app
from valueroute.providers.openai import OpenAIProviderAdapter


def _provider_from_environment():
    model_id = os.getenv("VALUEROUTE_OPENAI_MODEL_ID")
    if not model_id:
        return None
    return OpenAIProviderAdapter(
        model_id=model_id,
        provider_id=os.getenv("VALUEROUTE_PROVIDER_ID", "openai"),
        base_url=os.getenv("VALUEROUTE_OPENAI_BASE_URL", "https://api.openai.com/v1"),
    )


app = create_app(provider=_provider_from_environment())


def run() -> None:
    uvicorn.run("valueroute.main:app", host="127.0.0.1", port=8787)
