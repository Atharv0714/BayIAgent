from typing import Any, Protocol, runtime_checkable

from sf_agent.types import ToolResult


@runtime_checkable
class Tool(Protocol):
    """The contract the chunk-2 agent loop depends on.

    The loop only ever sees `name`, `description`, `input_schema` (to advertise the
    tool to Claude) and `run(**kwargs)`. Because run_sql and a future Cortex Analyst
    tool both satisfy this, swapping them needs no change to the loop's dispatch.
    """

    name: str
    description: str
    input_schema: dict[str, Any]

    def run(self, **kwargs: Any) -> ToolResult: ...
