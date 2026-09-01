"""MongoDB connection diagnostic.

Run with the bot's venv active:   python db_test.py
It loads .env exactly like bot.py and tests the DATABASE_URI, printing a
masked version so you can verify username/host without exposing the password.
"""
import os
import asyncio

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except Exception:
    pass

# Same normalisation as bot.py (strip quotes / batch ^& escaping)
uri = os.environ.get("DATABASE_URI", "")
if uri:
    uri = uri.strip().strip('"').strip("'").replace("^&", "&")
    os.environ["DATABASE_URI"] = uri


def mask(u: str) -> str:
    try:
        scheme, rest = u.split("://", 1)
        creds, host = rest.split("@", 1) if "@" in rest else ("", rest)
        if ":" in creds:
            user, _pw = creds.split(":", 1)
            creds = f"{user}:****"
        return f"{scheme}://{creds}@{host}" if creds else f"{scheme}://{host}"
    except Exception:
        return "(could not parse)"


async def main():
    print("=" * 60)
    print("DATABASE_URI loaded as:")
    print("  ", mask(uri) or "(NOT SET)")
    print("DATABASE_NAME:", os.environ.get("DATABASE_NAME", "(not set)"))
    print("=" * 60)
    if not uri:
        print("FAIL: DATABASE_URI is empty. Check your .env file.")
        return

    # Show the username so you can compare it to Atlas -> Database Access
    try:
        user = uri.split("://")[1].split(":")[0]
        print("Username in URI:", user)
    except Exception:
        pass
    print("Connecting (10s timeout)...\n")

    from motor.motor_asyncio import AsyncIOMotorClient
    client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=10000)
    try:
        # Force a round trip / auth
        await client.admin.command("ping")
        print("SUCCESS: Connected and authenticated to MongoDB ✅")
        db = client[os.environ.get("DATABASE_NAME", "moviebot")]
        cols = await db.list_collection_names()
        print(f"Database '{os.environ.get('DATABASE_NAME','moviebot')}' has {len(cols)} collections.")
        print("The bot should now start. Run:  python bot.py")
    except Exception as e:
        name = type(e).__name__
        print(f"FAILED: {name}")
        msg = str(e)
        print(msg[:400])
        print()
        if "bad auth" in msg or "authentication failed" in msg.lower() or "auth" in msg.lower():
            print(">> This is a USERNAME/PASSWORD problem.")
            print("   1. Atlas -> Database Access -> Edit the user -> Edit Password.")
            print("   2. Use a NEW alphanumeric password (letters+numbers only).")
            print("   3. Role = 'Read and write to any database'. Update User.")
            print("   4. Put that EXACT username:password in .env DATABASE_URI.")
        elif "timed out" in msg.lower() or "server selection" in msg.lower():
            print(">> This is a NETWORK/IP problem.")
            print("   Atlas -> Network Access -> Add IP -> Allow 0.0.0.0/0.")


if __name__ == "__main__":
    asyncio.run(main())
    input("\nPress Enter to close...")
