"""Entrypoint for the graphical executable."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from log_tools.desktop import main
    main()
