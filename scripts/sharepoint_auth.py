import os
import sys
import json
import msal

# Microsoft Graph PowerShell First-Party Client ID
CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
AUTHORITY = "https://login.microsoftonline.com/organizations"
SCOPES = ["User.Read", "Sites.Read.All", "Files.Read.All"]
CACHE_FILE = os.path.expanduser("~/.sharepoint_token_cache.bin")

def get_cache():
    cache = msal.SerializableTokenCache()
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            cache.deserialize(f.read())
    return cache

def save_cache(cache):
    if cache.has_state_changed:
        with open(CACHE_FILE, "w") as f:
            f.write(cache.serialize())

def main():
    cache = get_cache()
    app = msal.PublicClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        token_cache=cache
    )

    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            print("LOGGED_IN_SILENTLY")
            print(f"User: {accounts[0].get('username')}")
            save_cache(cache)
            return

    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        print("ERROR_INITIATING_FLOW:", flow)
        sys.exit(1)

    print("=== DEVICE LOGIN REQUIRED ===")
    print(f"URL: {flow['verification_uri']}")
    print(f"CODE: {flow['user_code']}")
    print(f"MESSAGE: {flow['message']}")
    print("=============================")
    sys.stdout.flush()

    # Block and poll until user completes login in browser
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" in result:
        print("LOGIN_SUCCESS")
        save_cache(cache)
        # Fetch current user info
        import requests
        headers = {"Authorization": f"Bearer {result['access_token']}"}
        me = requests.get("https://graph.microsoft.com/v1.0/me", headers=headers).json()
        print(f"Autenticado com sucesso como: {me.get('displayName')} ({me.get('userPrincipalName')})")
    else:
        print("LOGIN_FAILED:", result.get("error_description", result))
        sys.exit(1)

if __name__ == "__main__":
    main()
