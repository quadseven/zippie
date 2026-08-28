"""Entry point for `python3 -m zippie`.

pyproject declares a `zippie = zippie.cli:main` console script, but that
only exists after a pip install. The GL-MT3000 deploy is a plain directory
copy to /opt/zippie-agent - OpenWrt has no pip and only ~30 MB of writable
overlay - so on the actual target there is no console script and, without this
file, no way to run the package at all:

    /usr/bin/python3: No module named zippie.__main__;
    'zippie' is a package and cannot be directly executed

That is exactly how the agent came to be started by hand, which is how it came
to die at 13:22 on 2026-07-27 with nothing supervising it. `python3 -m zippie`
is what the procd init script calls.
"""

from zippie.cli import main

if __name__ == "__main__":
    main()
