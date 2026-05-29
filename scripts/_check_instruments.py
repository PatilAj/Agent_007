"""Print the current instrument-catalog row count. Used by run.ps1."""
import asyncio
import sys

from src.broker.instrument_catalog import count_instruments


async def main() -> None:
    n = await count_instruments()
    sys.stdout.write(str(n))


if __name__ == "__main__":
    asyncio.run(main())
