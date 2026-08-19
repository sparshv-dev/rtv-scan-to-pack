# Deploying to Render + Supabase

This turns the scan-to-pack app from "one PC only" into a real hosted app
with a real URL, backed by a Supabase Postgres database instead of the
local `rtv-shipment.db` SQLite file.

The code side is already done — `rtv-shipment-server.py` now reads a
`DATABASE_URL` environment variable and speaks Postgres instead of SQLite.
What's left is account/dashboard setup, which only you can do (it needs
your own logins).

**Never paste your Supabase connection string (it contains your database
password) into a chat, issue, or anywhere public — type it directly into
Render's environment variable field.**

## Part 1 — Supabase (the database)

1. Go to [supabase.com](https://supabase.com) and sign up (free, no card).
2. **New project** — pick an org, name it (e.g. `rtv-scan-to-pack`), set a
   database password (save it somewhere — a password manager, not a chat),
   pick a region close to you (e.g. Mumbai/`ap-south-1` if offered), create.
   Takes ~2 minutes to provision.
3. Once it's ready: click **Connect** (top of the project dashboard) →
   under **Connection string**, select the **Session pooler** tab (not
   "Direct connection" — that one's IPv6-only on the free tier and won't
   reach Render) → copy the URI. It looks like:
   ```
   postgresql://postgres.xxxxxxxxxxxx:[YOUR-PASSWORD]@aws-0-ap-south-1.pooler.supabase.com:5432/postgres
   ```
4. Replace `[YOUR-PASSWORD]` with the real password from step 2 if it's
   not already filled in. Keep this string somewhere private — it goes
   into Render in Part 3, step 4.

## Part 2 — GitHub (so Render has something to deploy)

5. Go to [github.com/new](https://github.com/new), create a repository
   (private is fine) — e.g. `rtv-scan-to-pack`. **Don't** initialize it
   with a README/`.gitignore`/license — leave it empty.
6. Copy the repository URL it gives you (looks like
   `https://github.com/yourname/rtv-scan-to-pack.git`) and share it —
   the code is already committed locally and ready to push.

## Part 3 — Render (hosting)

7. Go to [render.com](https://render.com) and sign up free — signing up
   with GitHub makes the next step easier (auto-lists your repos).
8. Dashboard → **New +** → **Web Service** → connect the
   `rtv-scan-to-pack` repo from Part 2.
9. Configure the service:
   - **Name**: `rtv-scan-to-pack` (or anything)
   - **Region**: closest to you
   - **Branch**: `master`
   - **Root Directory**: leave blank
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python rtv-shipment-server.py`
   - **Instance Type**: Free
10. Under **Environment Variables**, add one:
    - Key: `DATABASE_URL`
    - Value: the Supabase Session Pooler connection string from Part 1
      (typed straight into this field, not shared anywhere else)
11. **Create Web Service.** Render builds and deploys (~2–3 minutes).
    Watch the logs for `RTV Scan-to-Pack app running` — if the database
    connection fails, the error shows up here (usually a copy-paste
    mistake in the connection string, e.g. leftover `[YOUR-PASSWORD]`).
12. Once live, Render gives you a URL like
    `https://rtv-scan-to-pack.onrender.com` — that's the app now, reachable
    from any device (phone, another PC, a packing-station tablet).

## Part 4 — Verify

13. Open the Render URL, add a hub, import a dump CSV, scan a tracking ID
    — same flow as testing locally.
14. Free Render web services sleep after 15 minutes idle — the *first*
    request after a gap takes ~30–50 seconds to wake up. Every request
    after that is instant. This is normal, not a bug.

## Worth knowing: no login on this URL

The app has no authentication — anyone with the Render URL can view or
edit shipment data. Fine for an internal tool where the URL isn't shared
publicly, but worth knowing before handing the link around. If you want a
simple password gate on it later, that's a small addition (HTTP Basic
Auth) — ask and it can be added without much fuss.
