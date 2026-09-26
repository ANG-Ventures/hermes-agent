"""execute_code keeps the gh/git LANE env (t_45c11886).

Before: the scrub dropped GIT_CONFIG_KEY_* (KEY substring), GIT_AUTHOR_* (AUTH
substring) and the agent marker, so sandbox git fell back to the global
``gh auth git-credential`` helper and the gh shim could not resolve a profile.
"""

from tools.code_execution_tool import _scrub_child_env

SHIM = "/home/u/.h/var/gh-shim/gh"


def _scrub(env):
    return _scrub_child_env(env, is_passthrough=lambda _n: False, is_windows=False)


def _lane_env():
    return {
        "PATH": "/home/u/.h/var/gh-shim:/usr/bin",
        "HERMES_HOME": "/home/u/.h/profiles/daedalus",
        "HERMES_AGENT": "true",
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "credential.https://github.com.helper",
        "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": "credential.https://github.com.helper",
        "GIT_CONFIG_VALUE_1": f"!{SHIM} auth git-credential",
        "GIT_AUTHOR_NAME": "ang-fleet-workers[bot]",
        "GIT_AUTHOR_EMAIL": "1+ang-fleet-workers[bot]@users.noreply.github.com",
        "GIT_COMMITTER_NAME": "ang-fleet-workers[bot]",
        "GIT_COMMITTER_EMAIL": "1+ang-fleet-workers[bot]@users.noreply.github.com",
    }


def test_lane_env_survives_scrub():
    env = _lane_env()
    out = _scrub(env)
    for k, v in env.items():
        assert out.get(k) == v, k


def test_non_helper_git_config_group_is_dropped_whole():
    env = _lane_env()
    env["GIT_CONFIG_COUNT"] = "3"
    env["GIT_CONFIG_KEY_2"] = "http.https://github.com/.extraheader"
    env["GIT_CONFIG_VALUE_2"] = "AUTHORIZATION: bearer sekrit"
    out = _scrub(env)
    assert not [k for k in out if k.startswith("GIT_CONFIG_")]
    assert "sekrit" not in repr(out)


def test_count_mismatch_or_garbage_drops_group():
    env = _lane_env()
    env["GIT_CONFIG_COUNT"] = "3"  # KEY_2 missing
    assert not [k for k in _scrub(env) if k.startswith("GIT_CONFIG_")]
    env["GIT_CONFIG_COUNT"] = "nope"
    assert not [k for k in _scrub(env) if k.startswith("GIT_CONFIG_")]


def test_secret_git_vars_still_blocked():
    out = _scrub({"GIT_ASKPASS_TOKEN": "x", "GH_TOKEN": "y", "GITHUB_TOKEN": "z"})
    assert out == {} or not {"GIT_ASKPASS_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"} & set(out)
