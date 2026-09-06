"""Profile-scoped child used by the existing heartbeat and persona ticks."""

from personas import apply_persona_override

apply_persona_override()

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402

from personas.learning import worker  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Drain recorded persona learning work")
    parser.add_argument("--max-stages", type=int, default=1)
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(worker.wake_learning(test_mode=args.test, max_stages=args.max_stages))
    print(json.dumps(result))
    return int(result.get("status") in {"failed", "retry", "lease_lost"})


if __name__ == "__main__":
    sys.exit(main())
