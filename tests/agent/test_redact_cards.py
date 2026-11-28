"""Payment card masking (t_90c8b36c): tool results, inbound user text, browser_vision screenshots.

Every card number here is MADE UP: Luhn-valid with a real network prefix, last fours 0366 / 2226 / 2347,
none shared with a real card.
"""
from __future__ import annotations

import json
import shlex

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import redact_cards
from agent.redact_cards import (
    CARD_IMAGE_REFUSAL,
    count_card_numbers,
    luhn,
    mask_card_numbers,
)
from agent.tool_dispatch_helpers import make_tool_result_message

VISA = "4532015112830366"
MC = "5555444433332226"
AMEX = "341256789012347"


def _cfg(**security):
    return patch("hermes_cli.config.load_config_readonly", return_value={"security": security})


def test_fixture_numbers_are_luhn_valid_and_not_samples():
    for n in (VISA, MC, AMEX):
        assert luhn(n) and n not in redact_cards.PLACEHOLDERS
        assert not n.endswith("1120")


@pytest.mark.parametrize("raw, masked", [
    (f"{VISA[:4]} {VISA[4:8]} {VISA[8:12]} {VISA[12:]}", "XXXX XXXX XXXX 0366"),
    (f"{VISA[:4]}-{VISA[4:8]}-{VISA[8:12]}-{VISA[12:]}", "XXXX-XXXX-XXXX-0366"),
    (VISA, "XXXXXXXXXXXX0366"),
    (MC, "XXXXXXXXXXXX2226"),
    (f"{AMEX[:4]} {AMEX[4:10]} {AMEX[10:]}", "XXXX XXXXXX X2347"),
    (AMEX, "XXXXXXXXXXX2347"),
])
def test_card_groupings_mask_to_last_four(raw, masked):
    assert mask_card_numbers(f"card: {raw}.") == f"card: {masked}."


@pytest.mark.parametrize("text", [
    "order 112-1234567-1234567 shipped",                      # Amazon 3-7-7 order number
    "order 112 1234567 1234567 shipped",
    "ticket 4532015112830367",                                # 16 digits, Luhn fails
    "ticket 4532 0151 1283 0367",
    "sha 9f2c4532015112830366ab",                             # hex hash with a Luhn-valid digit run inside
    "deadbeef4532015112830366 and 4532015112830366cafe",
    "commit 4532015112830366a1b2c3d4e5f60718293a4b5c6d7e8f90",
    "ts=1795828039000000",                                    # microsecond epoch: no network prefix
    "4111 1111 1111 1111 cvc 123",                            # published sample number
    "card 4242424242424242 exp 12/30",
    "id 45320151128303661",                                   # 17 digits: not a card length
])
def test_false_positives_pass_untouched(text):
    assert mask_card_numbers(text) == text


def test_cvc_and_expiry_masked_on_same_and_next_two_lines():
    text = f"card {VISA} cvc: 417\nExpiry: 08/29\nCVV2 = 417\nexp 09/30\nzip 93401"
    assert mask_card_numbers(text) == (
        "card XXXXXXXXXXXX0366 cvc: XXX\nExpiry: XX/XX\nCVV2 = XXX\nexp 09/30\nzip 93401")


def test_cvc_needs_its_label_and_a_masked_number():
    assert mask_card_numbers(f"card {VISA}\nqty 417") == "card XXXXXXXXXXXX0366\nqty 417"
    assert mask_card_numbers("cvc: 417 exp 08/29") == "cvc: 417 exp 08/29"
    assert mask_card_numbers("ticket 4532015112830367 cvc 417") == "ticket 4532015112830367 cvc 417"


def test_json_encoded_form_fill_output():
    """Route 1 shape: terminal output inside a JSON tool result, lines joined by the \\n escape."""
    out = json.dumps({"output": f"filled #cardNumber={VISA}\ncardCvc=417\ncardExpiry=0829\nok", "exit_code": 0})
    masked = mask_card_numbers(out)
    assert VISA not in masked and "417" not in masked and "0829" not in masked
    assert json.loads(masked)["output"] == "filled #cardNumber=XXXXXXXXXXXX0366\ncardCvc=XXX\ncardExpiry=XXXX\nok"


def test_count_card_numbers_matches_the_hook_gate():
    assert count_card_numbers(f"{VISA[:4]} {VISA[4:8]} {VISA[8:12]} {VISA[12:]}") == 1
    assert count_card_numbers("4111 1111 1111 1111") == 0
    assert count_card_numbers("112-1234567-1234567") == 0
    assert count_card_numbers("****  ****  ****  0366") == 0


# ── route 1: terminal tool result ─────────────────────────────────────────────

def test_terminal_result_is_masked_in_the_transcript_message():
    content = json.dumps({"output": f"Card number: {VISA}\nCVC: 417", "exit_code": 0})
    with _cfg():
        msg = make_tool_result_message("terminal", content, "call_1")
    assert VISA not in msg["content"] and "XXXXXXXXXXXX0366" in msg["content"]
    assert "CVC: XXX" in msg["content"]


def test_terminal_result_untouched_when_knob_off():
    content = json.dumps({"output": f"Card number: {VISA}"})
    with _cfg(mask_card_numbers_tool_results=False):
        msg = make_tool_result_message("terminal", content, "call_1")
    assert VISA in msg["content"]


def test_result_without_card_is_byte_identical():
    content = json.dumps({"output": "order 112-1234567-1234567\nsha 4532015112830366ab"})
    with _cfg():
        assert make_tool_result_message("terminal", content, "call_1")["content"] == content


# ── route 2: image-read result (model wrote the number out of a screenshot) ───

def test_image_read_result_is_masked():
    content = json.dumps({"success": True, "analysis": f"The card reads {MC}, exp 11/28, CVC 913."})
    with _cfg():
        msg = make_tool_result_message("vision_analyze", content, "call_2")
    assert MC not in msg["content"] and "XXXXXXXXXXXX2226" in msg["content"]
    assert "exp XX/XX" in msg["content"] and "CVC XXX" in msg["content"]


def test_multimodal_text_parts_and_summary_are_masked():
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    content = [{"type": "text", "text": f"Amex {AMEX}"}, image]
    with _cfg():
        msg = make_tool_result_message("browser_vision", content, "call_3",
                                       multimodal_text_summary=f"summary {AMEX}")
    flat = json.dumps(msg)
    assert AMEX not in flat and "XXXXXXXXXXX2347" in flat
    assert msg["content"] == [{"type": "text", "text": "Amex XXXXXXXXXXX2347"}, image]
    assert msg["_multimodal_text_summary"] == "summary XXXXXXXXXXX2347"


# ── route 3: inbound user message (separate switch, default off) ──────────────

def _turn(user_message, **cfg):
    from tests.agent.test_turn_context import _FakeAgent, _build
    with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None):
        if cfg:
            with _cfg(**cfg):
                return _build(_FakeAgent(), user_message=user_message)
        return _build(_FakeAgent(), user_message=user_message)


def test_user_message_unmasked_by_default():
    assert redact_cards._KNOB_DEFAULTS["mask_card_numbers_user_messages"] is False
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["security"]["mask_card_numbers_user_messages"] is False
    ctx = _turn(f"my cards: {VISA} and {MC}")
    assert ctx.messages[-1]["content"] == f"my cards: {VISA} and {MC}"


def test_user_message_masked_when_switched_on():
    ctx = _turn(f"my cards: {VISA} and {MC}", mask_card_numbers_user_messages=True)
    assert ctx.messages[-1]["content"] == "my cards: XXXXXXXXXXXX0366 and XXXXXXXXXXXX2226"


