"""Print YES if there's an active Kite token in the DB, else NO. Used by run.ps1."""
import asyncio
import sys

from src.auth.kite_session import get_active_token


async def main() -> None:
    token = await get_active_token()
    sys.stdout.write("YES" if token else "NO")


if __name__ == "__main__":
    asyncio.run(main())
