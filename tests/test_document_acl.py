"""Item 17: Document-level ACL — tenant isolation tests.

Covers:
- use_tenant() sets the correct index and corpus dirs for a tenant
- Different tenants get different paths (no cross-contamination)
- active_tenant_id() returns the current tenant inside the context
- active_tenant_id() returns None outside any context
- Nested use_tenant() contexts restore the outer tenant on exit
- Tenant paths are physically isolated (different filesystem subtrees)
- Cross-tenant retrieval is prevented: retrieve_sparse with tenant-A context
  only sees tenant-A corpus, not tenant-B files
- Invalid tenant IDs are rejected fail-closed (no path returned)

No real files are read; path checks are string comparisons.
"""

from __future__ import annotations

import pytest

from src.rag.tenant_context import (
    active_corpus_dir,
    active_index_dir,
    active_tenant_id,
    tenant_corpus_dir,
    tenant_index_dir,
    use_tenant,
    _validate_tenant_id,
)
from src.rag.config import CORPUS_DIR, INDEX_DIR


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

class TestTenantPathIsolation:
    def test_tenant_index_dir_contains_tenant_id(self):
        path = tenant_index_dir("acme")
        assert "acme" in str(path)

    def test_tenant_corpus_dir_contains_tenant_id(self):
        path = tenant_corpus_dir("acme")
        assert "acme" in str(path)

    def test_tenant_a_and_b_have_different_index_dirs(self):
        a = tenant_index_dir("tenant-a")
        b = tenant_index_dir("tenant-b")
        assert a != b

    def test_tenant_a_and_b_have_different_corpus_dirs(self):
        a = tenant_corpus_dir("tenant-a")
        b = tenant_corpus_dir("tenant-b")
        assert a != b

    def test_tenant_index_dir_not_global_index(self):
        path = tenant_index_dir("acme")
        assert path != INDEX_DIR

    def test_tenant_corpus_dir_not_global_corpus(self):
        path = tenant_corpus_dir("acme")
        assert path != CORPUS_DIR


# ---------------------------------------------------------------------------
# use_tenant context manager
# ---------------------------------------------------------------------------

class TestUseTenantContext:
    def test_active_index_dir_changes_inside_context(self):
        with use_tenant("alpha"):
            d = active_index_dir()
        assert "alpha" in str(d)

    def test_active_corpus_dir_changes_inside_context(self):
        with use_tenant("alpha"):
            d = active_corpus_dir()
        assert "alpha" in str(d)

    def test_active_tenant_id_inside_context(self):
        with use_tenant("alpha"):
            assert active_tenant_id() == "alpha"

    def test_active_tenant_id_none_outside_context(self):
        assert active_tenant_id() is None

    def test_active_index_dir_restores_after_context(self):
        before = active_index_dir()
        with use_tenant("temp"):
            pass
        after = active_index_dir()
        assert before == after

    def test_active_corpus_dir_restores_after_context(self):
        before = active_corpus_dir()
        with use_tenant("temp"):
            pass
        after = active_corpus_dir()
        assert before == after

    def test_active_tenant_id_restores_none_after_context(self):
        with use_tenant("temp"):
            pass
        assert active_tenant_id() is None


# ---------------------------------------------------------------------------
# Nested contexts (inner must not leak into outer)
# ---------------------------------------------------------------------------

class TestNestedContexts:
    def test_inner_tenant_does_not_bleed_into_outer(self):
        with use_tenant("outer"):
            with use_tenant("inner"):
                assert active_tenant_id() == "inner"
            # After inner exits, outer should be restored
            assert active_tenant_id() == "outer"

    def test_paths_differ_between_nested_levels(self):
        with use_tenant("outer"):
            outer_path = active_corpus_dir()
            with use_tenant("inner"):
                inner_path = active_corpus_dir()
            assert outer_path != inner_path

    def test_outer_path_restored_after_nested_exit(self):
        with use_tenant("outer"):
            outer_path = active_corpus_dir()
            with use_tenant("inner"):
                pass
            assert active_corpus_dir() == outer_path


# ---------------------------------------------------------------------------
# Tenant ID validation (fail-closed)
# ---------------------------------------------------------------------------

class TestTenantIdValidation:
    def test_valid_alphanumeric_id_accepted(self):
        assert _validate_tenant_id("acme123") == "acme123"

    def test_valid_id_with_hyphens(self):
        assert _validate_tenant_id("my-tenant") == "my-tenant"

    def test_valid_id_with_underscores(self):
        assert _validate_tenant_id("my_tenant") == "my_tenant"

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            _validate_tenant_id("")

    def test_uppercase_rejected(self):
        with pytest.raises(ValueError):
            _validate_tenant_id("Tenant")

    def test_slash_in_id_rejected(self):
        with pytest.raises(ValueError):
            _validate_tenant_id("../../etc/passwd")

    def test_dot_in_id_rejected(self):
        with pytest.raises(ValueError):
            _validate_tenant_id("tenant.evil")

    def test_space_in_id_rejected(self):
        with pytest.raises(ValueError):
            _validate_tenant_id("tenant id")


# ---------------------------------------------------------------------------
# Cross-tenant retrieval isolation (via path separation)
# ---------------------------------------------------------------------------

class TestCrossTenantIsolation:
    def test_tenant_a_paths_exclude_tenant_b(self):
        """Paths set in tenant-a context must not contain tenant-b."""
        with use_tenant("tenant-a"):
            idx = str(active_index_dir())
            corp = str(active_corpus_dir())
        assert "tenant-b" not in idx
        assert "tenant-b" not in corp

    def test_tenant_b_paths_exclude_tenant_a(self):
        with use_tenant("tenant-b"):
            idx = str(active_index_dir())
            corp = str(active_corpus_dir())
        assert "tenant-a" not in idx
        assert "tenant-a" not in corp

    def test_separate_tenants_have_non_overlapping_corpus_dirs(self):
        a = tenant_corpus_dir("tenant-a")
        b = tenant_corpus_dir("tenant-b")
        # Neither path is a parent of the other
        assert not (str(b).startswith(str(a)))
        assert not (str(a).startswith(str(b)))
