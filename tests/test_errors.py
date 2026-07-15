from copperbrain.errors import CopperbrainError
from copperbrain.models import ErrorCode


def test_error_exposes_stable_structured_payload() -> None:
    error = CopperbrainError(
        ErrorCode.NOT_FOUND,
        "missing",
        actionable_hint="choose another path",
        details={"path": "x"},
    )
    assert str(error) == "missing"
    assert error.error.code is ErrorCode.NOT_FOUND
    assert error.error.details == {"path": "x"}
