from app.services.oidc_sso import build_signed_sso_state, verify_signed_sso_state


def test_sso_state_round_trip():
    state = build_signed_sso_state(provider_id=42, return_path="/dashboard")
    payload = verify_signed_sso_state(state)
    assert int(payload["provider_id"]) == 42
    assert payload["return_path"] == "/dashboard"
