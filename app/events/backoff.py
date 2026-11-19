import random


def backoff_ms(attempt: int, *, base_ms: int, cap_ms: int, rng: random.Random | None = None) -> int:
    """Exponential backoff with "equal jitter": half fixed, half random.

    attempt 1 -> [base/2, base], 2 -> [base, 2*base], ... capped at cap_ms. The fixed half
    guarantees real spacing between retries; the random half keeps many clients that
    failed at the same moment from retrying in lockstep (thundering herd).
    """
    if attempt < 1:
        raise ValueError("attempt starts at 1")
    ceiling = min(cap_ms, base_ms * 2 ** min(attempt - 1, 32))
    half = ceiling / 2
    return int(half + (rng or random).uniform(0, half))
