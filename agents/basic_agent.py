import os


class BasicAgent:
    """Base class for all RAPP Brainstem agents. Extend this in your private agent files."""

    @staticmethod
    def artifact_path(filename):
        """Return an absolute path under ~/.brainstem/artifacts/ for writing a
        user-visible artifact (HTML deck, report, export, etc.). The directory
        is created if missing, and is allow-listed by the brainstem's /open
        endpoint — so any absolute path returned here is openable from chat.
        Agents should return this path in their JSON response so the chat
        autolinker can surface it as a clickable link."""
        root = os.path.join(os.path.expanduser("~"), ".brainstem", "artifacts")
        os.makedirs(root, exist_ok=True)
        safe = os.path.basename(filename) or "artifact"
        return os.path.join(root, safe)

    def __init__(self, name=None, metadata=None):
        if name is not None:
            self.name = name
        elif not hasattr(self, "name"):
            self.name = "BasicAgent"
        if metadata is not None:
            self.metadata = metadata
        elif not hasattr(self, "metadata"):
            self.metadata = {
                "name": self.name,
                "description": "Base agent -- override this.",
                "parameters": {"type": "object", "properties": {}, "required": []}
            }

    def perform(self, **kwargs):
        return "Not implemented."

    def system_context(self):
        """Optional: return a string to inject into the system prompt each turn.
        Override in agents that provide persistent context (e.g. memory)."""
        return None

    def to_tool(self):
        """Returns OpenAI function-calling tool definition."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.metadata.get("description", ""),
                "parameters": self.metadata.get("parameters", {"type": "object", "properties": {}})
            }
        }
