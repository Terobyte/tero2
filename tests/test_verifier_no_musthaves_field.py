"""VerifierResult must not have must_haves_failed field after R2 fix."""
from tero2.players.verifier import VerifierResult

def test_verifier_result_has_no_must_haves_failed():
    result = VerifierResult(success=True, verdict="PASS")
    assert not hasattr(result, "must_haves_failed"), (
        "must_haves_failed was removed in R2 fix — if this fails, the fix wasn't applied"
    )
