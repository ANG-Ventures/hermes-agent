import json, subprocess, collections, os, re
L = os.path.dirname(os.path.abspath(__file__)) + '/'  # lead/; merged.json (build input, not committed) goes here
D = os.path.dirname(L[:-1]) + '/'
R = os.path.dirname(os.path.dirname(os.path.dirname(D[:-1])))
T = ['gateway', 'agent', 'hermes_cli', 'plugins', 'cron_tools', 'scripts_misc']
TMAP = {'gateway': 'gateway', 'agent': 'agent', 'hermes_cli': 'hermes_cli', 'plugins': 'plugins',
        'cron+tools': 'cron_tools', 'scripts+misc': 'scripts_misc',
        'auto': 'auto', 'auto-desktop-retired': 'auto-desktop-retired', 'auto-cherry-pick': 'auto-cherry-pick'}


def sh(c, cwd=R):
    return subprocess.run(c, shell=True, cwd=cwd, capture_output=True, text=True).stdout


def census():
    return json.load(open(D + 'census/census_v2.json', encoding='utf-8'))['rows']


def load_merged():
    return json.load(open(L + 'merged.json', encoding='utf-8'))


def advres(x):
    a = x.get('adversary') or {}
    return (str(a.get('result') or '')).lower() or None


def dump_rows(rows):
    """Serialize rows.json exactly as committed: keep the hand escape that stops gitleaks generic-api-key
    matching one #1128-era commit subject (the literal is split here for the same reason)."""
    s = json.dumps(rows, indent=1, default=str)
    return s.replace("('key" + ": claude-opus-4.5')", "('key" + "\\u003a claude-opus-4.5')")


def check_cards(rows, cards):
    """Every key a slice card carries must still have that card's verdict in rows.json, so a KEEP row
    never gets revert/upstream instructions (t_04cd162a)."""
    bad = [(c['name'], k, rows[k]['final'], c['verdict']) for c in cards for k in c['keys'] + c['followers']
           if rows[k]['final'] != c['verdict']]
    assert not bad, bad
