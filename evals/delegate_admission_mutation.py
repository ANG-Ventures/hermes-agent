"""Manual bounded mutation probe; run in an isolated checkout only."""
import os
from pathlib import Path
import subprocess


def main():
    path = Path("tools/delegate_tool.py")
    original = path.read_text(encoding="utf-8")
    old = "        def _reject_admission(reason, dispatch=None):\n"
    assert original.count(old) == 1
    mutant = original.replace(old, old + "            return json.dumps(_execute_and_aggregate())\n")
    try:
        path.write_text(mutant, encoding="utf-8")
        env = {**os.environ, "HERMES_TEST_FILE_RETRIES": "0"}
        result = subprocess.run(["scripts/run_tests.sh", "tests/tools/test_delegate_admission_rejection.py",
                                 "-q", "-k", "interactive_admission"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
        print(result.stdout)
        assert result.returncode == 1
        assert "5 failed" in result.stdout
        assert "interactive parent blocked on a rejected child" in result.stdout
        print("MUTATION KILLED: all five refusal reasons block the parent under inline fallback")
    finally:
        path.write_text(original, encoding="utf-8")
        assert path.read_text(encoding="utf-8") == original


if __name__ == "__main__":
    main()
