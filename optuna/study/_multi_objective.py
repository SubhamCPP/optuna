from __future__ import annotations

from collections.abc import Sequence

import numpy as np

import optuna
from optuna.study._study_direction import StudyDirection
from optuna.trial import FrozenTrial
from optuna.trial import TrialState


_CONSTRAINTS_KEY = "constraints"


def _get_feasible_trials(trials: Sequence[FrozenTrial]) -> list[FrozenTrial]:
    feasible_trials = []
    for trial in trials:
        constraints = trial.system_attrs.get(_CONSTRAINTS_KEY)
        if constraints is None or all([x <= 0.0 for x in constraints]):
            feasible_trials.append(trial)
    return feasible_trials


def _get_pareto_front_trials_2d(
    trials: Sequence[FrozenTrial],
    directions: Sequence[StudyDirection],
    consider_constraint: bool = False,
) -> list[FrozenTrial]:
    trials = [t for t in trials if t.state == TrialState.COMPLETE]
    if consider_constraint:
        trials = _get_feasible_trials(trials)

    n_trials = len(trials)
    if n_trials == 0:
        return []

    trials.sort(
        key=lambda trial: (
            _normalize_value(trial.values[0], directions[0]),
            _normalize_value(trial.values[1], directions[1]),
        ),
    )

    last_nondominated_trial = trials[0]
    pareto_front = [last_nondominated_trial]
    for i in range(1, n_trials):
        trial = trials[i]
        if _dominates(last_nondominated_trial, trial, directions):
            continue
        pareto_front.append(trial)
        last_nondominated_trial = trial

    pareto_front.sort(key=lambda trial: trial.number)
    return pareto_front


def _get_pareto_front_trials_nd(
    trials: Sequence[FrozenTrial],
    directions: Sequence[StudyDirection],
    consider_constraint: bool = False,
) -> list[FrozenTrial]:
    pareto_front = []
    trials = [t for t in trials if t.state == TrialState.COMPLETE]
    if consider_constraint:
        trials = _get_feasible_trials(trials)

    # TODO(vincent): Optimize (use the fast non dominated sort defined in the NSGA-II paper).
    for trial in trials:
        dominated = False
        for other in trials:
            if _dominates(other, trial, directions):
                dominated = True
                break

        if not dominated:
            pareto_front.append(trial)

    return pareto_front


def _get_pareto_front_trials_by_trials(
    trials: Sequence[FrozenTrial],
    directions: Sequence[StudyDirection],
    consider_constraint: bool = False,
) -> list[FrozenTrial]:
    if len(directions) == 2:
        return _get_pareto_front_trials_2d(
            trials, directions, consider_constraint
        )  # Log-linear in number of trials.
    return _get_pareto_front_trials_nd(
        trials, directions, consider_constraint
    )  # Quadratic in number of trials.


def _get_pareto_front_trials(
    study: "optuna.study.Study", consider_constraint: bool = False
) -> list[FrozenTrial]:
    return _get_pareto_front_trials_by_trials(study.trials, study.directions, consider_constraint)


def _fast_non_dominated_sort(
    loss_values: np.ndarray,
    *,
    penalty: np.ndarray | None = None,
    n_below: int | None = None,
) -> np.ndarray:
    """Perform the fast non-dominated sort algorithm.

    The fast non-dominated sort algorithm assigns a rank to each trial based on the dominance
    relationship of the trials, determined by the objective values and the penalty values. The
    algorithm is based on `the constrained NSGA-II algorithm
    <https://doi.org/10.1109/4235.99601>`_, but the handling of the case when penalty
    values are None is different. The algorithm assigns the rank according to the following
    rules:

    1. Feasible trials: First, the algorithm assigns the rank to feasible trials, whose penalty
        values are less than or equal to 0, according to unconstrained version of fast non-
        dominated sort.
    2. Infeasible trials: Next, the algorithm assigns the rank from the minimum penalty value of to
        the maximum penalty value.
    3. Trials with no penalty information (constraints value is None): Finally, The algorithm
        assigns the rank to trials with no penalty information according to unconstrained version
        of fast non-dominated sort. Note that only this step is different from the original
        constrained NSGA-II algorithm.
    Plus, the algorithm terminates whenever the number of sorted trials reaches n_below.

    Args:
        loss_values:
            Objective values, which is better when it is lower, of each trials.
        penalty:
            Constraints values of each trials. Defaults to None.
        n_below: The minimum number of top trials required to be sorted. The algorithm will
            terminate when the number of sorted trials reaches n_below. Defaults to None.

    Returns:
        An ndarray in the shape of (n_trials,), where each element is the non-dominated rank of
        each trial. The rank is 0-indexed and rank -1 means that the algorithm terminated before
        the trial was sorted.
    """
    if penalty is None:
        return _calculate_nondomination_rank(loss_values, n_below=n_below)

    if len(penalty) != len(loss_values):
        raise ValueError(
            "The length of penalty and loss_values must be same, but got "
            f"len(penalty)={len(penalty)} and len(loss_values)={len(loss_values)}."
        )

    nondomination_rank = np.full(len(loss_values), -1, dtype=int)
    is_penalty_nan = np.isnan(penalty)
    n_below = n_below or len(loss_values)

    # First, we calculate the domination rank for feasible trials.
    is_feasible = np.logical_and(~is_penalty_nan, penalty <= 0)
    nondomination_rank[is_feasible] = _calculate_nondomination_rank(
        loss_values[is_feasible], n_below=n_below
    )
    n_below -= np.count_nonzero(is_feasible)

    # Second, we calculate the domination rank for infeasible trials.
    top_rank_in_infeasible = np.max(nondomination_rank[is_feasible], initial=-1) + 1
    is_infeasible = np.logical_and(~is_penalty_nan, penalty > 0)
    n_infeasible = np.count_nonzero(is_infeasible)
    if is_infeasible > 0:
        _, ranks_in_infeas = np.unique(penalty[is_infeasible], return_inverse=True)
        nondomination_rank[is_infeasible] = ranks_in_infeas + top_rank_in_infeasible
        n_below -= n_infeasible

    # Third, we calculate the domination rank for trials with no penalty information.
    top_rank_in_penalty_nan = np.max(nondomination_rank[~is_penalty_nan], initial=-1) + 1
    ranks_in_penalty_nan = _calculate_nondomination_rank(
        loss_values[is_penalty_nan], n_below=n_below
    )
    nondomination_rank[is_penalty_nan] = ranks_in_penalty_nan + top_rank_in_penalty_nan

    return nondomination_rank


