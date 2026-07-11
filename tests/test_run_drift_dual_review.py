from scripts.run_drift_dual_review import _aggregate_exit_code


def test_degraded_only_result_uses_dedicated_exit_code() -> None:
    assert _aggregate_exit_code([0, 3, 3]) == 3


def test_hard_failure_is_not_masked_by_degraded_result() -> None:
    assert _aggregate_exit_code([3, 2, 0]) == 2


def test_largest_hard_failure_is_preserved() -> None:
    assert _aggregate_exit_code([1, 3, 2]) == 2
