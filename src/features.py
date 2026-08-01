"""Feature construction and temporal splitting for the cancellation model.

Every rule in this module is a conclusion reached in `notebooks/01_data_audit.ipynb`.
Nothing here is decided; it is only enforced. If you disagree with a choice, the
argument for it is in that notebook, not in this file.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

MONTHS = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"])}

TARGET = "is_canceled"

#: Columns that cannot be used, with the reason. See notebook 01, sections 2 and 3.
LEAKY_COLUMNS = {
    "reservation_status":          "encodes the target exactly (100% separation)",
    "reservation_status_date":     "dated after the outcome",
    "required_car_parking_spaces": "zero cancellations among 7,416 bookings — recorded on arrival",
    "assigned_room_type":          "rooms are assigned at check-in",
    "booking_changes":             "accumulates over the life of the booking",
}

#: `arrival_date_year` is deliberately absent. It raises test AUC by ~0.017 because the
#: model can learn "2017 cancels more", but a model that depends on the calendar year
#: cannot be applied to a year it has never seen. The upward drift is handled by
#: recalibration instead, which does generalise forward.
NUMERIC_FEATURES = [
    "lead_time", "month_num", "arrival_dayofweek", "arrival_date_week_number",
    "arrival_date_day_of_month", "total_nights", "stays_in_weekend_nights",
    "stays_in_week_nights", "total_guests", "adults", "children", "babies",
    "is_repeated_guest", "previous_cancellations", "previous_bookings_not_canceled",
    "previous_cancel_ratio", "days_in_waiting_list", "adr", "adr_per_guest",
    "total_of_special_requests",
]

CATEGORICAL_FEATURES = [
    "hotel", "meal", "country", "market_segment", "distribution_channel",
    "reserved_room_type", "deposit_type", "customer_type", "agent", "company",
]

DEFAULT_DATA = Path(__file__).resolve().parents[1] / "data" / "hotel_bookings.csv.gz"


def load_bookings(path: Path | str = DEFAULT_DATA) -> pd.DataFrame:
    """Read the raw export and attach the arrival date.

    Duplicate rows are kept — see notebook 01, section 5.
    """
    df = pd.read_csv(path)
    df["arrival_date"] = pd.to_datetime(dict(
        year=df["arrival_date_year"],
        month=df["arrival_date_month"].map(MONTHS),
        day=df["arrival_date_day_of_month"]))
    return df


def add_derived_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Add the derived columns the model uses. Every one is computable at booking time."""
    out = df.copy()
    out["total_nights"] = out["stays_in_weekend_nights"] + out["stays_in_week_nights"]
    out["total_guests"] = out["adults"] + out["children"].fillna(0) + out["babies"]
    out["month_num"] = out["arrival_date_month"].map(MONTHS)
    out["arrival_dayofweek"] = out["arrival_date"].dt.dayofweek

    # Rate per head rather than per room: a 200 EUR double is not a 200 EUR single.
    out["adr_per_guest"] = out["adr"] / out["total_guests"].replace(0, np.nan)

    # Share of a returning guest's history that ended in cancellation. -1 marks
    # "no history", which is a different statement from "history, all of it clean".
    prior = out["previous_cancellations"] + out["previous_bookings_not_canceled"]
    out["previous_cancel_ratio"] = (
        out["previous_cancellations"] / prior.replace(0, np.nan)).fillna(-1)
    return out


def collapse_rare(s: pd.Series, min_count: int = 100, other: str = "OTHER") -> pd.Series:
    """Fold categories seen fewer than `min_count` times into a single bucket.

    `agent` and `company` have several hundred levels each, most of them appearing a
    handful of times. Left alone they exceed the 255-category ceiling of
    HistGradientBoostingClassifier, and the rare levels carry no reliable signal.
    """
    counts = s.value_counts()
    return s.where(s.isin(set(counts[counts >= min_count].index)), other)


def build_feature_frame(df: pd.DataFrame, min_category_count: int = 100) -> pd.DataFrame:
    """Return the model matrix: derived columns added, leaky columns never included."""
    out = add_derived_columns(df)
    X = out[NUMERIC_FEATURES + CATEGORICAL_FEATURES].copy()

    X["children"] = X["children"].fillna(0)

    # A missing agent or company is not a missing value: it means the booking came
    # directly. Encoded as its own level rather than imputed.
    for col in ("agent", "company"):
        X[col] = collapse_rare(
            X[col].fillna(-1).astype(int).astype(str), min_category_count)
    X["country"] = collapse_rare(X["country"].fillna("UNK"), min_category_count)
    return X


def temporal_split(df: pd.DataFrame,
                   calibration_start: str = "2017-01-01",
                   test_start: str = "2017-04-01") -> dict[str, np.ndarray]:
    """Split on arrival date into train / calibration / test.

    Splitting on arrival date rather than at random does two things at once. It matches
    the operational question — predict a future arrival night from past bookings — and it
    keeps the 31,994 duplicate rows on one side of each boundary, since identical rows
    share an arrival date by construction.
    """
    cal_start = pd.Timestamp(calibration_start)
    test_start_ts = pd.Timestamp(test_start)
    arrival = df["arrival_date"]
    return {
        "train":       np.where(arrival < cal_start)[0],
        "calibration": np.where((arrival >= cal_start) & (arrival < test_start_ts))[0],
        "test":        np.where(arrival >= test_start_ts)[0],
    }


def split_summary(df: pd.DataFrame, splits: dict[str, np.ndarray]) -> pd.DataFrame:
    """One row per split: date range, size, cancellation rate."""
    rows = []
    for name, idx in splits.items():
        part = df.iloc[idx]
        rows.append({
            "split": name,
            "from": part["arrival_date"].min().date(),
            "to": part["arrival_date"].max().date(),
            "bookings": len(idx),
            "cancel_rate": round(part[TARGET].mean(), 4),
        })
    return pd.DataFrame(rows).set_index("split")