def test_config_defaults_carry_all_three_knobs():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    sec = DEFAULT_CONFIG["security"]
    assert sec["mask_card_numbers_tool_results"] is True
    assert sec["browser_vision_card_ocr"] is True
    for name, default in redact_cards._KNOB_DEFAULTS.items():
        assert sec[name] is default


# ── browser_vision screenshot OCR ─────────────────────────────────────────────

def test_screenshot_refusal_uses_the_hook_gate_wording(tmp_path):
    shot = tmp_path / "s.png"
    shot.write_bytes(b"png")
    with _cfg(), patch.object(redact_cards, "_ocr", return_value=(f"Card\n{VISA[:4]} {VISA[4:8]} {VISA[8:12]} {VISA[12:]}", None)):
        assert redact_cards.screenshot_card_refusal(shot) == CARD_IMAGE_REFUSAL
    with _cfg(), patch.object(redact_cards, "_ocr", return_value=("Order 112-1234567-1234567", None)):
        assert redact_cards.screenshot_card_refusal(shot) is None


def test_screenshot_ocr_fails_open_and_obeys_its_knob(tmp_path, caplog):
    shot = tmp_path / "s.png"
    shot.write_bytes(b"png")
    with _cfg(), patch.object(redact_cards, "_ocr", return_value=(None, "tesseract not installed")):
        assert redact_cards.screenshot_card_refusal(shot) is None
    assert "UNCHECKED" in caplog.text
    with _cfg(browser_vision_card_ocr=False), patch.object(redact_cards, "_ocr") as ocr:
        assert redact_cards.screenshot_card_refusal(shot) is None
    ocr.assert_not_called()


def test_screenshot_ocr_runs_the_real_subprocess_path(tmp_path):
    """_ocr through a stand-in tesseract that echoes its stdin: a card in the 'image' refuses."""
    fake = tmp_path / "tesseract"
    fake.write_text("#!/bin/sh\ncat\n")
    fake.chmod(0o755)
    shot = tmp_path / "s.png"
    shot.write_text(f"Card number\n{VISA[:4]} {VISA[4:8]} {VISA[8:12]} {VISA[12:]}\n")
    with _cfg(), patch.object(redact_cards, "TESSERACT", (str(fake),)):
        assert redact_cards.screenshot_card_refusal(shot) == CARD_IMAGE_REFUSAL
        shot.write_text("Order 112-1234567-1234567\n")
        assert redact_cards.screenshot_card_refusal(shot) is None


def _browser_vision(tmp_path, ocr_text):
    from tools.browser_tool import browser_vision
    shots_dir = tmp_path / "browser_screenshots"
    shots_dir.mkdir()
    shot = shots_dir / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    with (
        patch("hermes_constants.get_hermes_dir", return_value=shots_dir),
        patch("tools.browser_tool_lifecycle._cleanup_old_screenshots"),
        patch("tools.browser_tool_session._run_browser_command",
              return_value={"success": True, "data": {"path": str(shot)}}),
        patch("tools.vision_tools._should_use_native_vision_fast_path", return_value=False),
        patch.object(redact_cards, "_ocr", return_value=(ocr_text, None)),
        patch("tools.browser_tool_vision._analyze_screenshot_with_aux_llm", return_value="a checkout page") as aux,
    ):
        result = json.loads(browser_vision("what is on the page?", task_id="test"))
    return result, aux


def test_browser_vision_refuses_a_screenshot_with_a_card_before_any_model(tmp_path):
    result, aux = _browser_vision(tmp_path, f"Pay with\n{MC[:4]} {MC[4:8]} {MC[8:12]} {MC[12:]}\n08/29")
    aux.assert_not_called()
    assert result.get("success") is not True
    assert CARD_IMAGE_REFUSAL in json.dumps(result)
    assert MC not in json.dumps(result)


def test_browser_vision_without_a_card_reaches_the_model(tmp_path):
    result, aux = _browser_vision(tmp_path, "Order 112-1234567-1234567 total $41.00")
    aux.assert_called_once()
    assert result["success"] is True and result["analysis"] == "a checkout page"


# ── Prism round 1 (ee01291296) ───────────────────────────────────────────────

def test_json_escaped_separators_are_recognised_and_left_intact():
    nb = "\u2011"
    raw = json.dumps({"output": f"{VISA[:4]}{nb}{VISA[4:8]}{nb}{VISA[8:12]}{nb}{VISA[12:]}"})
    assert "\\u2011" in raw
    assert json.loads(mask_card_numbers(raw)) == {"output": f"XXXX{nb}XXXX{nb}XXXX{nb}0366"}
    tabbed = json.dumps({"output": "\t".join((VISA[:4], VISA[4:8], VISA[8:12], VISA[12:]))})
    assert json.loads(mask_card_numbers(tabbed)) == {"output": "XXXX\tXXXX\tXXXX\t0366"}


def test_masked_bare_json_numbers_stay_valid_json():
    out = mask_card_numbers('{"cardNumber":%s,"cvv":417, "exp": 1128}' % VISA)
    assert json.loads(out) == {"cardNumber": "XXXXXXXXXXXX0366", "cvv": "XXX", "exp": "XXXX"}
    quoted = mask_card_numbers('{"cardNumber":"%s","cvv":"417"}' % VISA)
    assert json.loads(quoted) == {"cardNumber": "XXXXXXXXXXXX0366", "cvv": "XXX"}


def test_bare_number_inside_a_json_encoded_string_keeps_both_levels_valid():
    inner = '{"cardNumber":%s}' % VISA
    out = mask_card_numbers(json.dumps({"output": inner}))
    assert json.loads(json.loads(out)["output"]) == {"cardNumber": "XXXXXXXXXXXX0366"}
    twice = mask_card_numbers(json.dumps({"o": json.dumps({"x": inner})}))
    assert json.loads(json.loads(json.loads(twice)["o"])["x"]) == {"cardNumber": "XXXXXXXXXXXX0366"}


def test_session_title_input_is_masked_with_the_user_message_switch():
    from tests.agent.test_turn_context import _FakeAgent, _build
    seen = []
    with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None), \
            patch("agent.turn_context._maybe_title_session_at_turn_start",
                  lambda agent, messages, title=None: seen.append(title)):
        with _cfg(mask_card_numbers_user_messages=True):
            _build(_FakeAgent(), user_message=f"card {VISA}", title_user_message=f"card {VISA}")
        with _cfg():
            _build(_FakeAgent(), user_message=f"card {VISA}", title_user_message=f"card {VISA}")
    assert seen == ["card XXXXXXXXXXXX0366", f"card {VISA}"]


def test_numbers_sharing_a_group_are_both_masked():
    a, b = "4000 0000 0004 4000", "4000 0000 0000 0366"  # both Luhn-valid; a's last group is b's first
    assert luhn(a.replace(" ", "")) and luhn(b.replace(" ", ""))
    out = mask_card_numbers(f"cards {a} 0000 0000 0366 end")
    assert out == "cards XXXX XXXX XXXX XXXX XXXX XXXX 0366 end"


def test_full_width_digits_are_masked():
    fw = VISA.translate({ord(c): ord(c) + 0xFEE0 for c in "0123456789"})
    assert mask_card_numbers(f"card {fw}") == "card " + "X" * 12 + fw[-4:]
    amex_fw = AMEX.translate({ord(c): ord(c) + 0xFEE0 for c in "0123456789"})
    assert mask_card_numbers(f"card {amex_fw}") == "card " + "X" * 11 + amex_fw[-4:]


