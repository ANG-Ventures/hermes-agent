import json, subprocess, collections, os, re
R = '/Volumes/ramscratch/kanban-workspaces/default/t_03e35f0e/fork'
D = R + '/docs/plans/fork-pr-audit/'
L = '/Volumes/ramscratch/kanban-workspaces/default/t_03e35f0e/lead/'
T = ['gateway', 'agent', 'hermes_cli', 'plugins', 'cron_tools', 'scripts_misc']
TMAP = {'gateway': 'gateway', 'agent': 'agent', 'hermes_cli': 'hermes_cli', 'plugins': 'plugins',
        'cron+tools': 'cron_tools', 'scripts+misc': 'scripts_misc',
        'auto': 'auto', 'auto-desktop-retired': 'auto-desktop-retired', 'auto-cherry-pick': 'auto-cherry-pick'}


def sh(c, cwd=R):
    return subprocess.run(c, shell=True, cwd=cwd, capture_output=True, text=True).stdout


def census():
    return json.load(open(D + 'census/census_v2.json'))['rows']


def load_merged():
    return json.load(open(L + 'merged.json'))


def advres(x):
    a = x.get('adversary') or {}
    return (str(a.get('result') or '')).lower() or None
