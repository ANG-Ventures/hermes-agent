"""Read-only fleet goal-judge migration inventory; never reads credential values."""
# SCHEDULE: manual (operator rollout inventory)
import json
from pathlib import Path


def main():
    from hermes_cli.config import load_config
    from hermes_constants import set_hermes_home_override
    root = Path.home() / '.hermes'
    homes = [root, *sorted((root / 'profiles').iterdir())] if (root / 'profiles').is_dir() else [root]
    rows = []
    for home in homes:
        if not (home / 'config.yaml').is_file():
            continue
        set_hermes_home_override(home)
        try:
            config = load_config()
            judge = config.get('auxiliary', {}).get('goal_judge', {})
            aliases = config.get('model', {}).get('aliases', {})
            def safe(route):
                return {key: route[key] for key in ('provider', 'model', 'timeout', 'total_timeout', 'reasoning_effort') if key in route}
            proposed = []
            missing = []
            for family in ('opus', 'sol', 'grok', 'kimi'):
                alias = aliases.get(family, '')
                if not isinstance(alias, str) or '/' not in alias:
                    missing.append(family)
                    continue
                provider, model = alias.split('/', 1)
                proposed.append(dict(provider=provider, model=model, timeout=30))
            rows.append(dict(profile='default' if home == root else home.name, home=str(home),
                             effective=safe(judge), fallback_chain=[safe(r) for r in judge.get('fallback_chain', [])],
                             inheritance='independent profile; no live default-profile inheritance',
                             proposed=proposed, missing_aliases=missing,
                             migration='replace auxiliary.goal_judge routes only; total_timeout=120; preserve max_tokens',
                             acceptance='pending fresh-client forced-failure canary under this home'))
        finally:
            set_hermes_home_override(None)
    print(json.dumps(rows, indent=2))


if __name__ == '__main__':
    main()
