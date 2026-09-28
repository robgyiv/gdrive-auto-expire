"""`python -m gdrive_auto_expire` — the fallback cron invocation for when the
console script is not on an absolute path we can find."""

import sys

from . import main

if __name__ == "__main__":
    sys.exit(main())
