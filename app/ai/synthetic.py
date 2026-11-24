"""Deterministic synthetic transaction activity for training and evaluating the model.

SYNTHETIC by design: there is no real transaction data in this project, and nothing here
claims to resemble real fraud. The world runs on a COMPRESSED CLOCK (seconds, not days) so
the AI Lab can reproduce its scenarios live in a few seconds:

  normal accounts   created during the first 20 min; first transfer 1-120 s later; then one
                    transfer every 2 s + Exp(3-40 s); amounts log-normal around a per-account
                    typical amount (median ~$40 across accounts, +/-35 % per transfer)
  large             1.5 % of normal transfers replaced by 8-40x the account's usual amount
  burst             5 % of accounts fire 5-9 transfers 0.05-0.6 s apart; from the 3rd one on
                    they are labelled anomalous (the first two look like normal activity)
  new_account       4 % extra accounts whose FIRST transfer comes 0.2-4 s after creation
                    and is 10-40x the population's typical amount; afterwards they behave normally

Labels are the generator's intent ("normal", "large", "burst", "new_account").
The same seed always yields exactly the same rows.

Scenarios (Phase 6B) shift the world for robustness evaluation, never for training:
  baseline       the defaults above (the training world)
  amount_shift   every account's typical amount x3 (inflation / a richer population)
  fast_normal    normal accounts transact 2-4x faster (mean gap 1-12 s): stresses false
                 positives of velocity features
  slow_bursts    burst transfers 0.6-2.0 s apart: evades a fixed "< 1 s" rule
The baseline draws random numbers in exactly the original order, so the training data
(and the v1 model's reproduction) are unchanged.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np

from app.ai.features import compute_features, prior_from_history

DEFAULT_SEED = 20261120
T0 = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str = "baseline"
    population_median: float = 4_000.0  # cents
    mean_gap_range: tuple[float, float] = (3.0, 40.0)
    burst_gap_range: tuple[float, float] = (0.05, 0.6)
    description: str = field(default="the training world", compare=False)


SCENARIOS: dict[str, Scenario] = {
    "baseline": Scenario(),
    "amount_shift": Scenario(
        name="amount_shift",
        population_median=12_000.0,
        description="every typical amount x3",
    ),
    "fast_normal": Scenario(
        name="fast_normal",
        mean_gap_range=(1.0, 12.0),
        description="normal accounts 2-4x faster",
    ),
    "slow_bursts": Scenario(
        name="slow_bursts",
        burst_gap_range=(0.6, 2.0),
        description="bursts 0.6-2.0 s apart",
    ),
}


@dataclass(frozen=True, slots=True)
class SyntheticTransfer:
    account: int
    at: datetime
    amount_minor: int
    label: str  # normal | large | burst | new_account


@dataclass(frozen=True, slots=True)
class Row:
    account: int
    at: datetime
    amount_minor: int
    label: str
    features: dict[str, float]

    @property
    def anomalous(self) -> bool:
        return self.label != "normal"


def generate(
    seed: int = DEFAULT_SEED,
    *,
    accounts: int = 1200,
    horizon_s: float = 1800.0,
    scenario: Scenario = SCENARIOS["baseline"],
) -> tuple[list[SyntheticTransfer], dict[int, datetime]]:
    rng = np.random.default_rng(seed)
    transfers: list[SyntheticTransfer] = []
    created: dict[int, datetime] = {}
    population_median = scenario.population_median
    n_new = round(accounts * 0.04)
    for account in range(accounts + n_new):
        is_new_anomaly = account >= accounts
        start = float(rng.uniform(0, 1200))
        created[account] = T0 + timedelta(seconds=start)
        typical = float(np.exp(rng.normal(np.log(population_median), 0.8)))
        mean_gap = float(rng.uniform(*scenario.mean_gap_range))
        t = start
        if is_new_anomaly:
            t += float(rng.uniform(0.2, 4.0))
            amount = population_median * float(rng.uniform(10, 40))
            transfers.append(_transfer(account, t, amount, "new_account"))
            t += 2 + float(rng.exponential(mean_gap))
        else:
            t += float(rng.uniform(1, 120))
        while t < horizon_s + start:
            amount = typical * float(np.exp(rng.normal(0, 0.35)))
            label = "normal"
            if not is_new_anomaly and rng.random() < 0.015 and _count(transfers, account) >= 3:
                amount = typical * float(rng.uniform(8, 40))
                label = "large"
            transfers.append(_transfer(account, t, amount, label))
            t += 2 + float(rng.exponential(mean_gap))
        if not is_new_anomaly and rng.random() < 0.05:
            burst_at = start + float(rng.uniform(300, horizon_s))
            size = int(rng.integers(5, 10))
            bt = burst_at
            for i in range(size):
                amount = typical * float(np.exp(rng.normal(0, 0.35)))
                transfers.append(_transfer(account, bt, amount, "burst" if i >= 2 else "normal"))
                bt += float(rng.uniform(*scenario.burst_gap_range))
    transfers.sort(key=lambda x: (x.at, x.account))
    return transfers, created


def _transfer(account: int, t: float, amount: float, label: str) -> SyntheticTransfer:
    return SyntheticTransfer(
        account=account,
        at=T0 + timedelta(seconds=round(t, 3)),
        amount_minor=max(100, round(amount)),
        label=label,
    )


def _count(transfers: list[SyntheticTransfer], account: int) -> int:
    # Only used while generating one account, whose transfers are at the end of the list.
    n = 0
    for x in reversed(transfers):
        if x.account != account:
            break
        n += 1
    return n


def featurize(transfers: list[SyntheticTransfer], created: dict[int, datetime]) -> list[Row]:
    """Replay the stream in time order; each row sees only its account's EARLIER transfers."""
    history: dict[int, list[tuple[datetime, int]]] = {}
    rows = []
    for x in transfers:
        past = history.setdefault(x.account, [])
        prior = prior_from_history(past, x.at)
        features = compute_features(
            amount_minor=x.amount_minor, at=x.at, account_created_at=created[x.account], prior=prior
        )
        rows.append(Row(x.account, x.at, x.amount_minor, x.label, features))
        past.append((x.at, x.amount_minor))
    return rows


def split_by_account(
    rows: list[Row], seed: int = DEFAULT_SEED
) -> tuple[list[Row], list[Row], list[Row]]:
    """60/20/20 by ACCOUNT, so no account's history appears in two splits."""
    accounts = sorted({r.account for r in rows})
    order = np.random.default_rng(seed + 1).permutation(len(accounts))
    n = len(accounts)
    train_ids = {accounts[i] for i in order[: int(n * 0.6)]}
    val_ids = {accounts[i] for i in order[int(n * 0.6) : int(n * 0.8)]}
    train = [r for r in rows if r.account in train_ids]
    val = [r for r in rows if r.account in val_ids]
    test = [r for r in rows if r.account not in train_ids and r.account not in val_ids]
    return train, val, test
