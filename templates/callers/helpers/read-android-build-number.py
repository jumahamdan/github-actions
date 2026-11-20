#!/usr/bin/env python3
"""Read a caller-selected Android build number; this does not allocate one."""
import json
import os
import re
import sys
from pathlib import Path


def main():
    try:
        if (os.environ.get('GITHUB_ACTIONS') != 'true'
                or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
                or os.environ.get('GITHUB_REF') != 'refs/heads/staging'):
            raise ValueError('Expected an explicit staging workflow dispatch')
        event = Path(os.environ.get('GITHUB_EVENT_PATH', ''))
        if not event.is_absolute() or not event.is_file() or event.stat().st_size > 1048576:
            raise ValueError('Expected the bounded GitHub event file')
        number = json.loads(event.read_text()).get('inputs', {}).get('build_number')
        if not isinstance(number, str) or not re.fullmatch(r'[1-9][0-9]{0,9}', number) or int(number) > 2000000000:
            raise ValueError('Expected a positive Android build number at most 2000000000')
        print(number)
        return 0
    except (OSError, ValueError, TypeError, AttributeError):
        print('Android build number selection is missing, invalid, or outside an approved staging dispatch.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
