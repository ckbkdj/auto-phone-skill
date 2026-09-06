#!/usr/bin/env python3
"""Local operator recovery. Stop Skill and inspect actual phone/business state first.

Never expose this command to the model, HTTP or MCP. Never delete a journal to unblock a device.
"""
import argparse
from pathlib import Path
from lobster_phone_agent.skill.journal import OperationJournal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal', type=Path, required=True)
    parser.add_argument('--device', required=True)
    parser.add_argument('--operation', required=True)
    parser.add_argument('--outcome', choices=['executed', 'not-executed'], required=True)
    parser.add_argument('--operator-verified', action='store_true', required=True)
    args = parser.parse_args()
    if not args.journal.is_file():
        parser.error('journal does not exist')
    OperationJournal(args.journal).reconcile(args.device, args.operation, executed=args.outcome == 'executed')
    print('Reconciliation recorded. No operation was replayed.')


if __name__ == '__main__':
    main()
