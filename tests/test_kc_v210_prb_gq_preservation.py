"""PR-B (Issue #220) Golden v1 preservation — written FIRST.

Preservation ONLY: the v1 config still loads and validates, all 10
questions are frozen, gq10's rights-unknown expectation is preserved,
and the config file bytes are unchanged (SHA-256 preservation anchor
from base 15867026). This file does NOT claim any per-question gq
execution: no dedicated gq01–gq10 runner execution is asserted here,
and the synthetic q01 set in tests/test_consumer_acceptance.py is a
different set and is never cited as gq execution evidence.
"""

import hashlib
from pathlib import Path

from cba_kb.consumer_projection import load_golden_questions

CONFIG = Path(__file__).resolve().parent.parent / (
    "config/consumer_golden_questions_v1.yaml")
BASE_SHA256 = "ea718725d2eabb593482b76d8501a5e9223873399230d949532a026a9fd0300e"


def test_gq_v1_config_loads_and_validates():
    value = load_golden_questions(CONFIG)
    assert value["version"] == "v1"
    assert value["schema_version"] == 1
    assert len(value["questions"]) == 10


def test_gq_v1_all_questions_frozen_with_complete_ids():
    value = load_golden_questions(CONFIG)
    ids = [q["id"] for q in value["questions"]]
    assert ids == ["gq%02d" % i for i in range(1, 11)]
    assert all(q["frozen"] is True for q in value["questions"])


def test_gq_v1_frozen_at_and_key_expectations_preserved():
    value = load_golden_questions(CONFIG)
    by_id = {q["id"]: q for q in value["questions"]}
    assert value["frozen_at"] == "2026-09-13"
    assert by_id["gq01"]["expected_answer"] == "v1.6.1-1"
    assert by_id["gq10"]["expected_answer"] == "BLOCKED"


def test_gq_v1_file_bytes_unchanged_preservation_anchor():
    digest = hashlib.sha256(CONFIG.read_bytes()).hexdigest()
    assert digest == BASE_SHA256
