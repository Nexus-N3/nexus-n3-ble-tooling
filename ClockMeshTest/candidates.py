"""Shared pre-update candidate evaluation; never accepts a tracking candidate."""
from __future__ import annotations


CANDIDATE_FIELDS = [
    "candidate_predicted_reference_ns", "candidate_prediction_error_ns",
    "candidate_scale_ns_per_source_unit", "retained_tracking_observation_count",
]


def observe_with_candidate(model, source_time: int, reference_time_ns: int) -> dict:
    # Deliberately evaluate the previous fit before this pair can influence it.
    prediction = model.map_candidate(source_time)
    error = None if prediction is None else reference_time_ns - prediction
    model.observe(source_time=source_time, reference_time_ns=reference_time_ns)
    return {
        "candidate_predicted_reference_ns": prediction,
        "candidate_prediction_error_ns": error,
        "candidate_scale_ns_per_source_unit": model.candidate_scale_ns_per_source_unit,
        "retained_tracking_observation_count": model.retained_tracking_observation_count,
    }
