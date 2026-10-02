"""Package entrypoint; normal launch always uses the production Project Hub."""
import sys

from reviewrelay.ui import main


if __name__ == "__main__":
    if "--packaged-smoke" in sys.argv[1:]:
        from reviewrelay_frozen_smoke import main as smoke_main
        raise SystemExit(smoke_main())
    raise SystemExit(main())
