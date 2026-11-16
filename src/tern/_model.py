"""Base class for every pydantic model that crosses a boundary (bridge, tools, config)."""

from pydantic import BaseModel, ConfigDict


class BoundaryModel(BaseModel):
    """Strict boundary model.

    - ``extra="forbid"``: unknown fields are an error, never silently dropped.
    - ``hide_input_in_errors=True``: validation errors never echo the input, which may be
      mail content (CLAUDE.md conventions).
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
