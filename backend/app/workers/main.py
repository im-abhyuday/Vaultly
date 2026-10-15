"""Worker process entrypoint forwarding to app.workers.__main__."""

from app.workers.__main__ import main_cli

if __name__ == "__main__":
    main_cli()
