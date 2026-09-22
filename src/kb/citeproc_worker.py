"""Isolated subprocess entry point for bounded report rendering."""

import os
import resource
import sys
from pathlib import Path


def main():
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    resource.setrlimit(resource.RLIMIT_FSIZE, (20_000_000, 20_000_000))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    if sys.argv[1] == "pandoc":
        # GHC reserves a large virtual address range; its own heap limit bounds
        # allocation without mistaking that reservation for physical memory.
        os.execv(sys.argv[2], [sys.argv[2], *sys.argv[3:], "+RTS", "-M512M", "-RTS"])
    if sys.argv[1] != "pdf":
        raise SystemExit("unsupported worker mode")
    resource.setrlimit(resource.RLIMIT_AS, (2_000_000_000, 2_000_000_000))
    import typst

    if typst.__version__ != "0.15.0":
        raise SystemExit("unsupported Typst version")
    Path("output.pdf").write_bytes(typst.compile("output.typ", root=str(Path.cwd())))


if __name__ == "__main__":
    main()
