"""Placeholder -- see ../langchain/chat_models.py (same reasoning; GTBench's
chat.py imports both langchain.chat_models and langchain_community.chat_models,
re-binding the same two names -- stub both directly, self-contained, rather
than cross-importing between the two stub roots)."""


class ChatOpenAI:
    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "ChatOpenAI is a langchain_community import-time stub (see "
            "gtbench_ext/_langchain_stub) -- this project never calls "
            "GTBench's remote-API chat_llm() path, only LocalTransformersModel."
        )


class ChatAnyscale:
    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "ChatAnyscale is a langchain_community import-time stub (see "
            "gtbench_ext/_langchain_stub) -- this project never calls "
            "GTBench's remote-API chat_llm() path, only LocalTransformersModel."
        )
