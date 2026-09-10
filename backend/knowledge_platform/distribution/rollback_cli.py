"""Explicit offline rollback candidate CLI; never switches installation writers."""
import argparse
import json
from pathlib import Path
from .sqlite_reverse_delta import build_rollback_candidate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-snapshot', required=True, type=Path)
    parser.add_argument('--target-before', required=True, type=Path)
    parser.add_argument('--target-after', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--table', required=True, action='append')
    args = parser.parse_args()
    try:
        result = build_rollback_candidate(args.source_snapshot, args.target_before,
            args.target_after, args.output, tuple(args.table))
    except Exception:
        print(json.dumps({'format':'puddingknowledge-sqlite-rollback-candidate/v1',
            'status':'error', 'error_code':'rollback_candidate_rejected', 'activation_allowed':False}))
        return 1
    print(json.dumps({'format':'puddingknowledge-sqlite-rollback-candidate/v1',
        'status':'candidate_verified', 'installation_cutover_performed':False,
        'writer_fence_verified':False, **result}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
