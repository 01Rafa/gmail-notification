import asyncio
import html
import os
from collections import deque
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel
from telegram import Bot

import gmail_watcher


load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
WATCHED_SENDERS = {
    item.strip().lower()
    for item in os.getenv("WATCHED_SENDERS", "").split(",")
    if item.strip()
}
PUBSUB_VERIFICATION_TOKEN = os.getenv("PUBSUB_VERIFICATION_TOKEN", "")

app = FastAPI(title="Gmail Push Alerts")
alerts: deque[dict[str, Any]] = deque(maxlen=50)


class SenderRequest(BaseModel):
    email: str


async def send_telegram_alert(alert: dict[str, Any]) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    message = (
        f"📬 [{alert['account']}] Nuevo email\n"
        f"De: {alert['from']}\n"
        f"Asunto: {alert['subject']}\n"
        f"Hora: {alert['time']}"
    )
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)


@app.on_event("startup")
async def start_watch_renewal_loop() -> None:
    async def renew_loop() -> None:
        while True:
            try:
                gmail_watcher.renew_expiring_watches()
            except Exception as exc:
                print(f"Watch renewal failed: {exc}")
            await asyncio.sleep(12 * 60 * 60)

    asyncio.create_task(renew_loop())


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request) -> dict[str, Any]:
    if PUBSUB_VERIFICATION_TOKEN:
        token = request.query_params.get("token")
        if token != PUBSUB_VERIFICATION_TOKEN:
            raise HTTPException(status_code=403, detail="Invalid webhook token")

    payload = await request.json()
    new_alerts = gmail_watcher.process_pubsub_notification(payload, WATCHED_SENDERS)
    for alert in new_alerts:
        alerts.appendleft(alert)
        await send_telegram_alert(alert)
    return {"ok": True, "alerts": len(new_alerts)}


@app.get("/alerts")
async def get_alerts() -> list[dict[str, Any]]:
    return list(alerts)


@app.post("/accounts")
async def add_account(request: Request) -> Response:
    email = await _email_from_request(request)
    try:
        auth_url = gmail_watcher.create_authorization_url(email)
    except FileNotFoundError as exc:
        return HTMLResponse(
            _error_page(
                "Falta configurar OAuth",
                str(exc),
            ),
            status_code=500,
        )
    return RedirectResponse(auth_url, status_code=303)


@app.get("/accounts")
async def get_accounts() -> list[dict[str, Any]]:
    return gmail_watcher.list_accounts()


@app.delete("/accounts/{email}")
async def delete_account(email: str) -> dict[str, Any]:
    removed = gmail_watcher.remove_account(email)
    return {"removed": removed}


@app.get("/oauth2callback")
async def oauth2callback(request: Request) -> HTMLResponse:
    response_url = _public_callback_url(request)
    state = request.query_params.get("state")
    try:
        account = gmail_watcher.exchange_oauth_callback(response_url, state)
    except Exception as exc:
        return HTMLResponse(
            _error_page(
                "No se pudo conectar la cuenta",
                str(exc),
            ),
            status_code=500,
        )
    return HTMLResponse(
        f"""
        <!doctype html>
        <html>
          <body>
            <h1>Cuenta conectada</h1>
            <p>{account.get("email")} ya esta registrada para Gmail Push Notifications.</p>
            <p>Puedes cerrar esta ventana.</p>
          </body>
        </html>
        """
    )


@app.get("/watched-senders")
async def get_watched_senders() -> list[str]:
    return sorted(WATCHED_SENDERS)


@app.post("/watched-senders")
async def add_watched_sender(request: SenderRequest) -> list[str]:
    WATCHED_SENDERS.add(request.email.strip().lower())
    return sorted(WATCHED_SENDERS)


@app.delete("/watched-senders/{email}")
async def delete_watched_sender(email: str) -> list[str]:
    WATCHED_SENDERS.discard(email.lower())
    return sorted(WATCHED_SENDERS)


@app.post("/watch/renew")
async def renew_watches() -> list[dict[str, Any]]:
    return gmail_watcher.renew_expiring_watches()


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
    return DASHBOARD_HTML


async def _email_from_request(request: Request) -> str:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        email = str(body.get("email", ""))
    else:
        from urllib.parse import parse_qs

        body = (await request.body()).decode("utf-8")
        email = parse_qs(body).get("email", [""])[0]

    email = email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Email invalido")
    return email


def _public_callback_url(request: Request) -> str:
    if gmail_watcher.PUBLIC_BASE_URL:
        query = request.url.query
        callback_url = f"{gmail_watcher.PUBLIC_BASE_URL}/oauth2callback"
        return f"{callback_url}?{query}" if query else callback_url
    return str(request.url)


def _error_page(title: str, detail: str) -> str:
    safe_title = html.escape(title)
    safe_detail = html.escape(detail)
    return f"""
    <!doctype html>
    <html lang="es">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>{safe_title}</title>
        <style>
          body {{ font-family: Arial, sans-serif; margin: 40px; color: #162033; }}
          pre {{ white-space: pre-wrap; background: #f4f7fb; padding: 16px; border-radius: 8px; }}
          a {{ color: #1f7a8c; }}
        </style>
      </head>
      <body>
        <h1>{safe_title}</h1>
        <pre>{safe_detail}</pre>
        <p><a href="/">Volver al dashboard</a></p>
      </body>
    </html>
    """


