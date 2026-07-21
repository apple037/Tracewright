from collections import deque


class FakeModelGateway:
    def __init__(self, responses: dict[str, list[object]]):
        self.responses = {role: deque(values) for role, values in responses.items()}
        self.calls: list[str] = []
        self.requests: list[object] = []

    async def structured(self, role: str, request: object, response_type: type):
        self.calls.append(role)
        self.requests.append(request)
        return response_type.model_validate(self.responses[role].popleft())

    async def complete(self, role: str, request: object) -> str:
        self.calls.append(role)
        self.requests.append(request)
        return str(self.responses[role].popleft())
