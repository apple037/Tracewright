from dataclasses import dataclass


@dataclass(eq=False, frozen=True)
class AgentError(Exception):
    error_code: str
    category: str
    retryable: bool = False
    failure_stage: str | None = None
    component: str | None = None
    operation: str | None = None
    field_path: str | None = None
    public_message: str = "The request could not be completed."

    def __post_init__(self) -> None:
        Exception.__init__(self, self.public_message)

    @classmethod
    def auth(cls, error_code: str, **details: object) -> "AgentError":
        return cls(
            error_code=error_code,
            category="authorization",
            public_message="The requested resource is not available.",
            **details,
        )

    @classmethod
    def validation(cls, error_code: str, **details: object) -> "AgentError":
        return cls(error_code=error_code, category="validation", **details)

    @classmethod
    def dependency(cls, error_code: str, **details: object) -> "AgentError":
        return cls(error_code=error_code, category="dependency", **details)
