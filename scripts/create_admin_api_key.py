"""One-off CLI to mint a new admin API key for the global-events upload
endpoint (POST /admin/events/upload). Run this once per admin who needs
upload access.

The raw key is printed ONCE, here -- only its SHA-256 hash is stored in
admin_api_keys, so there is no way to recover it later. If it's lost, mint a
new one for that admin instead.

Usage:
    python scripts/create_admin_api_key.py "Jane Doe"
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from admin_events import generate_admin_api_key  # noqa: E402


def main():
    if len(sys.argv) != 2:
        print('Usage: python scripts/create_admin_api_key.py "Admin Name"')
        sys.exit(1)

    admin_name = sys.argv[1]
    raw_key, row = generate_admin_api_key(admin_name)

    print(f"Created admin API key #{row['id']} for {row['admin_name']}.")
    print()
    print("Copy this now -- it will not be shown again:")
    print()
    print(f"  {raw_key}")
    print()
    print("Send this to the admin. They'll pass it as the X-Admin-Key header")
    print("on POST /admin/events/upload.")


if __name__ == "__main__":
    main()