@pytest.mark.parametrize("tool", ["read_file", "search_files"])
def test_file_reads_stay_byte_identical_to_disk(tool):
    content = json.dumps({"content": 'card = "4000 0000 0000 0002"\nother = "%s"' % VISA})
    with _cfg():
        assert make_tool_result_message(tool, content, "call_9")["content"] == content


def test_submit_time_user_row_is_masked_when_switched_on(tmp_path):
    from tui_gateway import session_workdir

    written = []

    class _DB:
        def append_message(self, target, role, content=None, **kw):
            written.append(content)
            return 1

    from contextlib import contextmanager

    @contextmanager
    def _db(_session):
        yield _DB()

    with patch.object(session_workdir, "_session_db", _db), \
            patch.object(session_workdir, "_submit_row_target_key", lambda s: "k"):
        with _cfg(mask_card_numbers_user_messages=True):
            staged = session_workdir._write_submit_user_row({"session_key": "k"}, f"card {VISA}", None)
        with _cfg():
            session_workdir._write_submit_user_row({"session_key": "k"}, f"card {VISA}", None)
    assert written == ["card XXXXXXXXXXXX0366", f"card {VISA}"]
    assert staged["content"] == "card XXXXXXXXXXXX0366"


def test_key_ending_in_a_backslash_keeps_json_valid():
    out = mask_card_numbers(json.dumps({"C:\\": int(VISA)}))
    assert json.loads(out) == {"C:\\": "XXXXXXXXXXXX0366"}
    nested = mask_card_numbers(json.dumps({"o": json.dumps({"C:\\": int(VISA)})}))
    assert json.loads(json.loads(nested)["o"]) == {"C:\\": "XXXXXXXXXXXX0366"}


def test_terminal_spill_file_is_masked_on_disk(tmp_path):
    from tools.terminal_tool_result import _redact_spill_file
    spill = tmp_path / "spill.txt"
    spill.write_text(f"card {VISA}\n", encoding="utf-8")
    with _cfg():
        assert _redact_spill_file(str(spill), 100, "echo") != []
    assert spill.read_text(encoding="utf-8") == "card XXXXXXXXXXXX0366\n"


def test_executor_masks_a_result_before_it_spills():
    from types import SimpleNamespace
    from tests.agent.test_malformed_tool_arguments import _make_agent
    agent = _make_agent()
    call = SimpleNamespace(id="call-1", type="function",
                           function=SimpleNamespace(name="web_search", arguments='{"query": "q"}'))
    spilled = []

    def persist(**kw):
        spilled.append(kw["content"])
        return kw["content"]

    with _cfg(), patch.object(agent, "_invoke_tool", return_value=f"card {VISA}"), \
            patch("model_tools.handle_function_call", return_value=f"card {VISA}"), \
            patch("agent.tool_executor.maybe_persist_tool_result", side_effect=persist):
        agent._execute_tool_calls_sequential(SimpleNamespace(content="", tool_calls=[call]), [], "task-1")
    assert spilled == ["card XXXXXXXXXXXX0366"]


# ── vision_analyze: OCR the bytes it is about to send (t_56a9d083) ────────────

