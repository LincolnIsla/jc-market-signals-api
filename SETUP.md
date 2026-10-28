# JC Market Signals secure device login

This backend update is required. The website alone cannot provide revocable persistent authentication for the Render API.

## Deploy

1. Give the ChatGPT GitHub connection write access to LincolnIsla/jc-market-signals-api, or replace that repository's app.py with the included version yourself.
2. Attach a persistent disk to the existing Render API service, mounted at /var/data. Set AUTH_SESSION_DB_PATH=/var/data/jc-device-sessions.sqlite3. The database directory must exist. Do not point this setting at /tmp or another ephemeral directory.
3. Keep the existing ADMIN_TOKEN and sandbox E*TRADE credentials unchanged. Keep ETRADE_ENV=sandbox. No new Python dependencies are required; requirements.txt is included for convenience.
4. Deploy the API, then open JC Market Signals. The Secure device login panel verifies /api/auth/session before enabling Sign In.
5. Enter ADMIN_TOKEN once, check Remember This Device, and select Sign In. The website clears the token input after exchange and verifies the returned session cookie before claiming success.

The current service has no persistent disk in the last known configuration. This package does not provision or bill for a disk. Render configuration and GitHub permissions remain operator steps.

## Authentication design

- The private Site preserves ChatGPT's hosted authentication. Its Worker additionally requires the platform-authenticated user identity for every /api request.
- The website proxies only an allowlist of routes to the existing Render origin. It forwards only the dedicated device cookie, never ChatGPT's cookies. This makes the device cookie first-party on the Site and avoids third-party cookie blocking.
- ADMIN_TOKEN is sent only on the explicit sign-in exchange, in an Authorization header over HTTPS. It is never put in a URL or persisted in localStorage. The legacy session-only token path is retained while setup is incomplete.
- The backend creates a cryptographically random 48-byte opaque session token. Only its SHA-256 hash, expiry, activity time and credential-generation fingerprint are stored in the database.
- The browser cookie is __Host-jc_device with Secure, HttpOnly, SameSite=Lax, Path=/ and no Domain. With Remember This Device it has a 30-day maximum lifetime and a server-enforced 7-day inactivity limit. Without that option it is a browser-session cookie with an 8-hour backend limit.
- Every API request revalidates the session server-side. No signed-in status in browser storage grants access.
- Sign Out deletes the backend record and expires the cookie. Sign Out All Devices revokes all remembered-device sessions. Rotating ADMIN_TOKEN invalidates existing sessions. Failed revocation is displayed as a failure, not a completed sign-out.
- POST requests require the exact Site Origin at the gateway and backend; the gateway requires JSON and rejects unsupported routes and oversized requests. Login exchanges are rate-limited.

Persistent browser login and E*TRADE OAuth authorization are separate. This preserves the existing OAuth behavior: E*TRADE may require reauthorization after its tokens expire or the API process restarts. Deploying the backend restarts the existing in-memory OAuth connection; reconnect via the current Connect E*TRADE page if needed. No real-money order endpoint is enabled.

## Tests

27 backend tests pass, including the existing sandbox-order tests and new tests for protected cookie attributes, hashed session storage, simulated browser reopen, revocation of a copied cookie, CSRF, login validation, idle and absolute expiry, credential rotation, device-session rotation, all-device revocation, rate limiting, unavailable storage, and production-mode rejection.

Run with pytest installed separately for development:

pytest -q tests --basetemp=/absolute/non-tmp/test-directory

The website has additional tests for the same-origin gateway, login restore, cookie-authenticated requests, setup-required state, failed sign-out, sandbox previews and manual confirmation. No actual order is submitted by these tests.
