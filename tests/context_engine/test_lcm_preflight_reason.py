"""LCM must record WHY preflight fired, so the banner can name the cause.

Ace's complaint was not just "no banner" — it was "why did lcm request compress?
we were way below our thresholds. this is not transparent at all." The arm is
useless if it says only "the engine asked".
"""
import tempfile

import pytest

lcm_engine = pytest.importorskip("plugins.context_engine.lcm.engine")
lcm_config = pytest.importorskip("plugins.context_engine.lcm.config")


def _engine(tmp_path):
    eng = lcm_engine.LCMEngine(
        config=lcm_config.LCMConfig(), hermes_home=str(tmp_path)
    )
    eng._session_id = "reason-test"
    eng._set_context_length(1_000_000, source="test")
    return eng


def test_reason_starts_empty(tmp_path):
    assert _engine(tmp_path).last_preflight_reason == ""


def test_mark_records_the_reason(tmp_path):
    eng = _engine(tmp_path)
    assert eng._mark_preflight_compression_requested("because reasons") is True
    assert eng.last_preflight_reason == "because reasons"


def test_mark_without_reason_clears_a_stale_one(tmp_path):
    """A later compaction must never inherit an earlier one's explanation."""
    eng = _engine(tmp_path)
    eng._mark_preflight_compression_requested("first cause")
    eng._mark_preflight_compression_requested()
    assert eng.last_preflight_reason == ""


@pytest.mark.parametrize(
    "marker,expected",
    [
        ("[Externalized LCM ingest payload: kind=ingest_payload; chars=99]",
         "a large attachment was moved to external storage"),
        ("[Externalized tool output: chars=99]",
         "a large tool result was moved to external storage"),
        ("[LCM active replay placeholder: assistant output quarantined; reason=x]",
         "a malformed assistant turn was quarantined"),
        ("[LCM sensitive redaction: secret]",
         "a secret was redacted from the transcript"),
    ],
)
def test_describe_names_each_cleanup_cause(tmp_path, marker, expected):
    eng = _engine(tmp_path)
    original = [{"role": "user", "content": "the real original content"}]
    replay = [{"role": "user", "content": marker}]
    assert eng._describe_ingest_cleanup_reason(original, replay) == expected


def test_describe_handles_length_mismatch(tmp_path):
    eng = _engine(tmp_path)
    got = eng._describe_ingest_cleanup_reason(
        [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}],
        [{"role": "user", "content": "a"}],
    )
    assert got == "the stored transcript and live context diverged"


def test_describe_never_raises_on_garbage(tmp_path):
    """An explanation must never be able to break a compaction."""
    eng = _engine(tmp_path)
    assert eng._describe_ingest_cleanup_reason(None, None)  # type: ignore[arg-type]


def test_every_gate_marker_has_a_label(tmp_path):
    """Lockstep guard: a new marker in the gate needs a label here.

    Without this, adding a trigger silently degrades the banner to the generic
    fallback — the exact class of quiet regression that produced this bug.
    """
    import inspect
    from plugins.context_engine.lcm import compaction as comp

    src = inspect.getsource(comp.CompactionMixin._replay_diff_requests_ingest_cleanup)
    labelled = {m for m, _ in comp.CompactionMixin._INGEST_CLEANUP_REASON_MARKERS}
    import re
    # markers the gate tests via startswith("...") / in "..."
    gate_markers = set(re.findall(r'"(\[[^"]+)"', src))
    missing = {m for m in gate_markers if not any(m.startswith(l) or l.startswith(m)
                                                  for l in labelled)}
    assert not missing, f"gate markers without a human label: {missing}"
