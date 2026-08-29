"""Helper script to export saved accounts into accounts.txt for distribution."""

import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from secure_store import load_accounts  # noqa: E402
from main import ACCOUNT_STORE  # noqa: E402


def export_bundle_accounts(target_dir: Path) -> int:
    target_file = target_dir / "accounts.txt"
    if not ACCOUNT_STORE.exists():
        print(f"No account store found at {ACCOUNT_STORE}")
        return 0

    try:
        accounts = load_accounts(ACCOUNT_STORE)
        if not accounts:
            print("No accounts found to bundle.")
            return 0

        lines = []
        for acc in accounts:
            line = f"{acc['email']}|{acc['password']}"
            if acc.get("mail_alias"):
                line += f"|{acc['mail_alias']}"
            lines.append(line)

        target_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[OK] Exported {len(lines)} account(s) to {target_file}")
        return len(lines)
    except Exception as exc:
        print(f"[Warning] Could not export accounts: {exc}")
        return 0


if __name__ == "__main__":
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else PROJECT_ROOT
    export_bundle_accounts(out_dir)
