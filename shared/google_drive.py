"""
Загрузка скриншотов в Google Drive.

Два режима:
1. Через веб-приложение Apps Script (если задан proxy.json / SHEETS_PROXY_URL) —
   файл сохраняется от имени владельца таблицы, ключ Google на ПК не нужен.
2. Напрямую сервисным аккаунтом в папку DRIVE_FOLDER_ID — только если эта папка
   принадлежит вам и расшарена на аккаунт (у самого аккаунта квоты нет).
"""

import base64
import json
import logging
import os
import uuid

from google.auth.transport.requests import AuthorizedSession

logger = logging.getLogger(__name__)

UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FILES_URL = "https://www.googleapis.com/drive/v3/files"


def get_folder_id() -> str:
    """ID папки в Drive для прямой загрузки (может быть пустым)."""
    return os.getenv("DRIVE_FOLDER_ID", "").strip()


def photo_proxy() -> tuple[str, str]:
    """Адрес и секрет веб-приложения для сохранения фото.

    Сначала отдельные переменные PHOTO_PROXY_URL/PHOTO_PROXY_SECRET
    (только фото через скрипт), затем общий proxy.json / SHEETS_PROXY_URL.
    """
    url = os.getenv("PHOTO_PROXY_URL", "").strip()
    if url:
        return url, os.getenv("PHOTO_PROXY_SECRET", "").strip()

    from shared.google_sheets import proxy_config

    return proxy_config()


def is_configured() -> bool:
    """Есть ли вообще способ сохранить скриншот."""
    url, _ = photo_proxy()
    return bool(url or get_folder_id())


def upload_image(data: bytes, filename: str) -> str:
    """Сохраняет картинку и возвращает ссылку. Пустая строка — не получилось."""
    url, secret = photo_proxy()
    if url:
        return _upload_via_proxy(url, secret, data, filename)
    if get_folder_id():
        return _upload_direct(data, filename)
    logger.warning(
        "Скриншот не сохранён: не настроено хранилище "
        "(нужен proxy.json с адресом Apps Script или DRIVE_FOLDER_ID)"
    )
    return ""


def _upload_via_proxy(url: str, secret: str, data: bytes, filename: str) -> str:
    """Отправляет фото в Apps Script (base64) — он сохранит его в ваш Drive."""
    import requests

    try:
        response = requests.post(
            url,
            json={
                "action": "photo",
                "secret": secret,
                "name": filename,
                "mime": "image/jpeg",
                "data_b64": base64.b64encode(data).decode("ascii"),
            },
            timeout=120,
        )
        if response.status_code >= 400:
            logger.error("Фото: сервер ответил %s: %s", response.status_code, response.text[:200])
            return ""
        result = response.json()
        if not result.get("ok"):
            logger.error("Фото: сервер вернул ошибку: %s", result.get("error"))
            return ""
        return str(result.get("url", ""))
    except Exception as exc:
        logger.error("Фото: ошибка отправки: %s: %s", type(exc).__name__, exc)
        return ""


def _upload_direct(data: bytes, filename: str) -> str:
    """Прямая загрузка сервисным аккаунтом в папку DRIVE_FOLDER_ID."""
    try:
        from shared.google_sheets import get_credentials

        session = AuthorizedSession(get_credentials())

        boundary = "======dsh" + uuid.uuid4().hex
        meta = json.dumps({"name": filename, "parents": [get_folder_id()]}, ensure_ascii=False)
        body = (
            (
                f"--{boundary}\r\n"
                "Content-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{meta}\r\n"
                f"--{boundary}\r\n"
                "Content-Type: image/jpeg\r\n\r\n"
            ).encode("utf-8")
            + data
            + f"\r\n--{boundary}--\r\n".encode("utf-8")
        )

        response = session.post(
            UPLOAD_URL,
            params={"uploadType": "multipart", "fields": "id,webViewLink"},
            data=body,
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
            timeout=60,
        )
        if response.status_code >= 400:
            logger.error(
                "Drive: не удалось загрузить %s: %s %s",
                filename, response.status_code, response.text[:300],
            )
            return ""

        info = response.json()
        file_id = info.get("id")
        if not file_id:
            return ""

        session.post(
            f"{FILES_URL}/{file_id}/permissions",
            json={"role": "reader", "type": "anyone"},
            timeout=30,
        )

        link = info.get("webViewLink")
        if not link:
            response = session.get(
                f"{FILES_URL}/{file_id}", params={"fields": "webViewLink"}, timeout=30
            )
            link = response.json().get("webViewLink", "")
        return link or ""
    except Exception as exc:
        logger.error("Drive: ошибка загрузки %s: %s: %s", filename, type(exc).__name__, exc)
        return ""