DASHBOARD_HTML = """
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Gmail Alerts</title>
  <style>
    :root { color-scheme: light dark; font-family: Inter, system-ui, Arial, sans-serif; }
    body { margin: 0; background: #f4f7fb; color: #162033; }
    header { padding: 20px 24px; background: #14213d; color: white; }
    main { max-width: 1100px; margin: 0 auto; padding: 24px; display: grid; gap: 18px; }
    section { background: white; border: 1px solid #d9e2ef; border-radius: 8px; padding: 18px; }
    h1, h2 { margin: 0 0 12px; }
    form { display: flex; gap: 8px; flex-wrap: wrap; }
    input { min-width: 260px; padding: 10px 12px; border: 1px solid #b9c6d8; border-radius: 6px; }
    button { padding: 10px 14px; border: 0; border-radius: 6px; background: #1f7a8c; color: white; cursor: pointer; }
    button.secondary { background: #52616b; }
    .alert { display: grid; gap: 5px; padding: 12px 0; border-bottom: 1px solid #e5ebf3; }
    .meta { color: #5b677a; font-size: 13px; }
    .row { display: flex; justify-content: space-between; gap: 12px; align-items: center; }
    .tag { padding: 4px 8px; border-radius: 999px; background: #e8f3f6; color: #1f6674; font-size: 12px; }
    .empty { color: #5b677a; margin: 12px 0 0; }
    .error { color: #9d1c1c; background: #fff2f2; border: 1px solid #f0b8b8; padding: 10px; border-radius: 6px; }
    ul { padding-left: 18px; }
    a { color: #1f7a8c; word-break: break-all; }
  </style>
</head>
<body>
  <header>
    <h1>Gmail Push Alerts</h1>
    <div>Alertas por Pub/Sub, Telegram y notificaciones del navegador</div>
  </header>
  <main>
    <section>
      <h2>Cuentas Gmail</h2>
      <form id="account-form" method="post" action="/accounts">
        <input id="account-email" name="email" type="email" placeholder="cuenta@gmail.com" required>
        <button type="submit">Agregar cuenta</button>
      </form>
      <p id="accounts-status" class="empty"></p>
      <ul id="accounts"></ul>
    </section>

    <section>
      <h2>Remitentes monitoreados</h2>
      <form id="sender-form">
        <input id="sender-email" type="email" placeholder="broker@company.com" required>
        <button type="submit">Agregar</button>
        <button type="button" class="secondary" id="notify-button">Activar notificaciones</button>
      </form>
      <ul id="senders"></ul>
    </section>

    <section>
      <div class="row">
        <h2>Ultimas alertas</h2>
        <span class="tag" id="count">0</span>
      </div>
      <div id="alerts"></div>
    </section>
  </main>

  <script>
    let latestId = null;
    let initialized = false;

    function beep() {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const oscillator = ctx.createOscillator();
      const gain = ctx.createGain();
      oscillator.frequency.value = 880;
      gain.gain.value = 0.08;
      oscillator.connect(gain);
      gain.connect(ctx.destination);
      oscillator.start();
      setTimeout(() => { oscillator.stop(); ctx.close(); }, 180);
    }

    function notify(alert) {
      if (Notification.permission === "granted") {
        new Notification("Nuevo email", {
          body: `${alert.account}\\nDe: ${alert.from}\\nAsunto: ${alert.subject}`,
        });
      }
    }

    async function loadAlerts() {
      const response = await fetch("/alerts");
      const data = await response.json();
      document.getElementById("count").textContent = data.length;
      const container = document.getElementById("alerts");
      container.innerHTML = data.map(alert => `
        <div class="alert">
          <div class="row"><strong>${alert.subject || "(sin asunto)"}</strong><span class="tag">${alert.account}</span></div>
          <div>De: ${alert.from}</div>
          <div class="meta">${alert.time}</div>
          <div class="meta">${alert.snippet || ""}</div>
        </div>
      `).join("");

      if (data.length > 0) {
        const currentId = data[0].id;
        if (initialized && currentId !== latestId) {
          beep();
          notify(data[0]);
        }
        latestId = currentId;
      }
      initialized = true;
    }

    async function loadAccounts() {
      const status = document.getElementById("accounts-status");
      const response = await fetch("/accounts");
      if (!response.ok) {
        status.className = "error";
        status.textContent = "No se pudieron cargar las cuentas conectadas.";
        return;
      }
      const data = await response.json();
      status.className = "empty";
      status.textContent = data.length ? "" : "No hay cuentas Gmail conectadas todavia.";
      document.getElementById("accounts").innerHTML = data.map(account => `
        <li>${account.email} <button onclick="deleteAccount('${account.email}')">Quitar</button></li>
      `).join("");
    }

    async function deleteAccount(email) {
      await fetch(`/accounts/${encodeURIComponent(email)}`, { method: "DELETE" });
      await loadAccounts();
    }

    async function loadSenders() {
      const response = await fetch("/watched-senders");
      const data = await response.json();
      document.getElementById("senders").innerHTML = data.map(email => `
        <li>${email} <button onclick="deleteSender('${email}')">Quitar</button></li>
      `).join("") || "<li class='empty'>No hay remitentes monitoreados.</li>";
    }

    async function deleteSender(email) {
      await fetch(`/watched-senders/${encodeURIComponent(email)}`, { method: "DELETE" });
      await loadSenders();
    }

    document.getElementById("notify-button").addEventListener("click", async () => {
      await Notification.requestPermission();
    });

    document.getElementById("sender-form").addEventListener("submit", async event => {
      event.preventDefault();
      const email = document.getElementById("sender-email").value;
      await fetch("/watched-senders", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      document.getElementById("sender-email").value = "";
      await loadSenders();
    });

    loadAlerts();
    loadAccounts();
    loadSenders();
    setInterval(loadAlerts, 5000);
  </script>
</body>
</html>
"""
