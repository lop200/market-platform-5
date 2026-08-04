from app.markets.data_state import DataState, resolve_data_state


def test_data_state_precedence_is_consistent():
    assert resolve_data_state(primary_available=False, primary_fresh=False) is DataState.NO_DATA
    assert resolve_data_state(primary_available=True, primary_fresh=False) is DataState.STALE
    assert resolve_data_state(primary_available=True, primary_fresh=True, blocked=True) is DataState.BLOCKED
    assert resolve_data_state(
        primary_available=True, primary_fresh=True, validator_status="stale"
    ) is DataState.VALIDATION_WARNING
    assert resolve_data_state(
        primary_available=True, primary_fresh=True, validator_status="validation_warning"
    ) is DataState.VALIDATION_WARNING
    assert resolve_data_state(primary_available=True, primary_fresh=True) is DataState.LIVE
