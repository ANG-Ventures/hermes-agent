"""Full tier STORES already-compressed members instead of deflating them again (t_5bea54fb)."""
import zipfile

from hermes_cli.backup import _write_full_zip_backup_locked


def test_precompressed_members_are_stored(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "a.zst").write_bytes(b"z" * 4096)
    (home / "b.PNG").write_bytes(b"p" * 4096)
    (home / "c.txt").write_text("t" * 4096)
    out = tmp_path / "out.zip"
    assert _write_full_zip_backup_locked(out, home) == out
    with zipfile.ZipFile(out) as zf:
        kind = {i.filename: i.compress_type for i in zf.infolist()}
        assert kind["a.zst"] == zipfile.ZIP_STORED and kind["b.PNG"] == zipfile.ZIP_STORED
        assert kind["c.txt"] == zipfile.ZIP_DEFLATED
        assert zf.read("a.zst") == b"z" * 4096 and zf.read("c.txt") == b"t" * 4096
        assert zf.testzip() is None
