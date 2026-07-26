"""Доступ к секретам через macOS Keychain.

Ключ биржи никогда не лежит в файлах проекта, в .env и в переменных окружения.
Единственный путь — сюда.
"""

import subprocess

SERVICE = "trade-journal"


class KeychainError(RuntimeError):
    pass


def get(account: str) -> str:
    """Читает секрет из Keychain. Бросает KeychainError, если его там нет."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        raise KeychainError(
            f"В Keychain нет записи {SERVICE}/{account}.\n"
            f"Добавить:\n"
            f"  security add-generic-password -s {SERVICE} -a {account} -w"
        ) from None
    return out.stdout.strip()


def redact(text: str, *secrets: str) -> str:
    """Вырезает секреты из текста перед логированием или показом ошибки."""
    for secret in secrets:
        if secret and len(secret) >= 8:
            text = text.replace(secret, f"<{len(secret)}-char secret>")
    return text
