import os

_LANGFUSE_ENABLED = bool(
    os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")
)


def get_langfuse_handler(project: str, task: str, run_id: str):
    """
    Returns a Langfuse callback handler.
    Returns None if Langfuse is not configured.
    Langfuse v3 reads LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST from env.
    """
    if not _LANGFUSE_ENABLED:
        return None

    from langfuse.langchain import CallbackHandler

    # Langfuse v4 only accepts public_key and trace_context.
    # All config (host, keys) is read from env vars.
    # Costs show up if the LLM provider (LiteLLM) returns usage metadata.
    return CallbackHandler()
