"""Entry point of the packaged command line tool (intercom-gateway.exe): run, check, devices, status, stop."""
import sys

from gateway.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
