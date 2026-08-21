from __future__ import annotations

import sys

from by2kb.jobs.runner import IngestOutcome


def report(outcome: IngestOutcome) -> None:
    stream = sys.stdout if outcome.exit_code == 0 else sys.stderr
    print(outcome.message, file=stream)
