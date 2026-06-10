import base64
import json
import os
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google_auth_oauthlib.flow import Flow

from gmail_client import (
    CREDENTIALS_FILE,
    SCOPES,
    build_gmail_service,
    headers_to_dict,
    refresh_credentials_if_needed,
    save_credentials,
    token_path_for_email,
)


load_dotenv()

GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "gmailnotification-498918")
PUBSUB_TOPIC = os.getenv("PUBSUB_TOPIC", "gmail-notifications")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
OAUTH_REDIRECT_URI = os.getenv("OAUTH_REDIRECT_URI", "")
ACCOUNTS_FILE = Path(os.getenv("ACCOUNTS_FILE", "accounts.json"))
GOOGLE_OAUTH_CLIENT_CONFIG_JSON = os.getenv("GOOGLE_OAUTH_CLIENT_CONFIG_JSON", "")
GOOGLE_OAUTH_CLIENT_CONFIG_BASE64 = os.getenv("GOOGLE_OAUTH_CLIENT_CONFIG_BASE64", "")


def get_redirect_uri() -> str:
    if OAUTH_REDIRECT_URI:
        return OAUTH_REDIRECT_URI
    if PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL}/oauth2callback"
    return "http://localhost:8080/oauth2callback"


def _allow_local_http_redirect() -> None:
    if get_redirect_uri().startswith("http://localhost"):
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"


def _topic_name() -> str:
    return f"projects/{GOOGLE_CLOUD_PROJECT}/topics/{PUBSUB_TOPIC}"


def load_accounts() -> dict[str, dict[str, Any]]:
    if not ACCOUNTS_FILE.exists():
        return {}
    return json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))


def save_accounts(accounts: dict[str, dict[str, Any]]) -> None:
    ACCOUNTS_FILE.write_text(json.dumps(accounts, indent=2), encoding="utf-8")


def _oauth_flow() -> Flow:
    redirect_uri = get_redirect_uri()
    if GOOGLE_OAUTH_CLIENT_CONFIG_JSON:
        return Flow.from_client_config(
            json.loads(GOOGLE_OAUTH_CLIENT_CONFIG_JSON),
            scopes=SCOPES,
            redirect_uri=redirect_uri,
            autogenerate_code_verifier=False,
        )
    if GOOGLE_OAUTH_CLIENT_CONFIG_BASE64:
        decoded = base64.b64decode(GOOGLE_OAUTH_CLIENT_CONFIG_BASE64).decode("utf-8")
        return Flow.from_client_config(
            json.loads(decoded),
            scopes=SCOPES,
            redirect_uri=redirect_uri,
            autogenerate_code_verifier=False,
        )
    if CREDENTIALS_FILE.exists():
        return Flow.from_client_secrets_file(
            str(CREDENTIALS_FILE),
            scopes=SCOPES,
            redirect_uri=redirect_uri,
            autogenerate_code_verifier=False,
        )
    raise FileNotFoundError(
        "No OAuth client config found. Set GOOGLE_OAUTH_CLIENT_CONFIG_JSON "
        "or GOOGLE_OAUTH_CLIENT_CONFIG_BASE64 in Railway, or provide credentials.json locally."
    )


def create_authorization_url(email: str) -> str:
    _allow_local_http_redirect()
    flow = _oauth_flow()
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        login_hint=email,
        state=email,
    )
    return authorization_url


def exchange_oauth_callback(authorization_response: str, fallback_email: str | None = None) -> dict[str, Any]:
    _allow_local_http_redirect()
    flow = _oauth_flow()
    flow.fetch_token(authorization_response=authorization_response)
    credentials = flow.credentials

    service = build_gmail_service(credentials)
    profile = service.users().getProfile(userId="me").execute()
    email = profile.get("emailAddress") or fallback_email
    if not email:
        raise RuntimeError("No se pudo detectar la cuenta Gmail autorizada.")

    token_file = token_path_for_email(email)
    save_credentials(credentials, token_file)

    accounts = load_accounts()
    accounts[email] = {
        "email": email,
        "token_file": str(token_file),
        "history_id": profile.get("historyId"),
        "watch_expiration": None,
        "watch_error": None,
        "updated_at": int(time.time()),
    }
    save_accounts(accounts)
    try:
        return setup_watch(email)
    except Exception as exc:
        accounts = load_accounts()
        account = accounts.setdefault(email, {"email": email})
        account["watch_error"] = str(exc)
        account["updated_at"] = int(time.time())
        save_accounts(accounts)
        return account


