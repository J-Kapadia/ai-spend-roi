"""The dose-response curve: fit, identification, marginal return and its standard error."""

import math

import duckdb
import numpy as np
import pytest
from scipy import optimize

from roi import curve

A_TRUE, K_TRUE = 0.30, 5000.0
X = np.array([1500.0, 3000.0, 4500.0, 5000.0, 5000.0, 5000.0])


def truth(x):
    return A_TRUE * (1.0 - np.exp(-x / K_TRUE))


def shared_base_cov(n, var=1e-4):
    """Cells that share a base month: variance 2 var, covariance var."""
    return var * (np.eye(n) + np.ones((n, n)))


def reference_gls(x, y, cov):
    """Independent GLS by least squares on Cholesky-whitened residuals, from several starts."""
    chol = np.linalg.cholesky(np.linalg.inv(cov))

    def resid(theta):
        return chol.T @ (y - theta[0] * (1.0 - np.exp(-x / theta[1])))

    best = None
    for k0 in (0.5 * x.max(), x.max(), 3.0 * x.max()):
        got = optimize.least_squares(resid, [0.2, k0], xtol=1e-14, ftol=1e-14, gtol=1e-14)
        if best is None or got.cost < best.cost:
            best = got
    return best.x


def test_noiseless_data_recovers_the_parameters():
    got = curve.fit(X, truth(X), np.diag(np.full(6, 1e-4)), 5000.0)
    assert got.identified, got.reason
    assert got.a == pytest.approx(A_TRUE, rel=1e-6)
    assert got.k == pytest.approx(K_TRUE, rel=1e-6)
    assert got.marginal_at_current == pytest.approx(A_TRUE / K_TRUE * math.exp(-1.0), rel=1e-6)


def test_diagonal_covariance_is_weighted_least_squares():
    rng = np.random.default_rng(3)
    y = truth(X) + rng.normal(0.0, 0.01, 6)
    var = np.array([1e-4, 4e-4, 1e-4, 2e-4, 3e-4, 1e-4])
    got = curve.fit(X, y, np.diag(var), 5000.0)
    w = 1.0 / np.sqrt(var)
    ref = optimize.least_squares(
        lambda th: w * (y - th[0] * (1.0 - np.exp(-X / th[1]))), [0.2, 5000.0], bounds=([0.0, 100.0], [10.0, 1e6]),
        xtol=1e-14, ftol=1e-14, gtol=1e-14,
    ).x
    assert got.a == pytest.approx(ref[0], rel=1e-5)
    assert got.k == pytest.approx(ref[1], rel=1e-5)


def test_correlated_covariance_matches_an_independent_gls():
    rng = np.random.default_rng(11)
    cov = shared_base_cov(6)
    y = truth(X) + np.linalg.cholesky(cov) @ rng.normal(0.0, 1.0, 6)
    got = curve.fit(X, y, cov, 5000.0)
    ref = reference_gls(X, y, cov)
    assert got.a == pytest.approx(ref[0], rel=1e-5)
    assert got.k == pytest.approx(ref[1], rel=1e-5)


def test_marginal_se_equals_the_delta_method_computed_by_finite_differences():
    rng = np.random.default_rng(5)
    cov = shared_base_cov(6)
    y = truth(X) + np.linalg.cholesky(cov) @ rng.normal(0.0, 1.0, 6)
    x0 = 4500.0
    got = curve.fit(X, y, cov, x0)

    def model(theta):
        return theta[0] * (1.0 - np.exp(-X / theta[1]))

    def m_at_current(theta):
        return theta[0] / theta[1] * math.exp(-x0 / theta[1])

    theta = np.array([got.a, got.k])
    step = theta * 1e-6
    jac = np.column_stack([(model(theta + e) - model(theta - e)) / (2 * e[i]) for i, e in enumerate(np.diag(step))])
    grad = np.array([(m_at_current(theta + e) - m_at_current(theta - e)) / (2 * e[i]) for i, e in enumerate(np.diag(step))])
    cov_theta = np.linalg.inv(jac.T @ np.linalg.inv(cov) @ jac)
    assert got.marginal_se == pytest.approx(math.sqrt(grad @ cov_theta @ grad), rel=1e-4)
    assert got.marginal_at_current == pytest.approx(m_at_current(theta), rel=1e-12)


def test_the_shared_base_month_changes_the_standard_error():
    cov = shared_base_cov(6)
    full = curve.fit(X, truth(X), cov, 4500.0)  # noiseless: both fits find the same curve
    diagonal = curve.fit(X, truth(X), np.diag(np.diag(cov)), 4500.0)
    assert full.a == pytest.approx(diagonal.a, rel=1e-6)
    assert abs(full.marginal_se / diagonal.marginal_se - 1.0) > 0.2  # weighting them as independent mis-states it


def test_a_singular_covariance_is_replaced_by_the_equicorrelated_one():
    series = np.array([[0.0, 0.1, 0.3], [0.2, 0.1, 0.0], [0.1, 0.5, 0.2], [0.3, 0.2, 0.1], [0.0, 0.4, 0.4]])
    cells = np.cov(series)  # five cells over three pre-period months: rank at most 2
    assert np.linalg.matrix_rank(cells) < 5
    usable = curve.usable_covariance(cells)
    sd = np.sqrt(np.diag(cells))
    assert np.allclose(np.diag(usable), np.diag(cells))
    assert usable[0, 1] == pytest.approx(0.5 * sd[0] * sd[1])
    assert np.linalg.eigvalsh(usable).min() > 0.0
    proper = shared_base_cov(3)
    assert curve.usable_covariance(proper) is proper  # a positive-definite matrix is left alone

    x = np.array([1000.0, 2000.0, 3000.0, 4000.0, 5000.0])
    got = curve.fit(x, truth(x), cells, 5000.0)  # must not raise
    assert got.a == pytest.approx(A_TRUE, rel=1e-4)


