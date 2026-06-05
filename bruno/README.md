# Bruno API collection — Ecommerce Store backend

A [Bruno](https://www.usebruno.com/) collection covering **all `/api/v1` endpoints**, scaffolded from the
FastAPI OpenAPI schema. Collections are plain-text `.bru` files, so this lives in-repo and diffs like code.

## Requirements
- The **Bruno** desktop app, or the CLI: `npm i -g @usebruno/cli` → `bru run`.
- The backend running locally: `uvicorn app.main:app --reload --port 8000`.

## Setup (credentials are NOT committed)
1. `cp bruno/.env.example bruno/.env`  — `bruno/.env` is gitignored.
2. Fill in your local dev seed accounts (documented in the project notes). For anything real, use your own values.
3. Bruno auto-loads `bruno/.env` into `process.env`; the `Local` environment maps them to `email` / `password` /
   `adminEmail` / `adminPassword`.

## Open it
1. Bruno → **Open Collection** → select this `bruno/` folder.
2. Pick the **Local** environment (top-right).
3. Fire any request.

## Auth — how the bearer gets injected
- Collection-level auth is `bearer {{token}}`; every protected request uses `auth: inherit`.
- A **collection pre-request script auto-logs-in as the customer** the first time you send a request
  (only when `token` is empty — so it never burns the login rate limit of 5 / 15 min), and stores the token.
- `01-auth > Login` and `01-auth > Login (admin)` capture `access_token` into the `token` env var via a
  post-response script. **Run `Login (admin)` once to switch the whole collection to admin access.**
- `token` is a Bruno **secret var** — stored locally, never written into the committed env file.
- Access tokens expire in ~15-30 min. On a `401`, run a Login request again, or run `01-auth > Refresh`
  (cookie-based, 30/hour — no login-budget cost). Bruno's cookie jar holds the refresh cookie automatically.

> `login` is **form-encoded** and its `username` field is the **email** (OAuth2 password flow).

## Folder map (mirrors `.claude/notes/research/2026-06-02-api-endpoint-testing-reference.md`)

| Folder | Endpoints | Folder | Endpoints |
|---|---|---|---|
| `01-auth` | login/refresh/sessions/verify/reset | `12-b2b-company` | company profile + team |
| `02-account` | profile, prefs, GDPR, addresses | `13-admin-dashboard` | KPIs + audit log |
| `03-catalog-products` | products + image upload | `14-admin-customers` | directory, notes, tags, impersonate |
| `04-catalog-categories` | category tree + SEO + hero | `15-admin-reviews-tags` | moderation + tag taxonomy |
| `05-reviews` | public read + customer write/vote | `16-admin-inventory` | levels, ledger, restock/transfer |
| `06-search` | Meili search + suggest | `17-admin-shipping` | zones/methods/rates |
| `07-storefront-misc` | banners, shipping quote | `18-admin-marketing` | coupons/banners/campaigns/pages |
| `08-orders` | create/list/cancel/invoice/refunds | `19-admin-b2b` | companies + quotes + convert |
| `09-coupons` | validate (preview) | `20-admin-reports` | sales/top/category/refunds |
| `10-payments` | PayU/Stripe + webhooks | `21-admin-staff-settings` | staff + store config |
| `11-b2b-quotes` | request/accept/PDF | `22-storefront-site` | store-config, newsletter, cookie-consent, contact, CMS pages/blocks |

## Conventions baked in
- **Path params** appear as `:param` with example values in a `params:path` block.
- **Query params**: required ones are enabled with examples; optional ones are present but **disabled** (`~`).
- **JSON bodies** pre-fill the required fields; the **full field reference** (optional/nullable + enums) is in
  each request's **Docs** tab.
- **Multipart** uploads have a `file: @file()` placeholder — point it at a local image before sending.

## Run headless / CI
```bash
# creds come from process.env — export them or pass --env-var
export BRUNO_EMAIL=... BRUNO_PASSWORD=...
bru run --env Local                          # whole collection
bru run 01-auth --env Local                  # one folder
bru run "08-orders/Create Order.bru" --env Local
```

## Heads-up
- Admin / B2B / destructive requests mutate dev data — point `baseUrl` at a throwaway DB if unsure.

## Regenerate
Generated from the live schema. After the API changes, re-dump `app.openapi()` and re-run the generator
(see the top of `.claude/notes/research/2026-06-02-api-endpoint-testing-reference.md`). Re-running preserves a
local `bruno/.env`.