def _png_bytes() -> bytes:
    import struct
    import zlib
    w, h = 8, 4
    rows = b"".join(b"\x00" + b"\xff\xff\xff" * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def _data_url() -> str:
    import base64
    return "data:image/png;base64," + base64.b64encode(_png_bytes()).decode()


class _RemoteEnv:
    """A worker's terminal backend on another host: the file exists only there."""
    def __init__(self):
        self.commands = []

    def execute(self, cmd):
        import base64
        self.commands.append(cmd)
        return {"returncode": 0, "output": base64.b64encode(_png_bytes()).decode()}


def _vision_analyze(tmp_path, image_url, ocr, *, native=False, env=None, **security):
    """Run the vision_analyze handler with the model calls stubbed; ``ocr`` is _ocr's return value."""
    import asyncio
    from unittest.mock import AsyncMock
    from tools import vision_tools
    patches = [
        _cfg(**security),
        patch("tools.vision_tools.get_hermes_dir", return_value=tmp_path),
        patch("tools.vision_tools._should_use_native_vision_fast_path", return_value=native),
        patch("tools.vision_tools._validate_raster_image_decodable", return_value=None),
        patch("tools.vision_tools.async_call_llm", new=AsyncMock(return_value=object())),
        patch("tools.vision_tools.extract_content_or_reasoning", return_value="a receipt"),
        patch("tools.vision_tools._load_auxiliary_client"),
        patch.object(redact_cards, "_ocr", return_value=ocr) if ocr is not None else patch.object(redact_cards, "luhn", redact_cards.luhn),
    ]
    if env is not None:
        patches += [patch("tools.image_source._is_local_terminal_backend", return_value=False),
                    patch("tools.image_source._ensure_container_env"),
                    patch("tools.image_source._get_active_env", return_value=env)]
    from contextlib import ExitStack
    with ExitStack() as stack:
        mocks = [stack.enter_context(p) for p in patches]
        out = asyncio.run(vision_tools._handle_vision_analyze(
            {"image_url": image_url, "question": "what does it say?"}, task_id="t"))
        llm = mocks[4]
    return out, llm


_CARD_OCR = (f"Card number\n{VISA[:4]} {VISA[4:8]} {VISA[8:12]} {VISA[12:]}\nExp 08/29", None)


def test_vision_analyze_refuses_a_data_url_image_with_a_card_before_any_model(tmp_path):
    out, llm = _vision_analyze(tmp_path, _data_url(), _CARD_OCR)
    llm.assert_not_called()
    result = json.loads(out)
    assert result["success"] is False and result["error"] == CARD_IMAGE_REFUSAL
    assert VISA not in out


def test_vision_analyze_refuses_a_remote_terminal_host_image_with_a_card(tmp_path):
    """The path exists only on the worker's terminal host; the bytes come back through env.execute."""
    env = _RemoteEnv()
    for native in (False, True):
        out, llm = _vision_analyze(tmp_path, "/srv/ci/scratch/t_x/shot.png", _CARD_OCR, native=native, env=env)
        llm.assert_not_called()
        assert CARD_IMAGE_REFUSAL in out and VISA not in out
        assert '"_multimodal"' not in out
    assert env.commands and "/srv/ci/scratch/t_x/shot.png" in env.commands[0]


@pytest.mark.parametrize("text", ["Card ending XXXX XXXX XXXX 0366", "Order 112-1234567-1234567 total $41.00"])
def test_vision_analyze_passes_masked_numbers_and_order_numbers(tmp_path, text):
    out, llm = _vision_analyze(tmp_path, _data_url(), (text, None))
    llm.assert_called()
    assert json.loads(out) == {"success": True, "analysis": "a receipt"}
    out, _ = _vision_analyze(tmp_path, _data_url(), (text, None), native=True)
    assert isinstance(out, dict) and out["_multimodal"] is True


def test_vision_analyze_ocr_missing_allows_with_one_warning(tmp_path, caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="agent.redact_cards"):
        out, llm = _vision_analyze(tmp_path, _data_url(), (None, "tesseract not installed"))
    llm.assert_called()
    assert json.loads(out)["success"] is True
    lines = [r.getMessage() for r in caplog.records if "UNCHECKED" in r.getMessage()]
    assert len(lines) == 1 and "image read sent UNCHECKED" in lines[0] and "tesseract not installed" in lines[0]


def test_vision_analyze_card_ocr_obeys_its_own_knob(tmp_path):
    with patch.object(redact_cards, "_ocr") as ocr:
        out, llm = _vision_analyze(tmp_path, _data_url(), None, image_read_card_ocr=False)
    ocr.assert_not_called()
    llm.assert_called()
    # browser_vision's knob does not turn this one off.
    out, llm = _vision_analyze(tmp_path, _data_url(), _CARD_OCR, browser_vision_card_ocr=False)
    assert CARD_IMAGE_REFUSAL in out


def test_vision_analyze_ocr_runs_the_real_subprocess_on_the_sent_bytes(tmp_path):
    """A stand-in tesseract records the bytes it was given and prints a card: the image is refused, and
    the bytes OCRed are exactly the PNG the tool would have sent."""
    seen = tmp_path / "seen.bin"
    fake = tmp_path / "tesseract"
    fake.write_text(f"#!/bin/sh\ncat > {shlex.quote(str(seen))}\nprintf 'Card\\n{VISA[:4]} {VISA[4:8]} {VISA[8:12]} {VISA[12:]}\\n'\n")
    fake.chmod(0o755)
    with patch.object(redact_cards, "TESSERACT", (str(fake),)):
        out, llm = _vision_analyze(tmp_path, _data_url(), None)
    llm.assert_not_called()
    assert CARD_IMAGE_REFUSAL in out
    assert seen.read_bytes() == _png_bytes()


def test_vision_analyze_real_ocr_on_a_rendered_card_image(tmp_path):
    """Real tesseract on a rendered image of a made-up Luhn-valid number (skipped where OCR is absent)."""
    import os
    pil = pytest.importorskip("PIL.Image")
    from PIL import ImageDraw, ImageFont
    if not any(os.access(b, os.X_OK) for b in redact_cards.TESSERACT):
        pytest.skip("tesseract not installed")
    import base64
    import io
    im = pil.new("RGB", (900, 160), "white")
    try:
        font = ImageFont.load_default(size=48)
    except TypeError:
        pytest.skip("Pillow too old for a sized default font")
    ImageDraw.Draw(im).text((20, 50), f"{VISA[:4]} {VISA[4:8]} {VISA[8:12]} {VISA[12:]}", fill="black", font=font)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    out, llm = _vision_analyze(tmp_path, url, None)
    llm.assert_not_called()
    assert CARD_IMAGE_REFUSAL in out


def _rgba_card_url(alpha: int, size=(1800, 200)):
    """A PNG whose RGB shows the card; ``alpha`` 0 hides it from a white-composited view (Prism 77de86bbc303)."""
    import base64
    import io
    pil = pytest.importorskip("PIL.Image")
    from PIL import ImageDraw, ImageFont
    try:
        font = ImageFont.load_default(size=60)
    except TypeError:
        pytest.skip("Pillow too old for a sized default font")
    im = pil.new("RGB", size, "white")
    ImageDraw.Draw(im).text((40, 60), f"{VISA[:4]}   {VISA[4:8]}   {VISA[8:12]}   {VISA[12:]}", fill="black", font=font)
    im = im.convert("RGBA")
    im.putalpha(alpha)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def test_vision_analyze_ocrs_the_alpha_dropped_view_of_a_transparent_image(tmp_path):
    """The JPEG resize drops alpha (convert("RGB")): OCR clean on the composited view, card under the alpha."""
    seen = []

    def ocr(path):
        seen.append(path)
        return ("", None) if len(seen) % 2 else _CARD_OCR
    for native in (False, True):
        with patch.object(redact_cards, "_ocr", side_effect=ocr):
            out, llm = _vision_analyze(tmp_path, _rgba_card_url(0), None, native=native)
        llm.assert_not_called()
        assert CARD_IMAGE_REFUSAL in out and '"_multimodal"' not in out
    assert len(seen) == 4 and seen[1].endswith(".rgb.png") and seen[3].endswith(".rgb.png")
    assert not list(tmp_path.glob("*.rgb.png"))


def test_vision_analyze_opaque_image_is_ocred_once(tmp_path):
    with patch.object(redact_cards, "_ocr", return_value=("receipt", None)) as ocr:
        out, llm = _vision_analyze(tmp_path, _rgba_card_url(255, (400, 100)), None, native=True)
    assert ocr.call_count == 1 and out["_multimodal"] is True


def test_vision_analyze_real_ocr_refuses_a_card_hidden_under_transparency(tmp_path):
    """Prism repro with real tesseract: 1800x200 RGBA, alpha 0, native path (it resizes to JPEG)."""
    import os
    if not any(os.access(b, os.X_OK) for b in redact_cards.TESSERACT):
        pytest.skip("tesseract not installed")
    out, llm = _vision_analyze(tmp_path, _rgba_card_url(0), None, native=True)
    assert CARD_IMAGE_REFUSAL in out and '"_multimodal"' not in out


def _trns_card_png(path):
    """RGB PNG: black made-up card digits on white, with black marked transparent (a tRNS colour key)."""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGB", (1800, 200), "white")
    try:
        font = ImageFont.load_default(size=60)
    except TypeError:
        pytest.skip("Pillow too old for a sized default font")
    ImageDraw.Draw(im).text((40, 60), f"{VISA[:4]}   {VISA[4:8]}   {VISA[8:12]}   {VISA[12:]}", fill="black", font=font)
    im.save(path, format="PNG", transparency=(0, 0, 0))


def test_alpha_dropped_copy_drops_a_trns_colour_key(tmp_path):
    pytest.importorskip("PIL.Image")
    from PIL import Image
    from tools.vision_tools import _alpha_dropped_copy
    src = tmp_path / "trns.png"
    _trns_card_png(src)
    out = _alpha_dropped_copy(src)
    assert out is not None
    with Image.open(out) as im:
        assert im.mode == "RGB" and "transparency" not in im.info



# ── t_c497904d: linear time, JSON arrays/root, flattened copy lifetime ───────

def _old_window(text, start, end):
    """``_window`` as it was before t_c497904d (rescans from the start of the text per card)."""
    line_start = 0
    for m in redact_cards._LINE_BREAK.finditer(text, 0, start):
        line_start = m.end()
    stop, breaks = len(text), 0
    for m in redact_cards._LINE_BREAK.finditer(text, end):
        breaks += 1
        if breaks == 3:
            stop = m.start()
            break
    return line_start, stop


def _card_rows(n, seed=7):
    """n rows, each with a made-up Luhn-valid Visa number: plain table rows (CRLF) and JSON-escaped rows."""
    import random
    rng = random.Random(seed)

    def card():
        body = "4" + "".join(rng.choice("0123456789") for _ in range(14))
        return next(body + d for d in "0123456789" if luhn(body + d))
    return "".join(
        "order %06d  customer name here  card %s  cvv %03d  exp 08/29  ok\r\n" % (i, card(), rng.randrange(1000))
        if i % 3 else '{\\"order\\": %d, \\"card\\": %s}\\n  cvc: %03d\n' % (i, card(), rng.randrange(1000))
        for i in range(n))


def test_mask_is_linear_and_matches_the_old_windowing():
    import time
    sample = _card_rows(500)
    with patch.object(redact_cards, "_window", lambda text, s, e, breaks=None: _old_window(text, s, e)):
        old = mask_card_numbers(sample)
    assert old != sample and mask_card_numbers(sample) == old
    assert mask_card_numbers(_card_rows(20000, seed=8)).count("XXXXXXXXXXXX") == 20000


def _best_of_3(fn, arg):
    import time
    best = None
    for _ in range(3):
        t0 = time.perf_counter()
        fn(arg)
        took = time.perf_counter() - t0
        best = took if best is None else min(best, took)
    return best


def _assert_scales_linearly(fn, small, big, what):
    """Best of 3 at 4x the input under 10x the best of 3 at 1x (linear ~4x; the old quadratic code ~16x).
    A ratio, not seconds: a loaded runner slows both sides (t_110f649d item 7)."""
    t1, t4 = _best_of_3(fn, small), _best_of_3(fn, big)
    assert t4 < 10 * t1, "%s: 4x input took %.1fx the time (%.3f s vs %.3f s)" % (what, t4 / t1, t4, t1)


def test_card_rows_mask_scales_linearly():
    _assert_scales_linearly(mask_card_numbers, _card_rows(5000, seed=8), _card_rows(20000, seed=8), "card rows")


def test_masked_bare_json_numbers_in_arrays_and_at_the_root_stay_valid_json():
    m = "XXXXXXXXXXXX0366"
    assert json.loads(mask_card_numbers('{"cards":[%s]}' % VISA)) == {"cards": [m]}
    assert json.loads(mask_card_numbers('{"cards":[1, %s, 5]}' % VISA)) == {"cards": [1, m, 5]}
    assert json.loads(mask_card_numbers(json.dumps({"cards": [int(VISA)] * 2}, indent=2))) == {"cards": [m, m]}
    assert json.loads(mask_card_numbers(VISA)) == m
    assert json.loads(mask_card_numbers(VISA + "\n")) == m
    assert json.loads(mask_card_numbers(json.dumps(["say \"hi\"", int(VISA)]))) == ["say \"hi\"", m]
    # Inside a JSON-encoded string nothing is added: the array item is only X'd, one and two levels deep.
    for inner in ('{"cards":[%s]}' % VISA, "[%s]" % VISA, json.dumps([int(VISA)], indent=2)):
        out = json.loads(mask_card_numbers(json.dumps({"output": inner})))["output"]
        assert out == inner.replace(VISA, m)
    twice = mask_card_numbers(json.dumps({"o": json.dumps({"x": "[%s]" % VISA})}))
    assert json.loads(json.loads(twice)["o"])["x"] == "[%s]" % m
    # Not a JSON number: a spaced card at the root stays unquoted.
    assert mask_card_numbers(f"{VISA[:4]} {VISA[4:8]} {VISA[8:12]} {VISA[12:]}") == "XXXX XXXX XXXX 0366"


def test_cancelled_image_read_leaves_no_flattened_copy(tmp_path):
    """The alpha-dropped ``.rgb.png`` copy shows the unmasked card: a read cancelled while the copy exists
    must still delete it."""
    import asyncio
    import threading
    from tools import vision_tools
    url = _rgba_card_url(0, (400, 100))
    made, release = threading.Event(), threading.Event()
    real = vision_tools._alpha_dropped_copy

    def copy_then_wait(path):
        out = real(path)
        made.set()
        release.wait(10)
        return out

    async def run():
        task = asyncio.create_task(vision_tools._prepare_image(url, "t", None, validate_decode=False))
        assert await asyncio.to_thread(made.wait, 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert list(tmp_path.glob("*.rgb.png")), "the copy was not made before the cancel"
        release.set()
        # asyncio.run joins the worker thread on exit, so the copy's fate is settled when it returns.

    with _cfg(), patch("tools.vision_tools.get_hermes_dir", return_value=tmp_path), \
            patch.object(vision_tools, "_alpha_dropped_copy", copy_then_wait), \
            patch.object(redact_cards, "_ocr", return_value=("", None)):
        asyncio.run(run())
    assert not list(tmp_path.glob("*.rgb.png"))


def test_array_quotes_only_for_number_tokens_outside_every_string():
    """Prism f224ce988f0c / 800ec6141d53: text inside a JSON string gets X's and nothing else; a bare number
    is quoted only when it is a token of a valid JSON document, outside every string."""
    m = "XXXXXXXXXXXX0366"
    # CSV rows inside a terminal result decode to the text with only the digits X'd.
    for output in (",%s,approved" % VISA, "id,card\n7,%s,ok\n" % VISA, "a,%s,b" % VISA, "[x,%s,y" % VISA,
                   "cards seen: [%s, %s]" % (VISA, VISA)):
        got = json.loads(mask_card_numbers(json.dumps({"output": output, "exit_code": 0})))
        assert got == {"output": output.replace(VISA, m), "exit_code": 0}
    # One document with a comma-first CSV string AND a bare number array: valid, array item quoted.
    doc = '{"output":",%s,approved","cards":[%s]}' % (VISA, VISA)
    assert json.loads(mask_card_numbers(doc)) == {"output": ",%s,approved" % m, "cards": [m]}
    # A top-level array and a lone root number are quoted.
    assert json.loads(mask_card_numbers(json.dumps([int(VISA), int(MC)]))) == [m, "XXXXXXXXXXXX2226"]
    assert json.loads(mask_card_numbers(" %s\n" % VISA)) == m
    # Not JSON: no quotes (prose with a bracketed list, a bare number on a line of prose).
    assert mask_card_numbers("cards seen: [%s, %s]" % (VISA, VISA)) == "cards seen: [%s, %s]" % (m, m)
    assert mask_card_numbers("%s\nnext line" % VISA) == m + "\nnext line"
    # A number token that is more than the card (a negative or a fraction): the whole token is quoted.
    assert json.loads(mask_card_numbers("[-%s]" % VISA)) == ["-" + m]


def test_many_cards_on_one_line_mask_in_linear_time():
    """Prism b327b5c2835b: every card on one line shared the whole line as its window."""
    import random
    import time
    rng = random.Random(11)

    def cards(n):
        out = []
        while len(out) < n:
            body = "4" + "".join(rng.choice("0123456789") for _ in range(14))
            out.append(int(next(body + d for d in "0123456789" if luhn(body + d))))
        return out
    sample = json.dumps({"ids": cards(300), "cvc": 123, "exp": "08/29"})
    with patch.object(redact_cards, "_merge", lambda windows: windows):
        per_card = mask_card_numbers(sample)
    assert per_card != sample and mask_card_numbers(sample) == per_card
    out = mask_card_numbers(json.dumps(cards(20000)))
    assert len(json.loads(out)) == 20000 and out.count("XXXXXXXXXXXX") == 20000


def test_one_line_cards_mask_scales_linearly():
    import random
    rng = random.Random(11)

    def cards(n):
        out = []
        while len(out) < n:
            body = "4" + "".join(rng.choice("0123456789") for _ in range(14))
            out.append(int(next(body + d for d in "0123456789" if luhn(body + d))))
        return out
    _assert_scales_linearly(mask_card_numbers, json.dumps(cards(5000)), json.dumps(cards(20000)),
                            "cards on one line")


# ── t_110f649d: the six paths PR 596 did not cover ───────────────────────────
# Made-up Luhn-valid Discover number for this block (last four 0993, not 1120).
DISC = "6011000990130993"


def test_disc_fixture_is_luhn_valid_and_masked():
    assert luhn(DISC) and DISC not in redact_cards.PLACEHOLDERS and not DISC.endswith("1120")
    assert mask_card_numbers(f"card {DISC}") == "card XXXXXXXXXXXX0993"


def _camofox_vision(tmp_path, ocr_text):
    from tools import browser_camofox
    shot = tmp_path / "camofox.png"
    llm = []
    with (
        _cfg(),
        patch.object(browser_camofox, "_require_tab", return_value=({"tab_id": "t", "user_id": "u"}, None)),
        patch.object(browser_camofox, "_get_raw", return_value=SimpleNamespace(content=b"\x89PNG")),
        patch.object(browser_camofox, "_save_screenshot", side_effect=lambda c: (shot.write_bytes(c), str(shot))[1]),
        patch.object(redact_cards, "_ocr", return_value=(ocr_text, None)),
        patch("agent.auxiliary_client.call_llm", side_effect=lambda **kw: llm.append(kw) or SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="a checkout page"))])),
    ):
        result = json.loads(browser_camofox.camofox_vision("what is on the page?", task_id="test"))
    return result, llm


