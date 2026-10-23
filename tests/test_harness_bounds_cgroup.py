"""t_123bafee (hermes-home#4108), ported t_422c5e8a: the cgroup clamp in tests/_fixtures/bounds.py counted ACTIVE clean page cache as used memory.

Measured on ci-box runner containers (memory.max 4 GiB, kernel 7.0 with MGLRU): ~3 GiB of the
checkout/pip page cache sat in ``active_file`` with ``inactive_file`` 0 and anon ~150 MiB. The clamp
(``max - used - 35 % headroom``) went negative and every test ran under the 16 MiB floor, so unrelated
tests failed "grew 0.02 GiB of RSS (limit 0.02 GiB)" on PRs while main's run on other boxes passed.
Clean file pages are reclaimed before the OOM killer runs, so they must not shrink the budget."""
from pathlib import Path

from tests._fixtures import bounds as pytest_bounds

MiB = 1024 ** 2
GiB = 1024 ** 3


def _cgroup(tmp_path: Path, current: int, **stat: int) -> str:
    (tmp_path / "memory.max").write_text(f"{4 * GiB}\n")
    (tmp_path / "memory.current").write_text(f"{current}\n")
    (tmp_path / "memory.stat").write_text("".join(f"{k} {v}\n" for k, v in stat.items()))
    return str(tmp_path)


def _budget(base: str) -> int:
    limit = 4 * GiB
    used = pytest_bounds._level_used(base)
    room = limit - used
    return max(min(GiB, room - pytest_bounds._headroom(limit)), pytest_bounds._MIN_GROWTH)


def test_active_clean_page_cache_is_not_working_set(tmp_path):
    # ci-box-ang-ventures-org-53 at 03:4x PT: current 2722M, anon 111M, active_file 32M ... the
    # worst sampled shape is the one below: active_file 3061M, inactive_file 0.
    base = _cgroup(tmp_path, current=3538 * MiB, anon=153 * MiB, file=3103 * MiB, shmem=42 * MiB,
                   active_file=3061 * MiB, inactive_file=0, file_dirty=0, file_writeback=0)
    used = pytest_bounds._level_used(base)
    assert used < 600 * MiB, used / MiB  # anon + shmem + kernel, not the cache
    assert _budget(base) == GiB  # the 1 GiB default, not the 16 MiB floor


def test_shmem_and_dirty_pages_stay_counted(tmp_path):
    base = _cgroup(tmp_path, current=3000 * MiB, anon=100 * MiB, file=2800 * MiB, shmem=1500 * MiB,
                   active_file=1300 * MiB, inactive_file=0, file_dirty=700 * MiB, file_writeback=100 * MiB)
    # clean = 2800 - 1500 - 700 - 100 = 500 MiB reclaimable; the rest stays used
    assert pytest_bounds._level_used(base) == 2500 * MiB


def test_a_real_anon_squeeze_still_clamps_to_the_floor(tmp_path):
    # ci-box-ang-ventures-org-62: anon 3917M of a 4 GiB cgroup. Real pressure keeps the floor.
    base = _cgroup(tmp_path, current=4095 * MiB, anon=3917 * MiB, file=30 * MiB, shmem=11 * MiB,
                   active_file=2 * MiB, inactive_file=17 * MiB)
    assert _budget(base) == pytest_bounds._MIN_GROWTH


def test_without_a_file_row_only_inactive_file_is_subtracted(tmp_path):
    base = _cgroup(tmp_path, current=1000 * MiB, inactive_file=400 * MiB, active_file=300 * MiB)
    assert pytest_bounds._level_used(base) == 600 * MiB


def test_mlocked_clean_file_pages_stay_counted(tmp_path):
    # Prism 2d3bf87e12ba (t_72c6bce7): 3 GiB of clean file pages mlock()ed by a sibling sit in
    # ``file`` AND ``unevictable``; the kernel cannot reclaim them before memory.max OOM-kills.
    base = _cgroup(tmp_path, current=3172 * MiB, anon=100 * MiB, file=3072 * MiB, shmem=0,
                   active_file=0, inactive_file=0, unevictable=3072 * MiB, file_dirty=0, file_writeback=0)
    assert pytest_bounds._level_used(base) == 3172 * MiB
    # Mlocked, they shrink the budget to what 4 GiB - 3.1 GiB leaves above the headroom; the same
    # pages reclaimable leave the 1 GiB default.
    assert _budget(base) == 4 * GiB - 3172 * MiB - pytest_bounds._headroom(4 * GiB)
    (tmp_path / "reclaimable").mkdir()
    reclaimable = _cgroup(tmp_path / "reclaimable", current=3172 * MiB, anon=100 * MiB, file=3072 * MiB,
                          shmem=0, active_file=0, inactive_file=0, unevictable=0, file_dirty=0, file_writeback=0)
    assert _budget(reclaimable) == GiB


def test_unevictable_never_drives_clean_negative(tmp_path):
    # ``unevictable`` also counts mlocked anon and SHM_LOCKed shmem (one LRU, no file-only split),
    # so file - shmem - unevictable can go below 0; used must never exceed current.
    base = _cgroup(tmp_path, current=2000 * MiB, anon=1500 * MiB, file=400 * MiB, shmem=100 * MiB,
                   unevictable=1200 * MiB, file_dirty=0, file_writeback=0)
    assert pytest_bounds._level_used(base) == 2000 * MiB