def _is_pareto_front_nd(unique_lexsorted_loss_values: np.ndarray) -> np.ndarray:
    loss_values = unique_lexsorted_loss_values.copy()
    n_trials = loss_values.shape[0]
    on_front = np.zeros(n_trials, dtype=bool)
    nondominated_indices = np.arange(n_trials)
    while len(loss_values):
        nondominated_and_not_top = np.any(loss_values < loss_values[0], axis=1)
        # NOTE: trials[j] cannot dominate trials[j] for i < j because of lexsort.
        # Therefore, nondominated_indices[0] is always non-dominated.
        on_front[nondominated_indices[0]] = True
        loss_values = loss_values[nondominated_and_not_top]
        nondominated_indices = nondominated_indices[nondominated_and_not_top]

    return on_front


def _is_pareto_front_2d(unique_lexsorted_loss_values: np.ndarray) -> np.ndarray:
    n_trials = unique_lexsorted_loss_values.shape[0]
    cummin_value1 = np.minimum.accumulate(unique_lexsorted_loss_values[:, 1])
    is_value1_min = cummin_value1 == unique_lexsorted_loss_values[:, 1]
    is_value1_new_min = cummin_value1[1:] < cummin_value1[:-1]
    on_front = np.ones(n_trials, dtype=bool)
    on_front[1:] = is_value1_min[1:] & is_value1_new_min
    return on_front


def _is_pareto_front(unique_lexsorted_loss_values: np.ndarray) -> np.ndarray:
    (n_trials, n_objectives) = unique_lexsorted_loss_values.shape
    if n_objectives == 1:
        return unique_lexsorted_loss_values[:, 0] == unique_lexsorted_loss_values[0]
    elif n_objectives == 2:
        return _is_pareto_front_2d(unique_lexsorted_loss_values)
    else:
        return _is_pareto_front_nd(unique_lexsorted_loss_values)


def _calculate_nondomination_rank(
    loss_values: np.ndarray, *, n_below: int | None = None
) -> np.ndarray:
    if n_below is not None and n_below <= 0:
        return np.zeros(len(loss_values), dtype=int)

    # Normalize n_below.
    n_below = n_below or len(loss_values)
    n_below = min(n_below, len(loss_values))

    (n_trials, n_objectives) = loss_values.shape
    if n_objectives == 1:
        _, ranks = np.unique(loss_values[:, 0], return_inverse=True)
        return ranks
    else:
        # It ensures that trials[j] will not dominate trials[i] for i < j.
        # np.unique does lexsort.
        unique_lexsorted_loss_values, order_inv = np.unique(
            loss_values, return_inverse=True, axis=0
        )

    n_unique = unique_lexsorted_loss_values.shape[0]
    ranks = np.zeros(n_unique, dtype=int)
    rank = 0
    indices = np.arange(n_unique)
    while n_unique - indices.size < n_below:
        on_front = _is_pareto_front(unique_lexsorted_loss_values)
        ranks[indices[on_front]] = rank
        # Remove the recent Pareto solutions.
        indices = indices[~on_front]
        unique_lexsorted_loss_values = unique_lexsorted_loss_values[~on_front]
        rank += 1

    ranks[indices] = rank  # Rank worse than the top n_below is defined as the worst rank.
    return ranks[order_inv]


def _dominates(
    trial0: FrozenTrial, trial1: FrozenTrial, directions: Sequence[StudyDirection]
) -> bool:
    values0 = trial0.values
    values1 = trial1.values

    if trial0.state != TrialState.COMPLETE:
        return False

    if trial1.state != TrialState.COMPLETE:
        return True

    assert values0 is not None
    assert values1 is not None

    if len(values0) != len(values1):
        raise ValueError("Trials with different numbers of objectives cannot be compared.")

    if len(values0) != len(directions):
        raise ValueError(
            "The number of the values and the number of the objectives are mismatched."
        )

    normalized_values0 = [_normalize_value(v, d) for v, d in zip(values0, directions)]
    normalized_values1 = [_normalize_value(v, d) for v, d in zip(values1, directions)]

    if normalized_values0 == normalized_values1:
        return False

    return all(v0 <= v1 for v0, v1 in zip(normalized_values0, normalized_values1))


def _normalize_value(value: None | float, direction: StudyDirection) -> float:
    if value is None:
        value = float("inf")

    if direction is StudyDirection.MAXIMIZE:
        value = -value

    return value
