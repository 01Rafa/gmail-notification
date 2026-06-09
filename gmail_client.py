import base64
import os
import re
import webbrowser
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from wsgiref.simple_server import make_server

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build


load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

GMAIL_USER = os.getenv("GMAIL_USER", "rafa980915@gmail.com")
CREDENTIALS_FILE = Path(os.getenv("CREDENTIALS_FILE", "credentials.json"))
TOKEN_FILE = Path(os.getenv("TOKEN_FILE", "token.json"))
TOKENS_DIR = Path(os.getenv("TOKENS_DIR", "tokens"))
OAUTH_REDIRECT_URI = os.getenv(
    "OAUTH_REDIRECT_URI",
    "http://localhost:8080/callback",
)

if OAUTH_REDIRECT_URI.startswith("http://localhost"):
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"


def token_path_for_email(email: str) -> Path:
    safe_email = re.sub(r"[^A-Za-z0-9_.-]+", "_", email.lower()).strip("_")
    return TOKENS_DIR / f"{safe_email}.json"


def build_gmail_service(credentials: Credentials) -> Any:
    return build("gmail", "v1", credentials=credentials)


def save_credentials(credentials: Credentials, token_file: Path) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(credentials.to_json(), encoding="utf-8")


def load_credentials_from_file(token_file: Path) -> Credentials | None:
    if not token_file.exists():
        return None
    return Credentials.from_authorized_user_file(str(token_file), SCOPES)


def refresh_credentials_if_needed(
    credentials: Credentials | None,
    token_file: Path,
) -> Credentials | None:
    if not credentials:
        return None
    if credentials.valid:
        return credentials
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        save_credentials(credentials, token_file)
        return credentials
    return None


def get_credentials_for_account(email: str) -> Credentials:
    token_file = token_path_for_email(email)
    credentials = load_credentials_from_file(token_file)
    credentials = refresh_credentials_if_needed(credentials, token_file)
    if not credentials:
        raise FileNotFoundError(
            f"No hay token valido para {email}. Agrega la cuenta desde POST /accounts."
        )
    return credentials


class OAuthCallbackHandler:
    def __init__(self) -> None:
        self.authorization_response: str | None = None
        self.error: str | None = None

    def app(self, environ: dict[str, Any], start_response: Any) -> list[bytes]:
        scheme = environ.get("wsgi.url_scheme", "http")
        host = environ.get("HTTP_HOST", "localhost:8080")
        path = environ.get("PATH_INFO", "")
        query = environ.get("QUERY_STRING", "")
        full_url = f"{scheme}://{host}{path}"
        if query:
            full_url = f"{full_url}?{query}"

        params = parse_qs(query)
        self.error = params.get("error", [None])[0]
        self.authorization_response = full_url

        status = "200 OK"
        body = (
            "<html><body><h1>Autenticacion completada</h1>"
            "<p>Ya puedes cerrar esta ventana y volver a la terminal.</p>"
            "</body></html>"
        )
        if self.error:
            status = "400 Bad Request"
            body = (
                "<html><body><h1>Autenticacion cancelada</h1>"
                f"<p>Error: {self.error}</p></body></html>"
            )

        start_response(status, [("Content-Type", "text/html; charset=utf-8")])
        return [body.encode("utf-8")]


def _validate_credentials_file() -> None:
    if not CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"No se encontro {CREDENTIALS_FILE}. "
            "Descarga el OAuth client JSON desde Google Cloud Console y guardalo ahi."
        )


def _run_local_web_oauth_flow() -> Credentials:
    _validate_credentials_file()
    if OAUTH_REDIRECT_URI.startswith("http://localhost"):
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

    redirect = urlparse(OAUTH_REDIRECT_URI)
    host = redirect.hostname or "localhost"
    port = redirect.port or 8080

    flow = Flow.from_client_secrets_file(
        str(CREDENTIALS_FILE),
        scopes=SCOPES,
        redirect_uri=OAUTH_REDIRECT_URI,
    )
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        login_hint=GMAIL_USER,
    )

    callback = OAuthCallbackHandler()
    print("Abriendo autorizacion OAuth en el navegador...")
    print(authorization_url)
    webbrowser.open(authorization_url)

    with make_server(host, port, callback.app) as server:
        print(f"Esperando callback OAuth en {OAUTH_REDIRECT_URI}")
        server.handle_request()

    if callback.error:
        raise RuntimeError(f"OAuth fallo o fue cancelado: {callback.error}")
    if not callback.authorization_response:
        raise RuntimeError("No se recibio respuesta OAuth.")

    flow.fetch_token(authorization_response=callback.authorization_response)
    credentials = flow.credentials
    save_credentials(credentials, TOKEN_FILE)
    return credentials


def get_credentials() -> Credentials:
    _validate_credentials_file()
    credentials = load_credentials_from_file(TOKEN_FILE)
    credentials = refresh_credentials_if_needed(credentials, TOKEN_FILE)
    if credentials:
        return credentials
    return _run_local_web_oauth_flow()


def get_gmail_service(account_email: str | None = None) -> Any:
    credentials = (
        get_credentials_for_account(account_email) if account_email else get_credentials()
    )
    return build_gmail_service(credentials)


def send_email(to: str, subject: str, body: str, account_email: str | None = None) -> dict[str, Any]:
    service = get_gmail_service(account_email)
    message = MIMEText(body, "plain", "utf-8")
    message["to"] = to
    message["from"] = account_email or GMAIL_USER
    message["subject"] = subject

    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    sent_message = (
        service.users()
        .messages()
        .send(userId="me", body={"raw": raw_message})
        .execute()
    )
    return sent_message


def headers_to_dict(headers: list[dict[str, str]]) -> dict[str, str]:
    return {header["name"].lower(): header["value"] for header in headers}


def get_emails(max_results: int = 10, account_email: str | None = None) -> list[dict[str, Any]]:
    service = get_gmail_service(account_email)
    response = (
        service.users()
        .messages()
        .list(userId="me", maxResults=max_results)
        .execute()
    )
    messages = response.get("messages", [])
    emails: list[dict[str, Any]] = []

    for message in messages:
        detail = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=message["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
        headers = headers_to_dict(detail.get("payload", {}).get("headers", []))
        emails.append(
            {
                "id": detail.get("id"),
                "thread_id": detail.get("threadId"),
                "from": headers.get("from", ""),
                "subject": headers.get("subject", ""),
                "date": headers.get("date", ""),
                "snippet": detail.get("snippet", ""),
                "label_ids": detail.get("labelIds", []),
            }
        )

    return emails


if __name__ == "__main__":
    for email in get_emails(max_results=10):
        print(f"{email['date']} | {email['from']} | {email['subject']}")
