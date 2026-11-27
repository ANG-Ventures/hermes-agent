"""Keep the checked-in admission inventory synchronized without weakening the ratchet."""
import contextlib
import importlib.util
import io
import json
import subprocess
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "admission_decider_lint", ROOT / ".github/router-core/admission-decider-lint.py"
)
LINT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LINT)


class AdmissionBaselineTests(unittest.TestCase):
    def run_lint(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = LINT.main(list(args))
        return rc, output.getvalue()

    def test_checked_in_baseline_matches_current_tree(self):
        rc, output = self.run_lint("--root", str(ROOT))
        self.assertEqual(rc, 0, output)

    def test_new_decider_still_fails_and_update_cannot_grandfather_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fleet").mkdir()
            baseline = root / "fleet/admission-decider-baseline.json"
            baseline.write_text(json.dumps({"schema": 1, "scan": ["."], "library": [], "files": {}}))
            before = baseline.read_bytes()
            (root / "scripts").mkdir()
            (root / "scripts/planted_decider.py").write_text(
                "if load1 > 0.9 * ncpu:\n    pass\n"
            )
            # the lint scans git-tracked files (and --update requires a git index)
            subprocess.run(["git", "init", "-q", str(root)], check=True, env=LINT._git_env())
            subprocess.run(["git", "-C", str(root), "add", "."], check=True, env=LINT._git_env())
            for flags in ((), ("--update",)):
                with self.subTest(flags=flags):
                    rc, output = self.run_lint("--root", str(root), *flags)
                    self.assertEqual(rc, 1, output)
                    self.assertIn("scripts/planted_decider.py: 1 new", output)
                    self.assertEqual(baseline.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
