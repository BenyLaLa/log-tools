"""Console/stdio executable: stdout is reserved for MCP protocol messages."""
import multiprocessing
import os
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from log_tools.desktop_config import DesktopSettings
    if "--config" not in sys.argv:
        sys.argv.extend(["--config", str(DesktopSettings().path)])
    from log_tools.__main__ import main
    try:
        main()
    finally:
        # The SDK's temporary TextIOWrapper may close stdout's binary buffer
        # when stdio shuts down. PyInstaller explicitly flushes stdout again
        # after this entry point, so give that final flush a valid empty stream.
        if sys.stdout is not None and sys.stdout.closed:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        if sys.__stdout__ is not None and sys.__stdout__.closed:
            sys.__stdout__ = sys.stdout
