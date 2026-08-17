"""Тести для app.utils.paths (T1.5, Волна 1) — спільна path-traversal утиліта.

safe_path_within/safe_path_within_any замінили ad-hoc
os.path.abspath(...).startswith(...) перевірки, продубльовані вручну в
documents.py / transcription.py / telegram.py.
"""
import os

import pytest

from app.utils.paths import safe_path_within, safe_path_within_any


@pytest.fixture()
def base_dir(tmp_path):
    d = tmp_path / "base"
    d.mkdir()
    return str(d)


class TestSafePathWithin:
    def test_file_directly_inside_base_is_allowed(self, base_dir):
        candidate = os.path.join(base_dir, "file.txt")
        result = safe_path_within(base_dir, candidate)
        assert result is not None
        assert os.path.normcase(result) == os.path.normcase(os.path.realpath(candidate))

    def test_file_in_nested_subdir_is_allowed(self, base_dir):
        nested = os.path.join(base_dir, "sub", "dir", "file.txt")
        result = safe_path_within(base_dir, nested)
        assert result is not None

    def test_base_itself_is_allowed(self, base_dir):
        result = safe_path_within(base_dir, base_dir)
        assert result is not None

    def test_dotdot_traversal_outside_base_is_rejected(self, base_dir):
        traversal = os.path.join(base_dir, "..", "secret.txt")
        assert safe_path_within(base_dir, traversal) is None

    def test_sibling_directory_with_matching_prefix_is_rejected(self, tmp_path):
        # base_dir = ".../base", candidate = ".../base_evil/secret.txt" —
        # голий startswith('.../base') хибно пропустив би це; перевірка
        # межі по os.sep має відхилити.
        base = tmp_path / "base"
        base.mkdir()
        evil = tmp_path / "base_evil"
        evil.mkdir()
        candidate = str(evil / "secret.txt")
        assert safe_path_within(str(base), candidate) is None

    def test_unrelated_absolute_path_is_rejected(self, base_dir, tmp_path):
        other = tmp_path / "elsewhere" / "file.txt"
        assert safe_path_within(base_dir, str(other)) is None

    def test_empty_or_none_inputs_are_rejected(self, base_dir):
        assert safe_path_within(base_dir, "") is None
        assert safe_path_within(base_dir, None) is None
        assert safe_path_within("", os.path.join(base_dir, "f.txt")) is None
        assert safe_path_within(None, os.path.join(base_dir, "f.txt")) is None

    def test_nonexistent_candidate_within_base_is_still_allowed(self, base_dir):
        # Файл ще не створений (наприклад, ціль завантаження) — все одно ОК,
        # якщо резолвиться всередину base.
        target = os.path.join(base_dir, "not_yet_created.bin")
        assert not os.path.exists(target)
        assert safe_path_within(base_dir, target) is not None


class TestSafePathWithinAny:
    def test_matches_second_root(self, tmp_path):
        root_a = str(tmp_path / "a")
        root_b = str(tmp_path / "b")
        os.makedirs(root_a)
        os.makedirs(root_b)
        candidate = os.path.join(root_b, "file.txt")
        result = safe_path_within_any([root_a, root_b], candidate)
        assert result is not None

    def test_rejects_when_outside_all_roots(self, tmp_path):
        root_a = str(tmp_path / "a")
        root_b = str(tmp_path / "b")
        os.makedirs(root_a)
        os.makedirs(root_b)
        outside = str(tmp_path / "outside" / "file.txt")
        assert safe_path_within_any([root_a, root_b], outside) is None

    def test_ignores_falsy_roots(self, tmp_path):
        root_a = str(tmp_path / "a")
        os.makedirs(root_a)
        candidate = os.path.join(root_a, "file.txt")
        result = safe_path_within_any(["", None, root_a], candidate)
        assert result is not None
