# Deploying Indocount on astrikos.xyz

Follows `deployment_context.md`: two odd ports, one frontend block, one backend block (REST + websocket on the same port).

| Item | Value |
| --- | --- |
| POC name | `indocount` |
| Frontend | port **3371**, `indocount.astrikos.xyz` (Cloudflare **Orange**), pm2 `indocount_3371` |
| Backend (REST + `/ws`) | port **4371**, `indocount-api.astrikos.xyz` (Cloudflare **Gray**), pm2 `indocount_be_4371` |

Registry row to add: `indocount | 3371 | 4371 | live`.

The stack is React (Create React App) + FastAPI, so the env vars are `REACT_APP_*` (CRA), not `VITE_*`, and the build folder is `build/`, not `dist/`.

## Env

`.env.production` (baked in at build time; no host or port literals in source):
```
REACT_APP_API_URL=https://indocount-api.astrikos.xyz:8443
REACT_APP_WS_URL=wss://indocount-api.astrikos.xyz:8443/ws
```
`.env.development` points `npm start` at `http://localhost:8000`. `backend/.env` on the server (`backend/.env.example` lists every option):
```
PORT=4371
CORS_ORIGINS=https://indocount.astrikos.xyz:8443,https://indocount.astrikos.xyz
FRONTEND_DIR=none
```
CCTV clips, posters and analytics JSON (`/cctv/*`) are static files in the frontend build and load from the frontend host.

## Build and start

```bash
# Frontend
npm install && npm run build                      # -> build/
pm2 start serve --name "indocount_3371" -- ./build -s -p 3371

# Backend (Python 3.12; creates and seeds the SQLite DB on first start)
cd backend
python -m venv .venv && ./.venv/bin/pip install -r requirements.txt
printf 'CORS_ORIGINS=https://indocount.astrikos.xyz:8443,https://indocount.astrikos.xyz
FRONTEND_DIR=none
' > .env
PORT=4371 pm2 start ./.venv/bin/python --name "indocount_be_4371" -- run_server.py
pm2 save
```
For a single-port local run (backend serves the build), build with the variables emptied:
`REACT_APP_API_URL= REACT_APP_WS_URL= npm run build`, then start uvicorn without `FRONTEND_DIR=none`.

## nginx

`/etc/nginx/conf/astrikos.conf` (frontend):
```nginx
server {
    listen 8443 ssl;
    ssl_certificate     /etc/certs/astrikos.xyz/fullchain.pem;
    ssl_certificate_key /etc/certs/astrikos.xyz/privkey.pem;
    server_name indocount.astrikos.xyz;
    location / {
        add_header 'Access-Control-Allow-Origin' '*' always;
        proxy_pass http://127.0.0.1:3371;
    }
}
```

`/etc/nginx/conf/astriverse.conf` (backend; the `map` goes once at the top of the file if not already there):
```nginx
map $http_upgrade $connection_upgrade { default upgrade; '' close; }

server {
    listen 8443 ssl;
    ssl_certificate     /etc/certs/astrikos.xyz/fullchain.pem;
    ssl_certificate_key /etc/certs/astrikos.xyz/privkey.pem;
    server_name indocount-api.astrikos.xyz;
    location / {
        proxy_pass http://127.0.0.1:4371;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 7d;
        proxy_send_timeout 7d;
        proxy_read_timeout 7d;
        proxy_buffering off;
    }
}
```
Apply: `sudo nginx -t && sudo systemctl reload nginx`

## Cloudflare
- `indocount.astrikos.xyz` -> **Orange**
- `indocount-api.astrikos.xyz` -> **Gray** (DNS-only)

## Verify
```bash
curl -k https://indocount.astrikos.xyz:8443              # SPA HTML
curl -k https://indocount-api.astrikos.xyz:8443/api/health   # {"ok":true,...}
```
