from bess_ote.cli import main

if __name__ == "__main__":  # guard: worker processes (spawn) re-import the main module
    raise SystemExit(main())
