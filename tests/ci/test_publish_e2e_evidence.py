"""Tests for scripts/ci/publish_e2e_evidence.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "publish_e2e_evidence.py"
_spec = importlib.util.spec_from_file_location("publish_e2e_evidence", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load publish_e2e_evidence.py")
_mod = importlib.util.module_from_spec(_spec)
sys.modules["publish_e2e_evidence"] = _mod
_spec.loader.exec_module(_mod)


def _png(width: int = 4, height: int = 3) -> bytes:
    return _mod.PNG_SIGNATURE + b"\x00\x00\x00\rIHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big")


def test_load_evidence_validates_manifest_and_pngs(tmp_path):
    (tmp_path / "shot.png").write_bytes(_png())
    (tmp_path / "diff.png").write_bytes(_png())
    (tmp_path / "actual.png").write_bytes(_png())
    (tmp_path / "expected.png").write_bytes(_png())
    (tmp_path / "e2e-evidence.json").write_text(
        """{
          "version": 1,
          "screenshots": [{"name": "main-view.png", "file": "shot.png"}],
          "diffs": [{"name": "main-view", "diff": "diff.png", "actual": "actual.png", "expected": "expected.png"}]
        }""",
        encoding="utf-8",
    )

    files, payloads = _mod.load_evidence(tmp_path)

    assert [item.label for item in files] == [
        "new screenshot: main-view.png",
        "visual diff: main-view",
        "visual actual: main-view",
        "visual expected: main-view",
    ]
    assert set(payloads) == {"shot.png", "diff.png", "actual.png", "expected.png"}


def test_load_evidence_rejects_path_escape_and_non_png(tmp_path):
    (tmp_path / "e2e-evidence.json").write_text(
        '{"version":1,"screenshots":[{"name":"bad","file":"../secret.png"}],"diffs":[]}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsafe filename"):
        _mod.load_evidence(tmp_path)

    (tmp_path / "e2e-evidence.json").write_text(
        '{"version":1,"screenshots":[{"name":"bad","file":"not-png.png"}],"diffs":[]}',
        encoding="utf-8",
    )
    (tmp_path / "not-png.png").write_bytes(b"not a png")

    with pytest.raises(ValueError, match="not a PNG"):
        _mod.load_evidence(tmp_path)




def test_upload_evidence_accepts_only_attachment_urls(tmp_path, monkeypatch):
    shot = tmp_path / "shot.png"
    shot.write_bytes(_png())
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return _mod.subprocess.CompletedProcess(
            args,
            0,
            stdout="![shot.png](https://github.com/user-attachments/assets/12345678-1234-1234-1234-123456789abc)\n",
        )

    monkeypatch.setattr(_mod.subprocess, "run", fake_run)

    result = _mod.upload_evidence(
        [_mod.EvidenceFile("shot.png", "new screenshot: shot.png")],
        tmp_path,
        "NousResearch/hermes-agent",
        "bot-session-token",
    )

    assert result == {"shot.png": "https://github.com/user-attachments/assets/12345678-1234-1234-1234-123456789abc"}
    assert calls[0][0] == ["gh", "image", "--repo", "NousResearch/hermes-agent", str(shot)]
    assert calls[0][1]["env"]["GH_SESSION_TOKEN"] == "bot-session-token"




def test_upload_evidence_reports_gh_image_error(tmp_path, monkeypatch, capsys):
    shot = tmp_path / "shot.png"
    shot.write_bytes(_png())

    def fake_run(args, **kwargs):
        raise _mod.subprocess.CalledProcessError(
            1,
            args,
            output="upload output",
            stderr="upload error",
        )

    monkeypatch.setattr(_mod.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Failed to upload shot.png.*upload error"):
        _mod.upload_evidence(
            [_mod.EvidenceFile("shot.png", "new screenshot: shot.png")],
            tmp_path,
            "NousResearch/hermes-agent",
            "bot-session-token",
        )

    captured = capsys.readouterr()
    assert "Failed to upload shot.png" in captured.err
    assert "upload output" in captured.err
    assert "upload error" in captured.err


def test_publish_marks_evidence_upload_failure_in_pr_comment(tmp_path, monkeypatch):
    comment = {
        "id": 123,
        "body": "before\n<!-- hermes-e2e-evidence:start -->\npending\n<!-- hermes-e2e-evidence:end -->\nafter",
    }
    updates = []

    monkeypatch.setattr(
        _mod,
        "load_evidence",
        lambda evidence_dir: (
            [_mod.EvidenceFile("shot.png", "new screenshot: shot.png")],
            {},
        ),
    )
    monkeypatch.setattr(_mod, "_wait_for_review_comment", lambda *args: comment)
    monkeypatch.setattr(
        _mod,
        "upload_evidence",
        lambda *args: (_ for _ in ()).throw(
            RuntimeError("Failed to upload shot.png: bad <response>")
        ),
    )
    monkeypatch.setattr(
        _mod,
        "_api_request",
        lambda url, token, method, payload: updates.append((
            url,
            token,
            method,
            payload,
        )),
    )

    with pytest.raises(RuntimeError, match="Failed to upload shot.png"):
        _mod.publish(
            "github-token",
            "NousResearch/hermes-agent",
            tmp_path,
            "69868",
            "image-token",
        )

    assert updates == [
        (
            "https://api.github.com/repos/NousResearch/hermes-agent/issues/comments/123",
            "github-token",
            "PATCH",
            {
                "body": "before\n<!-- hermes-e2e-evidence:start -->\n<sub>inline evidence upload failed.</sub>\n\n<pre>Failed to upload shot.png: bad &lt;response&gt;</pre>\n<!-- hermes-e2e-evidence:end -->\nafter"
            },
        )
    ]


def test_publish_skips_when_no_review_comment_exists(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        _mod,
        "load_evidence",
        lambda evidence_dir: (
            [_mod.EvidenceFile("shot.png", "new screenshot: shot.png")],
            {},
        ),
    )
    monkeypatch.setattr(_mod, "_wait_for_review_comment", lambda *args: None)
    monkeypatch.setattr(
        _mod,
        "upload_evidence",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not upload")),
    )

    assert _mod.publish(
        "github-token",
        "NousResearch/hermes-agent",
        tmp_path,
        "83202",
        "image-token",
    ) is False
    assert "no CI review comment" in capsys.readouterr().out


def test_find_review_comment_requires_the_evidence_marker():
    pending = "<!-- hermes-ci-review-bot -->\n<!-- hermes-e2e-evidence:start -->\npending\n<!-- hermes-e2e-evidence:end -->"

    assert _mod._find_review_comment([{"body": "<!-- hermes-ci-review-bot --> no evidence"}]) is None
    assert _mod._find_review_comment([{"body": pending, "id": 123}]) == {"body": pending, "id": 123}


def test_replace_evidence_marker_requires_exactly_one_marker():
    with pytest.raises(ValueError, match="does not contain one"):
        _mod.replace_evidence_marker("no marker", "evidence")


# ─────────────────────────────────────────────────────────────────────────
# Transient classification. The publisher makes its OWN API calls (comment
# GET/PATCH), so a transient condition can arise HERE, after the artifact
# download. It signals that class with a dedicated exit code because its
# stdout echoes filenames from the untrusted PR artifact and can therefore
# never be trusted to classify anything.
# ─────────────────────────────────────────────────────────────────────────


def _http_error(code: int, headers: dict | None = None, body: bytes = b"") -> Exception:
    import io
    import urllib.error

    return urllib.error.HTTPError(
        "https://api.github.com/x", code, "err", headers or {}, io.BytesIO(body)
    )


@pytest.mark.parametrize("code", [500, 502, 503, 504, 505, 507, 508, 510, 511, 599])
def test_server_errors_are_transient(code):
    """A 5xx is GitHub's fault, not a defect in this repository.

    FleetReview F1 (P2): the class was a four-entry tuple `(500, 502, 503,
    504)`, so a 505 or 507 was re-raised and CI reported a REPOSITORY
    failure for a transient server-side condition. The contract is the
    whole 5xx range except 501, and this covers codes beyond the original
    four.
    """
    assert _mod._is_transient(_http_error(code)) is True


@pytest.mark.parametrize("code", [400, 401, 404, 409, 422, 501])
def test_client_errors_and_501_are_not_transient(code):
    """Only 5xx-server and rate limits retry.

    501 is deliberately excluded: it means the request itself is wrong, so
    retrying cannot help and tolerating it would hide a real defect.
    """
    assert _mod._is_transient(_http_error(code)) is False


def test_429_is_transient():
    assert _mod._is_transient(_http_error(429)) is True


def test_403_is_transient_only_with_a_rate_limit_shape():
    """A bare 403 is a permissions failure and must stay red."""
    assert _mod._is_transient(_http_error(403)) is False
    assert _mod._is_transient(
        _http_error(403, {"X-RateLimit-Remaining": "0"})) is True
    assert _mod._is_transient(_http_error(403, {"Retry-After": "60"})) is True
    assert _mod._is_transient(
        _http_error(403, body=b"You have exceeded a rate limit")) is True


def test_transient_exit_code_is_distinct_from_ordinary_failure():
    """0 or 1 would be indistinguishable from success / a real failure."""
    assert _mod.TRANSIENT_EXIT_CODE not in (0, 1, 2)


def _run_main(monkeypatch, tmp_path, raises: Exception | None):
    """Drive the real main() with publish() stubbed to raise ``raises``."""
    def fake_publish(*_a, **_k):
        if raises is not None:
            raise raises

    monkeypatch.setattr(_mod, "publish", fake_publish)
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GH_SESSION_TOKEN", "s")
    monkeypatch.setattr(sys, "argv", [
        "publish_e2e_evidence.py", "--evidence-dir", str(tmp_path),
        "--source-repo", "example-org/example-repo", "--pr-number", "1",
    ])
    return _mod.main()


def test_main_exits_with_the_transient_code_on_a_transient_error(monkeypatch, tmp_path):
    """Classification is useless unless main() actually routes through it.

    Without this the wrapper's tolerance is unreachable: the publisher
    would propagate the HTTPError and exit 1, which the wrapper correctly
    reds — a transient condition reported as a defect.
    """
    rc = _run_main(monkeypatch, tmp_path, _http_error(503))
    assert rc == _mod.TRANSIENT_EXIT_CODE, (
        f"a 503 from the publisher exited {rc}, so the wrapper cannot "
        "distinguish it from a real failure"
    )


def test_main_propagates_a_real_error(monkeypatch, tmp_path):
    """A genuine failure must still crash loudly, never exit 75."""
    import urllib.error

    with pytest.raises(urllib.error.HTTPError):
        _run_main(monkeypatch, tmp_path, _http_error(404))


def test_main_returns_zero_on_success(monkeypatch, tmp_path):
    assert _run_main(monkeypatch, tmp_path, None) == 0
