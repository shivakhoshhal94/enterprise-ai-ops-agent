"""Reuse pytest's offline fixtures; compare orchestration, not model intelligence."""

import argparse
from pathlib import Path
import subprocess
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--orchestrator', choices=('both', 'responses', 'langgraph'), default='both')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    command = [sys.executable, '-m', 'pytest', '-W', 'always', '-ra',
               '--log-disable=enterprise_ai_ops_agent.events',
               '--capture=tee-sys', str(root / 'tests/test_orchestrator_comparison.py')]
    if args.orchestrator != 'both':
        command.extend(['-k', args.orchestrator])
    return subprocess.run(command, cwd=root).returncode


if __name__ == '__main__':
    raise SystemExit(main())
