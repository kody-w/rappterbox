class BasicAgent:
    def __init__(self, name=None, metadata=None):
        if name is not None: self.name = name
        elif not hasattr(self, 'name'): self.name = 'BasicAgent'
        if metadata is not None: self.metadata = metadata
        elif not hasattr(self, 'metadata'):
            self.metadata = {'name': self.name, 'description': '', 'parameters': {'type': 'object', 'properties': {}}}
    def perform(self, **kwargs):
        return 'Not implemented.'
    def system_context(self):
        return None
    def to_tool(self):
        return {'type': 'function', 'function': {
            'name': self.name,
            'description': self.metadata.get('description', ''),
            'parameters': self.metadata.get('parameters', {'type': 'object', 'properties': {}})}}
