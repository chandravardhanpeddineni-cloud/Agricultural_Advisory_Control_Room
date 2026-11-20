import logging

from app.core.config import Settings

logger = logging.getLogger(__name__)


def create_framework_agent(settings: Settings, *, name: str, instructions: str):
    """Create a real Microsoft Agent Framework agent when a model provider is configured."""
    if not settings.llm_configured:
        return None
    from agent_framework import Agent
    from agent_framework.openai import OpenAIChatClient, OpenAIChatCompletionClient

    client_args = {
        "model": settings.llm_model_id,
        "api_key": settings.llm_api_key.get_secret_value(),
    }
    if settings.llm_provider in {"openai_compatible", "groq"}:
        base_url = settings.llm_base_url
        if settings.llm_provider == "groq":
            base_url = base_url or "https://api.groq.com/openai/v1"
        if not base_url:
            raise RuntimeError("LLM_BASE_URL is required for an OpenAI-compatible provider")
        client_args["base_url"] = base_url
    elif settings.llm_provider == "azure_openai":
        if not settings.azure_openai_endpoint:
            raise RuntimeError("AZURE_OPENAI_ENDPOINT is required for Azure OpenAI")
        client_args["azure_endpoint"] = settings.azure_openai_endpoint
        if settings.azure_openai_api_version:
            client_args["api_version"] = settings.azure_openai_api_version
    # Groq's Responses endpoint rejects an `include` option sent by the
    # Responses client; its Chat Completions endpoint supports this model and
    # structured JSON schema outputs.
    chat_client = (
        OpenAIChatCompletionClient if settings.llm_provider == "groq" else OpenAIChatClient
    )
    client = chat_client(**client_args)
    return Agent(
        client=client,
        name=name,
        instructions=instructions,
        default_options={"temperature": 0.1, "max_tokens": 4096},
    )


async def run_structured(agent, prompt: str, response_model):
    if agent is None:
        raise RuntimeError(
            "LLM provider is not configured; set the server-side LLM environment variables"
        )
    options = {"response_format": response_model}
    try:
        response = await agent.run(prompt, options=options)
    except Exception as exc:
        message = str(exc).casefold()
        if "max_completion_tokens" not in message and "output was truncated" not in message:
            raise
        logger.warning(
            "Structured response was truncated; retrying once with a larger token limit"
        )
        response = await agent.run(prompt, options={**options, "max_tokens": 8192})
    value = getattr(response, "value", None)
    if isinstance(value, response_model):
        return value
    if value is not None:
        return response_model.model_validate(value)
    raise ValueError("The model did not return a structured response matching the required schema")
