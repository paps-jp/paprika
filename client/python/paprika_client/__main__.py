"""``python -m paprika_client ...`` entry point.

Forwards to :func:`paprika_client._cli.main`. The packaged
``paprika-client`` script (declared in pyproject.toml's
``[project.scripts]``) calls the same function -- this file exists so
ad-hoc installs without setuptools' console_scripts (e.g. ``pip
install -e .`` from a worktree, or running directly out of the source
tree) still work.
"""
from paprika_client._cli import main

raise SystemExit(main())