def get_account_service(email: str) -> Any:
    accounts = load_accounts()
    account = accounts.get(email)
    if not account:
        raise KeyError(f"La cuenta {email} no esta registrada.")

    from google.oauth2.credentials import Credentials

    token_file = Path(account["token_file"])
    credentials = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    credentials = refresh_credentials_if_needed(credentials, token_file)
    if not credentials:
        raise RuntimeError(f"El token de {email} expiro y no se pudo renovar.")
    return build_gmail_service(credentials)


def setup_watch(email: str) -> dict[str, Any]:
    service = get_account_service(email)
    response = (
        service.users()
        .watch(
            userId="me",
            body={
                "topicName": _topic_name(),
            },
        )
        .execute()
    )

    accounts = load_accounts()
    account = accounts.setdefault(email, {"email": email})
    account["history_id"] = response.get("historyId", account.get("history_id"))
    account["watch_expiration"] = response.get("expiration")
    account["watch_error"] = None
    account["updated_at"] = int(time.time())
    save_accounts(accounts)
    return account


def renew_expiring_watches(max_age_seconds: int = 6 * 24 * 60 * 60) -> list[dict[str, Any]]:
    renewed: list[dict[str, Any]] = []
    now_ms = int(time.time() * 1000)
    max_age_ms = max_age_seconds * 1000
    for email, account in load_accounts().items():
        expiration = int(account.get("watch_expiration") or 0)
        if not expiration or expiration - now_ms <= max_age_ms:
            renewed.append(setup_watch(email))
    return renewed


def decode_pubsub_message(payload: dict[str, Any]) -> dict[str, Any]:
    message = payload.get("message", payload)
    data = message.get("data", "")
    if not data:
        return {}
    decoded = base64.b64decode(data).decode("utf-8")
    return json.loads(decoded)


def _extract_sender(headers: dict[str, str]) -> str:
    _, address = parseaddr(headers.get("from", ""))
    return address.lower()


def _format_internal_date(internal_date: str | None) -> str:
    if not internal_date:
        return datetime.now(timezone.utc).isoformat()
    timestamp = int(internal_date) / 1000
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _message_to_alert(account_email: str, message: dict[str, Any]) -> dict[str, Any]:
    headers = headers_to_dict(message.get("payload", {}).get("headers", []))
    return {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "account": account_email,
        "from": headers.get("from", ""),
        "sender_email": _extract_sender(headers),
        "subject": headers.get("subject", ""),
        "time": _format_internal_date(message.get("internalDate")),
        "snippet": message.get("snippet", ""),
        "starred": "STARRED" in message.get("labelIds", []),
    }


def _history_message_ids(history_items: list[dict[str, Any]]) -> set[str]:
    message_ids: set[str] = set()
    for item in history_items:
        for added in item.get("messagesAdded", []):
            message = added.get("message", {})
            if message.get("id"):
                message_ids.add(message["id"])
        for label_added in item.get("labelsAdded", []):
            labels = set(label_added.get("labelIds", []))
            message = label_added.get("message", {})
            if "STARRED" in labels and message.get("id"):
                message_ids.add(message["id"])
    return message_ids


def process_pubsub_notification(
    payload: dict[str, Any],
    watched_senders: set[str],
) -> list[dict[str, Any]]:
    notification = decode_pubsub_message(payload)
    account_email = notification.get("emailAddress")
    notification_history_id = notification.get("historyId")
    if not account_email or not notification_history_id:
        return []

    accounts = load_accounts()
    account = accounts.get(account_email)
    if not account:
        return []

    start_history_id = account.get("history_id")
    if not start_history_id:
        account["history_id"] = notification_history_id
        save_accounts(accounts)
        return []

    service = get_account_service(account_email)
    history_items: list[dict[str, Any]] = []
    page_token: str | None = None
    try:
        while True:
            request = (
                service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=start_history_id,
                    historyTypes=["messageAdded", "labelAdded"],
                    pageToken=page_token,
                )
            )
            history_response = request.execute()
            history_items.extend(history_response.get("history", []))
            page_token = history_response.get("nextPageToken")
            if not page_token:
                break
    except Exception:
        account["history_id"] = notification_history_id
        save_accounts(accounts)
        raise

    account["history_id"] = notification_history_id
    account["updated_at"] = int(time.time())
    save_accounts(accounts)

    alerts: list[dict[str, Any]] = []
    for message_id in _history_message_ids(history_items):
        message = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
        alert = _message_to_alert(account_email, message)
        if alert["sender_email"] in watched_senders or alert["starred"]:
            alerts.append(alert)
    return alerts


def list_accounts() -> list[dict[str, Any]]:
    return list(load_accounts().values())


def remove_account(email: str) -> bool:
    accounts = load_accounts()
    account = accounts.pop(email, None)
    if not account:
        return False
    save_accounts(accounts)
    token_file = Path(account.get("token_file", ""))
    if token_file.exists() and token_file.is_file():
        token_file.unlink()
    return True