def test_camofox_vision_refuses_a_card_screenshot_before_any_model(tmp_path):
    """Path 1: browser_vision in camofox mode returns before browser_tool's OCR; camofox_vision checks itself."""
    result, llm = _camofox_vision(tmp_path, f"Pay with\n{DISC[:4]} {DISC[4:8]} {DISC[8:12]} {DISC[12:]}")
    assert llm == []
    assert result.get("success") is False and CARD_IMAGE_REFUSAL in json.dumps(result)
    result, llm = _camofox_vision(tmp_path, "Order 112-1234567-1234567")
    assert len(llm) == 1 and result["success"] is True


def test_gateway_progress_preview_and_args_are_masked():
    """Path 2: tool.started preview/args become a chat message on a third-party service."""
    import queue
    from gateway.config import Platform
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext
    ctx = TurnContext(source=SimpleNamespace(chat_id="c", platform=Platform.DISCORD), progress_queue=queue.Queue(),
                      _run_still_current=lambda: True)
    ctx.tool_progress_enabled, ctx.progress_mode = True, "verbose"
    tr = TurnRunner(SimpleNamespace(_delivery_adapter_for=lambda source: None, _session_initiated_restart={}), ctx)
    with _cfg():
        tr.progress_callback("tool.started", "browser_type", f"type {DISC}", {"ref": "@e3", "text": DISC})
        tr.progress_callback("tool.started", "web_search", f"q {DISC}", None)
    sent = []
    while not ctx.progress_queue.empty():
        sent.append(str(ctx.progress_queue.get()))
    assert sent and DISC not in "".join(sent)
    assert "XXXXXXXXXXXX0993" in "".join(sent)


