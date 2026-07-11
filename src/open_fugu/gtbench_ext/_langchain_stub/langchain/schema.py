"""Placeholder -- see _langchain_stub/__init__.py (one level up). Never
instantiated by this project (only by gamingbench/chat/chat.py's chat_llm(),
which this project never calls -- see chat_models.py in this same stub)."""


class SystemMessage:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs


class HumanMessage:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs


class AIMessage:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
