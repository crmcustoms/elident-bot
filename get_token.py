"""
Run once to exchange an authorization code for access+refresh tokens.

Usage:
  1. Open amoCRM → Settings → Integrations → your private integration
  2. Click "Allow" and copy the authorization code from redirect URL
  3. python get_token.py <auth_code> <account_domain>
     e.g.: python get_token.py abc123 piece16174.amocrm.ru
"""
import sys
import json
import httpx
from config import settings


def main():
    if len(sys.argv) < 3:
        print("Usage: python get_token.py <auth_code> <account_domain>")
        sys.exit(1)

    code = sys.argv[1]
    domain = sys.argv[2].rstrip("/")
    if not domain.startswith("http"):
        domain = f"https://{domain}"

    r = httpx.post(
        f"{domain}/oauth2/access_token",
        json={
            "client_id": settings.amo_client_id,
            "client_secret": settings.amo_client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.amo_redirect_uri,
        },
        headers={"Content-Type": "application/json"},
    )
    r.raise_for_status()
    tokens = r.json()

    with open(settings.tokens_file, "w") as f:
        json.dump(tokens, f, indent=2)

    print(f"Tokens saved to {settings.tokens_file}")
    print(f"  access_token:  {tokens.get('access_token', '')[:20]}...")
    print(f"  refresh_token: {tokens.get('refresh_token', '')[:20]}...")


if __name__ == "__main__":
    main()
