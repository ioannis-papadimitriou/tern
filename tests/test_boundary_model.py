"""BoundaryModel: no input echo in validation errors, unknown fields rejected (plan 01 step 4)."""

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from tern._model import BoundaryModel

SENTINEL = "SENTINEL-x"


class Msg(BoundaryModel):
    count: int


class LeakyMsg(BaseModel):
    """Control: same shape without hide_input_in_errors."""

    model_config = ConfigDict(extra="forbid")
    count: int


def _err(model: type[BaseModel], data: dict[str, object]) -> ValidationError:
    with pytest.raises(ValidationError) as exc:
        model.model_validate(data)
    return exc.value


def test_control_plain_model_leaks_input() -> None:
    """The sentinel check below can fail: without the setting pydantic echoes the input."""
    err = _err(LeakyMsg, {"count": f"mail body {SENTINEL}"})
    assert SENTINEL in str(err)


def test_invalid_value_not_echoed() -> None:
    err = _err(Msg, {"count": f"mail body {SENTINEL}"})
    assert SENTINEL not in str(err)
    assert SENTINEL not in repr(err)


def test_unknown_field_rejected_and_not_echoed() -> None:
    err = _err(Msg, {"count": 1, "subject": f"Re: {SENTINEL}"})
    assert [e["type"] for e in err.errors()] == ["extra_forbidden"]
    assert SENTINEL not in str(err)


def test_errors_without_input_carry_no_mail_text() -> None:
    """`errors()` keeps the raw input by default: log it only with include_input=False."""
    err = _err(Msg, {"count": f"mail body {SENTINEL}"})
    assert SENTINEL not in str(err.errors(include_input=False))


def test_config_is_the_boundary_config() -> None:
    assert Msg.model_config.get("extra") == "forbid"
    assert Msg.model_config.get("hide_input_in_errors") is True
