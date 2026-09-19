"""Tenant-scoped paths and active context for phase 4 tenancy isolation."""

from __future__ import annotations

import contextlib
import re
from contextvars import ContextVar
from pathlib import Path

from .config import CORPUS_DIR, INDEX_DIR, PROJECT_ROOT

_TENANT_ID_RE = re.compile(r"^[a-z0-9_-]+$")

_active_index_dir: ContextVar[Path | None] = ContextVar("active_index_dir", default=None)
_active_corpus_dir: ContextVar[Path | None] = ContextVar("active_corpus_dir", default=None)
_active_tenant_id: ContextVar[str | None] = ContextVar("active_tenant_id", default=None)


def active_tenant_id() -> str | None:
    return _active_tenant_id.get()


def _validate_tenant_id(tenant_id: str) -> str:
    if not isinstance(tenant_id, str) or not tenant_id or not _TENANT_ID_RE.fullmatch(tenant_id):
        raise ValueError("tenant_id must be a safe slug using only a-z, 0-9, underscore, or hyphen")
    return tenant_id


def tenant_index_dir(tenant_id: str) -> Path:
    tenant = _validate_tenant_id(tenant_id)
    return PROJECT_ROOT / "data" / "index" / "tenants" / tenant / "chroma"


def tenant_corpus_dir(tenant_id: str) -> Path:
    tenant = _validate_tenant_id(tenant_id)
    return PROJECT_ROOT / "data" / "tenants" / tenant / "corpus"


def active_index_dir() -> Path:
    return _active_index_dir.get() or INDEX_DIR


def active_corpus_dir() -> Path:
    return _active_corpus_dir.get() or CORPUS_DIR


@contextlib.contextmanager
def use_tenant(tenant_id: str):
    index_token = _active_index_dir.set(tenant_index_dir(tenant_id))
    corpus_token = _active_corpus_dir.set(tenant_corpus_dir(tenant_id))
    tid_token = _active_tenant_id.set(tenant_id)
    try:
        yield
    finally:
        _active_corpus_dir.reset(corpus_token)
        _active_index_dir.reset(index_token)
        _active_tenant_id.reset(tid_token)
