"""One-time helper: obtain a Google Drive refresh token for the spec-builder.

Run this ONCE, locally, signed in as the target Drive account (Oranit's).
It opens a browser, you approve access, and it prints a GOOGLE_REFRESH_TOKEN
you paste into Render (together with the client id/secret).

Prereqs:
    pip install google-auth-oauthlib
    A Google Cloud OAuth 2.0 "Desktop app" client → download client_secret.json.

Usage:
    python google_oauth_setup.py /path/to/client_secret.json
"""

import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python google_oauth_setup.py <client_secret.json>")
        raise SystemExit(1)

    flow = InstalledAppFlow.from_client_secrets_file(sys.argv[1], SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    print("\n=== paste these into Render (service env vars) ===")
    print("GOOGLE_CLIENT_ID     =", creds.client_id)
    print("GOOGLE_CLIENT_SECRET =", creds.client_secret)
    print("GOOGLE_REFRESH_TOKEN =", creds.refresh_token)
    print("\n(Also set GDRIVE_PARENT_FOLDER_ID to the target Drive folder's id.)")
    if not creds.refresh_token:
        print("\n⚠ No refresh_token returned — revoke prior access at "
              "https://myaccount.google.com/permissions and re-run.")


if __name__ == "__main__":
    main()
