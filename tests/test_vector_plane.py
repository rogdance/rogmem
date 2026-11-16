"""The ball's two directions come from the small matrix, and only once.

The graph is redrawn on every read. Solving a 155×1024 singular value problem to get two
directions cost seconds per request, on the same GIL as the distill pipeline. The two
directions of an n×d matrix are the top two eigenvectors of its n×n Gram matrix — the same
answer from the smaller problem.
"""

from __future__ import annotations

import numpy as np

from rogmem import engine


def _vectors(count=12, width=8):
    rng = np.random.default_rng(7)
    return [list(map(float, row)) for row in rng.normal(size=(count, width))]


def _reference(vectors):
    known = [i for i, vec in enumerate(vectors) if vec]
    width = min(len(vectors[i]) for i in known)
    matrix = np.array([vectors[i][:width] for i in known], dtype=float)
    matrix -= matrix.mean(axis=0)
    _, _, vt = np.linalg.svd(matrix, full_matrices=False)
    flat = matrix @ vt[:2].T
    scale = np.abs(flat).max(axis=0)
    scale[scale == 0] = 1.0
    return flat / scale


def test_the_big_singular_value_problem_is_not_solved(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the full singular value problem is back in the read path")

    monkeypatch.setattr(np.linalg, "svd", refuse)
    monkeypatch.setattr(engine, "_PLANE_KEY", None)
    plane = engine._vector_plane(_vectors())
    assert all(pair is not None for pair in plane)


def test_the_directions_are_the_ones_the_big_problem_gave(monkeypatch):
    vectors = _vectors()
    monkeypatch.setattr(engine, "_PLANE_KEY", None)
    plane = engine._vector_plane(vectors)
    reference = _reference(vectors)
    got = np.array([pair for pair in plane], dtype=float)
    assert np.allclose(np.abs(got), np.abs(reference), atol=1e-6)


def test_a_second_read_reuses_the_directions(monkeypatch):
    vectors = _vectors()
    monkeypatch.setattr(engine, "_PLANE_KEY", None)
    calls: list[int] = []
    real = np.linalg.eigh

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(np.linalg, "eigh", counted)
    first = engine._vector_plane(vectors)
    second = engine._vector_plane(vectors)
    assert calls == [1]
    assert first == second


def test_a_changed_vector_redraws_the_directions(monkeypatch):
    vectors = _vectors()
    monkeypatch.setattr(engine, "_PLANE_KEY", None)
    engine._vector_plane(vectors)
    before = engine._PLANE_KEY
    moved = [list(vec) for vec in vectors]
    moved[0][0] = moved[0][0] + 1.0
    engine._vector_plane(moved)
    assert engine._PLANE_KEY != before


def test_a_new_page_is_not_answered_with_the_old_page_count(monkeypatch):
    """A page written between two reads makes the list longer. The solved rows still answer it."""
    vectors = _vectors(11, 8)
    monkeypatch.setattr(engine, "_PLANE_KEY", None)
    first = engine._vector_plane(vectors)
    assert len(first) == 11 and first[0] is not None
    grown = [None] + [list(vec) for vec in vectors]
    second = engine._vector_plane(grown)
    assert len(second) == 12
    assert second[0] is None
    assert second[1:] == first


def test_pages_without_a_vector_get_no_plane(monkeypatch):
    monkeypatch.setattr(engine, "_PLANE_KEY", None)
    assert engine._vector_plane([None, None, None, None]) == [None, None, None, None]
    assert engine._vector_plane([None, [0.0], None, [0.0]]) == [None, None, None, None]
