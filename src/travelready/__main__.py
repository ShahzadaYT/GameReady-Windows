"""Allow ``python -m travelready`` to start the GUI."""
from .main import main

if __name__ == "__main__":
    raise SystemExit(main())
