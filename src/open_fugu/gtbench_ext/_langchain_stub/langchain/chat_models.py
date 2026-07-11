"""Placeholder -- see _langchain_stub/__init__.py (one level up) for why
this exists. Never instantiated: this project's gtbench_ext/ always uses
LocalTransformersModel instead of GTBench's own LLMModel, so
gamingbench/chat/chat.py's chat_llm() (the only caller of these classes)
never actually runs."""


class ChatOpenAI:
    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "ChatOpenAI is a langchain import-time stub (see "
            "gtbench_ext/_langchain_stub) -- this project never calls "
            "GTBench's remote-API chat_llm() path, only LocalTransformersModel."
        )


class ChatAnyscale:
    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "ChatAnyscale is a langchain import-time stub (see "
            "gtbench_ext/_langchain_stub) -- this project never calls "
            "GTBench's remote-API chat_llm() path, only LocalTransformersModel."
        )
