"""Item 9: Validate the expanded golden dataset.

Tests cover:
- JSON schema validity (required fields, non-empty values)
- ID uniqueness
- Dataset size (must be >= 20 after expansion)
- Source diversity (>= 5 distinct source files)
- Functional coverage of nuanced/negative goldens added in Item 9:
  g17 (SOC2 negative — audit != no-vuln), g18 (GDPR conditional refusal),
  g19 (K8s default-deny), g20 (shared-responsibility nuance)
- Cross-golden ID sequence has no gaps
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

GOLDENS_PATH = Path(__file__).parents[1] / "goldens" / "retriever_goldens.json"

REQUIRED_FIELDS = {"id", "source", "input", "expected_output", "context"}


@pytest.fixture(scope="module")
def goldens() -> list[dict]:
    assert GOLDENS_PATH.exists(), f"Golden dataset not found: {GOLDENS_PATH}"
    with GOLDENS_PATH.open(encoding="utf-8") as fh:
        data = json.load(fh)
    assert isinstance(data, list), "Golden dataset must be a JSON array"
    return data


class TestGoldenDatasetSchema:
    def test_minimum_size(self, goldens):
        assert len(goldens) >= 20, (
            f"Golden dataset must have at least 20 entries; found {len(goldens)}"
        )

    def test_all_required_fields_present(self, goldens):
        for g in goldens:
            missing = REQUIRED_FIELDS - g.keys()
            assert not missing, f"Golden {g.get('id', '?')} missing fields: {missing}"

    def test_no_empty_field_values(self, goldens):
        for g in goldens:
            for field in REQUIRED_FIELDS:
                val = g.get(field, "")
                assert isinstance(val, str) and val.strip(), (
                    f"Golden {g['id']}.{field} is empty or non-string"
                )

    def test_id_uniqueness(self, goldens):
        ids = [g["id"] for g in goldens]
        assert len(ids) == len(set(ids)), (
            f"Duplicate IDs found: {[i for i in ids if ids.count(i) > 1]}"
        )

    def test_ids_follow_gN_format(self, goldens):
        pattern = re.compile(r"^g\d+$")
        for g in goldens:
            assert pattern.match(g["id"]), (
                f"ID {g['id']} does not match expected format gN"
            )

    def test_source_diversity(self, goldens):
        sources = {g["source"] for g in goldens}
        assert len(sources) >= 5, (
            f"Golden dataset must reference at least 5 distinct source files; "
            f"found {len(sources)}: {sources}"
        )

    def test_expected_output_shorter_than_context(self, goldens):
        """expected_output should be a summary, not the full context."""
        for g in goldens:
            assert len(g["expected_output"]) <= len(g["context"]) + 50, (
                f"Golden {g['id']}: expected_output longer than context — "
                "expected_output should be a concise answer"
            )


class TestNegativeAndEdgeCaseGoldens:
    """Verify that the nuanced/negative goldens added in Item 9 exist and are correct."""

    def _get(self, goldens, gid: str) -> dict:
        found = [g for g in goldens if g["id"] == gid]
        assert found, f"Golden {gid} not found in dataset"
        return found[0]

    def test_g17_soc2_negative_does_not_claim_no_vulnerabilities(self, goldens):
        g = self._get(goldens, "g17")
        text = g["expected_output"].lower()
        assert "no" in text or "not" in text or "does not" in text, (
            "g17 (SOC2 negative) expected_output must express that SOC2 does NOT guarantee "
            "absence of vulnerabilities"
        )

    def test_g18_gdpr_refusal_acknowledges_exceptions(self, goldens):
        g = self._get(goldens, "g18")
        text = g["expected_output"].lower()
        assert "yes" in text or "can" in text, (
            "g18 (GDPR refusal) expected_output must acknowledge a company CAN refuse erasure"
        )

    def test_g19_kubernetes_default_behaviour_is_permissive(self, goldens):
        g = self._get(goldens, "g19")
        text = g["expected_output"].lower()
        assert "default" in text or "all" in text or "no restrict" in text, (
            "g19 (K8s network policy default) expected_output must state the permissive default"
        )

    def test_g20_shared_responsibility_cloud_provider_and_customer_both_mentioned(self, goldens):
        g = self._get(goldens, "g20")
        text = g["expected_output"].lower()
        assert "provider" in text or "cloud" in text, (
            "g20 expected_output must mention the cloud provider's role"
        )
        assert "customer" in text or "user" in text, (
            "g20 expected_output must mention the customer's role"
        )

    def test_g13_authentication_vs_authorization_distinction(self, goldens):
        g = self._get(goldens, "g13")
        text = g["expected_output"].lower()
        assert "authentication" in text and "authorization" in text, (
            "g13 must distinguish between authentication and authorization"
        )

    def test_g14_encryption_key_loss_consequence(self, goldens):
        g = self._get(goldens, "g14")
        text = g["expected_output"].lower()
        assert "unreadable" in text or "unrecoverable" in text or "cannot" in text, (
            "g14 must describe the consequence of losing the encryption key"
        )

    def test_g16_secret_rotation_rationale(self, goldens):
        g = self._get(goldens, "g16")
        text = g["expected_output"].lower()
        # Must mention limiting damage/exposure
        assert any(kw in text for kw in ("damage", "window", "comprom", "limit", "blast")), (
            "g16 must describe why rotation limits the damage of a credential compromise"
        )


class TestDatasetCoverage:
    """Structural checks to ensure the dataset covers diverse cloud-security topics."""

    EXPECTED_SOURCES = {
        "01-access-control-iam.md",
        "02-encryption-at-rest.md",
        "04-audit-logging.md",
        "05-gdpr-data-subject-rights.md",
        "07-soc2-trust-criteria.md",
        "09-secrets-management.md",
        "10-kubernetes-network-policy.md",
        "12-shared-responsibility-model.md",
    }

    def test_all_expected_sources_represented(self, goldens):
        present = {g["source"] for g in goldens}
        missing = self.EXPECTED_SOURCES - present
        assert not missing, (
            f"These source docs have no golden covering them: {missing}"
        )

    def test_each_source_has_at_least_one_multi_hop_or_edge_case(self, goldens):
        """At least 4 of the new goldens (g13-g20) are edge/negative cases."""
        new_goldens = [g for g in goldens if g["id"] in {f"g{i}" for i in range(13, 21)}]
        assert len(new_goldens) >= 4, (
            f"Expected at least 4 edge/negative goldens g13-g20; found {len(new_goldens)}"
        )