def test_synthetic_error_tool_rows_are_masked():
    """Path 3: the validation/mixed-batch rows (one helper) and the outer-loop error rows."""
    from agent.turn_tool_validation import _append_tool_error_results
    call = SimpleNamespace(id="call-1", type="function", function=SimpleNamespace(name="web_search", arguments="{}"))
    rows = []
    with _cfg():
        _append_tool_error_results(rows, [call], lambda tc: f"Error: bad args {DISC}")
    assert rows[0]["content"] == "Error: bad args XXXXXXXXXXXX0993"

    from agent import turn_loop_errors
    agent = SimpleNamespace(suppress_status_output=True, max_iterations=10, _safe_print=lambda *a, **k: None)
    messages = [{"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "web_search"}}]}]
    with _cfg():
        try:
            turn_loop_errors.handle_outer_loop_error(
                agent, e=RuntimeError(f"provider echoed {DISC}"), _outer_error_count=0, api_call_count=1,
                messages=messages, conversation_history=[], _turn_exit_reason=None, failed=False, final_response=None)
        except Exception:
            pass  # only the synthetic row matters here; the rest of the handler needs a real agent
    tool_rows = [m for m in messages if m.get("role") == "tool"]
    assert tool_rows and DISC not in tool_rows[0]["content"] and "XXXXXXXXXXXX0993" in tool_rows[0]["content"]


def test_multimodal_text_part_is_masked_before_it_spills():
    """Path 4: _persist_multimodal_text_parts writes text parts to disk; the mask runs first."""
    from agent.tool_executor import _persist_multimodal_text_parts
    spilled = []

    def persist(**kw):
        spilled.append(kw["content"])
        return "<spilled to /tmp/x>"

    envelope = {"content": [{"type": "text", "text": f"stdout card {DISC}"}, {"type": "image_url", "image_url": {}}]}
    budget = SimpleNamespace(resolve_threshold=lambda name: 10)
    with _cfg(), patch("agent.tool_executor.maybe_persist_tool_result", side_effect=persist):
        out = _persist_multimodal_text_parts(envelope, "browser_exec", "call-1", None, budget)
    assert spilled == ["stdout card XXXXXXXXXXXX0993"]
    assert out["content"][0]["text"] == "<spilled to /tmp/x>"
    # Masked but under the threshold (no spill): the part still carries the masked text.
    with _cfg(), patch("agent.tool_executor.maybe_persist_tool_result", side_effect=lambda **kw: kw["content"]):
        out = _persist_multimodal_text_parts(envelope, "browser_exec", "call-1", None, budget)
    assert out["content"][0]["text"] == "stdout card XXXXXXXXXXXX0993"


def test_submit_row_adoption_does_not_rewrite_the_card_back(tmp_path):
    """Path 5 (Prism e847bb92b9cf): the row written masked must not be rewritten unmasked at adoption."""
    from contextlib import contextmanager
    from tui_gateway import session_workdir
    rewrites = []

    class _DB:
        def append_message(self, target, role, content=None, **kw):
            return 7

        def set_user_message_content(self, key, row_id, content):
            rewrites.append(content)

    @contextmanager
    def _db(_session):
        yield _DB()

    agent = SimpleNamespace(_pending_cli_user_message=None)
    raw = f"card {DISC}"
    with patch.object(session_workdir, "_session_db", _db), \
            patch.object(session_workdir, "_submit_row_target_key", lambda s: "k"), \
            _cfg(mask_card_numbers_user_messages=True):
        session = {"session_key": "k"}
        session_workdir._persist_submit_user_row(session, raw, None)
        session_workdir._adopt_submit_user_row(session, agent, raw, raw)
        session_workdir._persist_submit_user_row(session, raw, None)
        session_workdir._adopt_submit_user_row(session, agent, f"@file expanded\n{raw}", raw)
    assert rewrites == ["@file expanded\ncard XXXXXXXXXXXX0993"]
    assert agent._pending_cli_user_message["content"] == "@file expanded\ncard XXXXXXXXXXXX0993"


def test_codex_mcp_tool_results_are_masked():
    """Path 6 (Prism 875afa3a8d9a): hermes-tools MCP results reach Codex without make_tool_result_message."""
    import sys
    import types
    from agent.transports import hermes_tools_mcp_server as srv
    handlers = {}

    class _FakeMCP:
        def __init__(self, *a, **k):
            pass

        def add_tool(self, fn, name=None, description=None):
            handlers[name] = fn

    defs = [{"type": "function", "function": {"name": "vision_analyze", "parameters": {"type": "object", "properties": {
        "question": {"type": "string"}}}}}]
    fake_server = types.ModuleType("mcp.server")
    fake_server.MCPServer = _FakeMCP
    with patch.dict(sys.modules, {"mcp": types.ModuleType("mcp"), "mcp.server": fake_server}), \
            patch("model_tools.get_tool_definitions", return_value=defs), \
            patch("model_tools.handle_function_call", return_value=json.dumps({"analysis": f"card {DISC}"})), _cfg():
        srv._build_server()
        out = handlers["vision_analyze"](question="what card?")
    assert DISC not in out and "XXXXXXXXXXXX0993" in out


# ── t_110f649d Prism round 1 (#602 @862329a1d80d) ────────────────────────────


def _runner(platform_name="DISCORD"):
    import queue
    from gateway.config import Platform
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext
    ctx = TurnContext(source=SimpleNamespace(chat_id="c", platform=getattr(Platform, platform_name)),
                      progress_queue=queue.Queue(), _run_still_current=lambda: True)
    runner = SimpleNamespace(_delivery_adapter_for=lambda source: None, _session_initiated_restart={},
                             _deliver_platform_notice=lambda source, line: line)
    return TurnRunner(runner, ctx), ctx


def _drain(ctx):
    sent = []
    while not ctx.progress_queue.empty():
        sent.append(json.dumps(ctx.progress_queue.get(), default=str))
    return "".join(sent)


def test_slack_native_task_card_title_is_masked():
    """Prism 119a6de996ea: native task cards build their title from args in tool_start_callback."""
    tr, ctx = _runner("SLACK")
    with _cfg():
        tr.native_tool_start_callback("call-1", "browser_type", {"ref": "@e3", "text": DISC})
    sent = _drain(ctx)
    assert sent and DISC not in sent and "XXXXXXXXXXXX0993" in sent


def test_compact_progress_does_not_rejoin_a_line_split_card():
    """Prism edad8432494d: the preview collapses newlines, which rejoins a card the per-line mask missed."""
    split = "\n".join((DISC[:4], DISC[4:8], DISC[8:12], DISC[12:]))
    joined = " ".join((DISC[:4], DISC[4:8], DISC[8:12], DISC[12:]))
    tr, ctx = _runner()
    ctx.tool_progress_enabled, ctx.progress_mode = True, "all"
    with _cfg():
        args = {"ref": "@e3", "text": split}
        from agent.display import build_tool_preview
        tr.progress_callback("tool.started", "browser_type", build_tool_preview("browser_type", args), args)
    sent = _drain(ctx)
    assert sent and joined not in sent and "XXXX XXXX XXXX 0993" in sent


def test_subagent_failure_notice_masks_goal_and_summary():
    """Prism 3313bd690cea: the failure notice prefers kwargs summary over preview and renders the goal."""
    tr, ctx = _runner()
    notices = []
    tr._schedule = lambda line, _msg: notices.append(line)
    with _cfg(), patch("gateway.warning_notifications.warning_notifications_enabled", return_value=True):
        tr.progress_callback("subagent.complete", preview=f"card {DISC}", status="failed",
                             summary=f"card {DISC}", goal=f"pay with {DISC}")
    assert notices and DISC not in "".join(notices) and "XXXXXXXXXXXX0993" in "".join(notices)


def test_codex_mcp_exception_response_is_masked():
    """Prism c5d4f1214b21: the except branch serializes str(exc) without the result mask."""
    import sys
    import types
    from agent.transports import hermes_tools_mcp_server as srv
    handlers = {}

    class _FakeMCP:
        def __init__(self, *a, **k):
            pass

        def add_tool(self, fn, name=None, description=None):
            handlers[name] = fn

    defs = [{"type": "function", "function": {"name": "vision_analyze", "parameters": {"type": "object", "properties": {
        "question": {"type": "string"}}}}}]
    fake_server = types.ModuleType("mcp.server")
    fake_server.MCPServer = _FakeMCP
    with patch.dict(sys.modules, {"mcp": types.ModuleType("mcp"), "mcp.server": fake_server}), \
            patch("model_tools.get_tool_definitions", return_value=defs), \
            patch("model_tools.handle_function_call", side_effect=RuntimeError(f"vision echoed card {DISC}")), _cfg():
        srv._build_server()
        out = handlers["vision_analyze"](question="what card?")
    assert DISC not in out and "XXXXXXXXXXXX0993" in out


def test_camofox_annotated_vision_masks_the_accessibility_tree(tmp_path):
    """Prism ee28670894fb: annotate=True sends the page's accessibility text along with the screenshot."""
    from tools import browser_camofox
    shot = tmp_path / "camofox.png"
    llm = []
    with (
        _cfg(),
        patch.object(browser_camofox, "_require_tab", return_value=({"tab_id": "t", "user_id": "u"}, None)),
        patch.object(browser_camofox, "_get_raw", return_value=SimpleNamespace(content=b"\x89PNG")),
        patch.object(browser_camofox, "_save_screenshot", side_effect=lambda c: (shot.write_bytes(c), str(shot))[1]),
        patch.object(browser_camofox, "_snapshot_data", return_value={"snapshot": f'textbox "Payment number": {DISC}'}),
        patch.object(redact_cards, "_ocr", return_value=("Checkout", None)),
        patch("agent.auxiliary_client.call_llm", side_effect=lambda **kw: llm.append(kw) or SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="a checkout page"))])),
    ):
        browser_camofox.camofox_vision("What is on this page?", annotate=True, task_id="test")
    sent = json.dumps(llm)
    assert len(llm) == 1 and DISC not in sent and "XXXXXXXXXXXX0993" in sent