def test_fewer_than_three_cells_is_not_identified():
    got = curve.fit(X[:2], truth(X[:2]), np.diag([1e-4, 1e-4]), 3000.0)
    assert not got.identified and got.a is None and got.marginal_se is None
    assert "three cells" in got.reason


def test_a_falling_effect_is_not_identified():
    got = curve.fit(X, -truth(X), np.diag(np.full(6, 1e-4)), 5000.0)
    assert not got.identified and got.a is not None and got.a < 0.0


def test_k_at_a_bound_is_not_identified():
    x = np.array([1000.0, 2000.0, 3000.0])
    got = curve.fit(x, 1e-5 * x, np.diag(np.full(3, 1e-8)), 3000.0)  # a straight line: k runs to its upper bound
    assert not got.identified
    assert got.k == pytest.approx(curve.K_HIGH * 3000.0, rel=1e-3)
    assert "bound" in got.reason


def test_a_marginal_return_not_above_zero_is_not_identified():
    rng = np.random.default_rng(2)
    y = truth(X) + rng.normal(0.0, 0.05, 6)
    got = curve.fit(X, y, np.diag(np.full(6, 0.05**2)), 5000.0)
    assert not got.identified
    assert got.reason


def test_no_spend_is_not_fitted():
    got = curve.fit(np.zeros(4), np.zeros(4), np.eye(4), 0.0)
    assert not got.identified and got.a is None


def test_k_stays_inside_the_observed_range_bounds():
    rng = np.random.default_rng(9)
    for _ in range(20):
        y = truth(X) + rng.normal(0.0, 0.02, 6)
        got = curve.fit(X, y, np.diag(np.full(6, 4e-4)), 5000.0)
        assert curve.K_LOW * 1500.0 <= got.k <= curve.K_HIGH * 5000.0


def test_the_effect_is_concave_over_the_capped_range():
    xs = np.linspace(0.0, 1.5 * 5000.0, 200)
    vals = np.array([curve.effect(A_TRUE, K_TRUE, x) for x in xs])
    assert np.all(np.diff(vals, 2) <= 1e-15)
    marg = np.array([curve.marginal(A_TRUE, K_TRUE, x) for x in xs])
    assert np.all(np.diff(marg) < 0.0)
    assert curve.marginal(A_TRUE, K_TRUE, 2000.0) == pytest.approx(
        (curve.effect(A_TRUE, K_TRUE, 2000.0 + 1e-3) - curve.effect(A_TRUE, K_TRUE, 2000.0 - 1e-3)) / 2e-3, rel=1e-8
    )


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute(
        "CREATE TABLE fct_team_month(team_id VARCHAR, month DATE, spend_per_head_cents DOUBLE, headcount INTEGER)"
    )
    c.execute(
        "INSERT INTO fct_team_month VALUES ('a', '2025-01-01', 0, 10), ('a', '2025-02-01', 100, 10),"
        " ('a', '2025-03-01', 200, 11), ('a', '2025-04-01', 400, 12), ('a', '2025-05-01', 300, 13),"
        " ('b', '2025-04-01', 50, 5)"
    )
    c.execute("CREATE TABLE est_att_gt(through_month VARCHAR, team_id VARCHAR, g DATE, t DATE, att DOUBLE, se DOUBLE, n_controls INTEGER)")
    c.execute("CREATE TABLE est_att_cov(through_month VARCHAR, team_id VARCHAR, g DATE, t1 DATE, t2 DATE, cov DOUBLE)")
    c.execute(
        "INSERT INTO est_att_gt VALUES ('2025-05', 'a', '2025-02-01', '2025-03-01', 0.1, 0.1, 1),"
        " ('2025-05', 'a', '2025-02-01', '2025-04-01', 0.2, 0.1, 1), ('2025-05', 'a', '2025-02-01', '2025-05-01', 0.15, 0.1, 1)"
    )
    c.execute(
        "INSERT INTO est_att_cov VALUES ('2025-05', 'a', '2025-02-01', '2025-03-01', '2025-03-01', 4.0),"
        " ('2025-05', 'a', '2025-02-01', '2025-03-01', '2025-04-01', 1.0),"
        " ('2025-05', 'a', '2025-02-01', '2025-03-01', '2025-05-01', 0.5),"
        " ('2025-05', 'a', '2025-02-01', '2025-04-01', '2025-04-01', 5.0),"
        " ('2025-05', 'a', '2025-02-01', '2025-04-01', '2025-05-01', 2.0),"
        " ('2025-05', 'a', '2025-02-01', '2025-05-01', '2025-05-01', 6.0)"
    )
    yield c
    c.close()


def test_spend_profile_is_the_mean_of_the_last_three_months(con):
    prof = curve.spend_profile(con, "2025-05")
    assert prof["a"] == (pytest.approx((200 + 400 + 300) / 3), 400.0, 13)
    assert prof["b"] == (50.0, 50.0, 5)
    assert curve.spend_profile(con, "2025-03")["a"] == (pytest.approx(100.0), 200.0, 11)  # only months up to through


def test_read_cells_returns_spend_effect_and_the_symmetric_covariance(con):
    x, y, cov = curve.read_cells(con, "2025-05", "a")
    assert x.tolist() == [200.0, 400.0, 300.0]
    assert y.tolist() == [0.1, 0.2, 0.15]
    assert cov.tolist() == [[4.0, 1.0, 0.5], [1.0, 5.0, 2.0], [0.5, 2.0, 6.0]]
