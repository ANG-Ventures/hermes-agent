"""Manual, non-mutating live fallback canary using the caller's own family alias."""
# SCHEDULE: manual (operator/worker acceptance only)
import json
import logging
import argparse


def main():
    from hermes_cli.config import load_config
    from hermes_cli import goal_judge_chain, goals
    parser = argparse.ArgumentParser()
    parser.add_argument('--family', choices=('sol', 'grok', 'kimi'), default='sol')
    family = parser.parse_args().family
    alias = load_config()['model']['aliases'][family]
    provider, model = alias.split('/', 1)
    logging.basicConfig(level=logging.INFO)
    routes = [
        dict(provider='custom', model='unreachable-control', base_url='http://127.0.0.1:9/v1', api_key='unused', timeout=10),
        dict(provider=provider, model=model, timeout=60, reasoning_effort='medium'),
    ]
    # Inject only the policy in this disposable process; real judge prompt, parser,
    # central provider resolution and owned-request cleanup remain in use.
    goal_judge_chain.chain_config = lambda timeout: (routes, 75)
    result = goals.judge_goal('Write a verified artifact', 'Artifact not written yet; work remains.')
    print(json.dumps(dict(verdict=result[0], parse_failed=result[2], transport_failed=result[4])))
    if result[0] != 'continue' or result[2] or result[4]:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
