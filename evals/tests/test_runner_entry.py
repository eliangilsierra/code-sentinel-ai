import pytest

from evals.runner.__main__ import NOT_IMPLEMENTED, main


def test_unimplemented_command_does_not_report_success() -> None:
    assert main(["judge", "--run", "x"]) == NOT_IMPLEMENTED


def test_unknown_command_is_rejected() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["frobnicate"])
    assert exc.value.code == 2