# ── t_110f649d Prism round 2 (#602 @cd0fa7c7f80a) ────────────────────────────


def test_progress_callback_never_scans_a_completed_tool_result():
    """Prism edad8432494d: tool.completed carries the raw result, which no progress path renders."""
    tr, ctx = _runner()
    scanned = []
    real = redact_cards.mask_card_numbers
    with _cfg(), patch.object(redact_cards, "mask_card_numbers", side_effect=lambda t: scanned.append(t) or real(t)):
        tr.progress_callback("tool.completed", "terminal", None, None, duration=1.0, is_error=False,
                             result=f"stdout card {DISC}")
    assert not any(DISC in t for t in scanned)


def test_verbose_progress_masks_a_card_given_as_a_json_integer():
    """Prism 172d24d70731: the display walker skipped ints; verbose progress prints the args JSON."""
    tr, ctx = _runner()
    ctx.tool_progress_enabled, ctx.progress_mode = True, "verbose"
    with _cfg():
        tr.progress_callback("tool.started", "mcp_checkout_pay", "pay", {"card": int(DISC), "qty": 1234567890123456})
    sent = _drain(ctx)
    assert sent and DISC not in sent and "XXXXXXXXXXXX0993" in sent and "1234567890123456" in sent


def test_codex_mcp_native_vision_envelope_is_masked():
    """Prism c5d4f1214b21: native vision_analyze returns a dict envelope whose text part echoes the question."""
    import sys
    import types
    from agent.transports import hermes_tools_mcp_server as srv
    from tools.vision_tools import _build_native_vision_tool_result
    handlers = {}

    class _FakeMCP:
        def __init__(self, *a, **k):
            pass

        def add_tool(self, fn, name=None, description=None):
            handlers[name] = fn

    defs = [{"type": "function", "function": {"name": "vision_analyze", "parameters": {"type": "object", "properties": {
        "question": {"type": "string"}}}}}]
    fake_server = types.ModuleType("mcp.server")
    fake_server.MCPServer = _FakeMCP
    envelope = _build_native_vision_tool_result("x.png", f"is {DISC} on it?", "data:image/png;base64,AA", 2)
    with patch.dict(sys.modules, {"mcp": types.ModuleType("mcp"), "mcp.server": fake_server}), \
            patch("model_tools.get_tool_definitions", return_value=defs), \
            patch("model_tools.handle_function_call", return_value=envelope), _cfg():
        srv._build_server()
        out = handlers["vision_analyze"](question="what card?")
    sent = json.dumps(out)
    assert DISC not in sent and "XXXXXXXXXXXX0993" in sent and out["_multimodal"] is True


def test_rejected_tool_name_in_a_synthetic_row_is_masked():
    """Prism 771038845f0e: the error row copied the model-supplied (invalid) tool name verbatim."""
    from agent.turn_tool_validation import _append_tool_error_results
    call = SimpleNamespace(id="call-1", type="function", function=SimpleNamespace(name=DISC, arguments="{}"))
    rows = []
    with _cfg():
        _append_tool_error_results(rows, [call], lambda tc: "Error: unknown tool")
    # The whole name is a JSON number token, so the masker quotes it (PR 604); either way no digits remain.
    assert DISC not in rows[0]["name"] and rows[0]["name"].strip('"') == "XXXXXXXXXXXX0993"


