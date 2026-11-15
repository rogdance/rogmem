from rogmem.cli import main
from rogmem.host_hook import adopt_stdio

if __name__ == "__main__":
    adopt_stdio()
    raise SystemExit(main())
