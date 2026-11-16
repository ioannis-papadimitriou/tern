"""Generic end-to-end smoke contracts. Do not edit per project; edit adapter.py.

Each test calls into the adapter. Unwired functions FAIL loudly; NotApplicable skips
are visible in the report (`pytest -rs`).
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from . import adapter as A

pytestmark = pytest.mark.smoke


def call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except NotImplementedError:
        pytest.fail(
            f"tests/smoke/adapter.py::{fn.__name__} is not wired yet", pytrace=False
        )
    except A.NotApplicable as e:
        pytest.skip(f"{fn.__name__}: {e}")


@pytest.fixture(scope="module")
def cfg():
    return call(A.load_smoke_config)


@pytest.fixture(scope="module")
def ds(cfg):
    return call(A.build_tiny_dataset, cfg)


@pytest.fixture(scope="module")
def trained(cfg, ds, tmp_path_factory) -> tuple[list[float], Path]:
    out = tmp_path_factory.mktemp("ckpt")
    losses = call(A.train_tiny, cfg, ds, out)
    return losses, out


def test_dataset_nonempty(ds):
    n = call(A.dataset_size, ds)
    assert n > 0, "preprocessing produced an empty dataset"


def test_training_losses_finite(trained):
    losses, _ = trained
    assert len(losses) > 0, "train_tiny returned no losses"
    bad = [i for i, l in enumerate(losses) if not math.isfinite(l)]
    assert not bad, f"non-finite loss at steps {bad}"


def test_training_reduces_loss(trained):
    # Few steps on a tiny set should overfit at least a little. If this is flaky for
    # your setup, raise the step count or lr in the smoke config. Don't loosen this check.
    losses, _ = trained
    if len(losses) < 2:
        pytest.skip("need >= 2 steps to compare")
    assert losses[-1] < losses[0], (
        f"loss did not decrease: first={losses[0]:.4f} last={losses[-1]:.4f}"
    )


def test_checkpoint_written(trained):
    _, out = trained
    assert any(out.iterdir()), f"no files written to checkpoint dir {out}"


def test_checkpoint_roundtrip_deterministic(cfg, trained):
    _, out = trained
    inputs = call(A.smoke_inputs)
    p1 = call(A.predict, cfg, out, inputs)
    p2 = call(A.predict, cfg, out, inputs)
    assert len(p1) == len(inputs)
    assert p1 == p2, (
        "predictions from the same checkpoint differ between loads (nondeterminism?)"
    )


def test_eval_deterministic(cfg, trained):
    _, out = trained
    m1 = call(A.evaluate, cfg, out, 0)
    m2 = call(A.evaluate, cfg, out, 0)
    assert m1, "evaluate returned no metrics"
    for k, v in m1.items():
        assert math.isfinite(v), f"metric {k} is not finite: {v}"
    assert m1 == m2, f"same seed, different metrics: {m1} vs {m2}"


def test_reference_case():
    computed, expected, tol = call(A.reference_case)
    assert abs(computed - expected) <= tol, (
        f"reference case: got {computed}, expected {expected} ± {tol}"
    )


def test_api_smoke():
    client = call(A.api_client)
    method, path, body, status = call(A.api_smoke_request)
    resp = client.request(method, path, json=body)
    assert resp.status_code == status, (
        f"{method} {path} -> {resp.status_code}: {resp.text[:300]}"
    )