def test_terminal_outer_loop_error_reply_is_masked():
    """Prism f50f62a3c37b: the terminal branch's chat reply and exit reason came from the raw exception."""
    from agent import turn_loop_errors
    agent = SimpleNamespace(suppress_status_output=True, max_iterations=10, _safe_print=lambda *a, **k: None)
    messages = [{"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "web_search"}}]}]
    with _cfg(), patch("agent.conversation_loop._ra", return_value=SimpleNamespace(AIAgent=SimpleNamespace(
            _get_tool_call_name_static=lambda tc: "web_search"))):
        out = turn_loop_errors.handle_outer_loop_error(
            agent, e=RuntimeError(f"provider echoed {DISC}"), _outer_error_count=0, api_call_count=9,
            messages=messages, conversation_history=[], _turn_exit_reason=None, failed=False, final_response=None)
    text = json.dumps(out, default=str) + json.dumps(messages)
    assert DISC not in text and "XXXXXXXXXXXX0993" in text


# ── t_110f649d operator send-back (#602 @aef821979e83) ──────────────────────


def _split_disc(sep="\n"):
    return sep.join((DISC[:4], DISC[4:8], DISC[8:12], DISC[12:]))


_PROSE = "the order page shows a shipping form and a billing form with several fields to fill in. " * 27


@pytest.mark.parametrize("prefix", ["\n" * 2048, "\n" * 4096, _PROSE], ids=["2048nl", "4096nl", "2.3KB-prose"])
@pytest.mark.parametrize("mode", ["all", "verbose"])
def test_split_card_anywhere_in_an_arg_is_masked_in_progress(prefix, mode):
    """Prism 172d24d70731: the split-card check stopped at 2,048 characters; the compact preview collapses
    whitespace and verbose progress dumps the args with escaped line breaks, both rejoining the card."""
    import re
    assert len(_PROSE) > 2300
    tr, ctx = _runner()
    ctx.tool_progress_enabled, ctx.progress_mode = True, mode
    args = {"ref": "@e3", "text": prefix + _split_disc()}
    with _cfg():
        from agent.display import build_tool_preview
        tr.progress_callback("tool.started", "browser_type", build_tool_preview("browser_type", args, max_len=0), args)
    sent = _drain(ctx)
    assert sent and DISC not in re.sub(r"\\\\[nrt]|\\[nrt]|\s", "", sent)
    masked = redact_cards.mask_tool_display_value(args)
    assert DISC not in re.sub(r"\s", "", json.dumps(masked)) and "XXXX XXXX XXXX 0993" in masked["text"]
    assert args["text"].endswith(_split_disc()), "the dispatched args are never touched"


def test_multi_line_command_without_a_card_keeps_its_line_breaks():
    cmd = "\n".join("echo step %d done" % i for i in range(30))
    with _cfg():
        assert redact_cards.mask_tool_display_value({"command": cmd})["command"].count("\n") == 29


def test_display_mask_scales_linearly_on_a_numeric_column():
    """A 100 KB argument (a column of 4-digit numbers) against 25 KB: linear, not quadratic."""
    import random
    rng = random.Random(5)

    def column(kb):
        return {"content": "\n".join("%04d" % rng.randrange(10000) for _ in range(kb * 1024 // 5))}
    with _cfg():
        _assert_scales_linearly(redact_cards.mask_tool_display_value, column(25), column(100), "display column")


def test_display_walker_masks_a_whole_number_float():
    """Prism e5227933383e: an argument decoded from <card>.0 is a float; ints were masked, floats not."""
    with _cfg():
        out = redact_cards.mask_tool_display_value({"card": float(DISC), "price": 1234567890123456.0, "x": 1.5})
    dumped = json.dumps(out)
    assert DISC not in dumped and "XXXXXXXXXXXX0993" in dumped
    assert out["price"] == 1234567890123456.0 and out["x"] == 1.5


def test_terminal_error_reply_masks_before_the_clip():
    """Prism 6c34937e0f11: short_detail clipped to 199 characters, then the mask missed a card cut at its
    last digit and the chat reply kept 15 clear digits."""
    from agent import turn_loop_errors
    agent = SimpleNamespace(suppress_status_output=True, max_iterations=10, _safe_print=lambda *a, **k: None)
    messages = [{"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "web_search"}}]}]
    exc = RuntimeError("a " * 92 + DISC + " declined")
    assert len(str(exc)[:199]) == 199 and DISC[:15] in str(exc)[:199]
    with _cfg(), patch("agent.conversation_loop._ra", return_value=SimpleNamespace(AIAgent=SimpleNamespace(
            _get_tool_call_name_static=lambda tc: "web_search"))):
        out = turn_loop_errors.handle_outer_loop_error(
            agent, e=exc, _outer_error_count=0, api_call_count=9,
            messages=messages, conversation_history=[], _turn_exit_reason=None, failed=False, final_response=None)
    text = json.dumps(out, default=str) + json.dumps(messages)
    assert DISC[:15] not in text and "XXXXXXXXXXXX" in text


def test_codex_mcp_native_vision_meta_is_masked():
    """Prism c5d4f1214b21: meta carries annotation labels (page text) and the caller's image_url."""
    import sys
    import types
    from agent.transports import hermes_tools_mcp_server as srv
    from tools.vision_tools import _build_native_vision_tool_result
    handlers = {}

    class _FakeMCP:
        def __init__(self, *a, **k):
            pass

        def add_tool(self, fn, name=None, description=None):
            handlers[name] = fn

    defs = [{"type": "function", "function": {"name": "browser_vision", "parameters": {"type": "object",
             "properties": {"question": {"type": "string"}}}}}]
    fake_server = types.ModuleType("mcp.server")
    fake_server.MCPServer = _FakeMCP
    envelope = _build_native_vision_tool_result("https://shop.test/c/%s.png" % DISC, "what is on the page?",
                                                "data:image/png;base64,AA", 2)
    envelope["meta"]["annotations"] = [{"id": 1, "label": "Card %s" % DISC}]
    with patch.dict(sys.modules, {"mcp": types.ModuleType("mcp"), "mcp.server": fake_server}), \
            patch("model_tools.get_tool_definitions", return_value=defs), \
            patch("model_tools.handle_function_call", return_value=envelope), _cfg():
        srv._build_server()
        out = handlers["browser_vision"](question="what is on the page?")
    sent = json.dumps(out)
    assert DISC not in sent and out["meta"]["annotations"][0]["label"] == "Card XXXXXXXXXXXX0993"
    assert out["meta"]["size_bytes"] == 2 and out["content"][1] == envelope["content"][1]


def test_colon_first_string_value_stays_valid_json():
    """Prism 1aa4cb0d2a7a: '":<card>,' inside a top-level string looked like a key and got RAW quotes."""
    doc = '{"output":":%s,approved","exit_code":0}' % DISC
    assert json.loads(mask_card_numbers(doc)) == {"output": ":XXXXXXXXXXXX0993,approved", "exit_code": 0}
    # A real key outside every string is still quoted.
    assert json.loads(mask_card_numbers('{"card":%s,"ok":1}' % DISC)) == {"card": "XXXXXXXXXXXX0993", "ok": 1}


@pytest.mark.parametrize("shape", ["[%s.0]", '{"n": %s.0}', "[-%s]"])
def test_sign_and_fraction_on_a_bare_number_stay_valid_json(shape):
    """Item 8: the quotes go around the whole number token (minus, fraction), card digits masked inside."""
    doc = shape % DISC
    json.loads(doc)
    out = mask_card_numbers(doc)
    assert DISC not in out
    assert "XXXXXXXXXXXX0993" in json.dumps(json.loads(out))
    assert mask_card_numbers("[%se5]" % DISC[:15]) == "[%se5]" % DISC[:15]  # exponent form: out of scope
