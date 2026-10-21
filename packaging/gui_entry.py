"""Entry point of the packaged desktop app (IntercomGateway.exe).

Started normally it opens the UI. The UI starts the gateway as a detached process by running this very
executable again with `--gateway-run <args of the "run" command>`; that path must not import Qt."""
import sys


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--gateway-run":
        from gateway.__main__ import main as cli_main

        return cli_main(["run", *sys.argv[2:]])
    from gateway.ui.app import main as ui_main

    return ui_main()


if __name__ == "__main__":
    sys.exit(main())
