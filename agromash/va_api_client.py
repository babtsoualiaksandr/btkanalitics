import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import requests
from django.db import transaction

from agromash.models import AccountVideoAnalytics


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: Optional[str] = None


class VAApiClient:
    """HTTP-клиент для VideoAnalytics API с корректным refresh/login.

    Алгоритм:
      - обычный запрос с access_token
      - если 401: refresh (POST /oauth2/v1/auth/refresh) с refresh_token
      - если refresh неуспешен или после refresh снова 401: login (POST /oauth2/v1/auth/authenticate)
      - ограничение попыток + backoff + обработка сетевых ошибок/таймаутов
      - обновление токенов в БД атомарно (transaction + select_for_update)
    """

    def __init__(
        self,
        *,
        account_id: int,
        base_url: str,
        session: Optional[requests.Session] = None,
        max_attempts: int = 4,
        base_backoff_sec: float = 0.4,
        timeout: Tuple[float, float] = (7.0, 30.0),
    ) -> None:
        self._account_id = account_id
        self._base_url = base_url.rstrip('/')
        self._session = session or requests.Session()
        self._max_attempts = max(1, max_attempts)
        self._base_backoff_sec = max(0.0, base_backoff_sec)
        self._timeout = timeout

    # -----------------
    # Public API
    # -----------------
    def ensure_authenticated(self) -> None:
        """Гарантирует, что в БД есть валидная пара токенов (как минимум access_token).

        Если access_token отсутствует — делает login.
        """
        account = self._get_account()
        if account.access_token:
            return
        self._login_and_persist()

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[Tuple[float, float]] = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> requests.Response:
        """Запрос с авто-refresh/login.

        Возвращает requests.Response. Исключения requests (ConnectionError/Timeout)
        прокидываются наружу только после исчерпания попыток.
        """
        url = self._build_url(path)
        timeout = timeout or self._timeout

        last_exc: Optional[BaseException] = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                account = self._get_account()
                req_headers = self._auth_headers(account.access_token, headers=headers)

                resp = self._session.request(
                    method=method,
                    url=url,
                    headers=req_headers,
                    timeout=timeout,
                    stream=stream,
                    **kwargs,
                )

                # 401: пробуем refresh → повтор; если снова 401 или refresh неуспешен → login → повтор
                if resp.status_code == 401:
                    # Важно не читать resp.text при stream=True
                    resp.close()

                    refresh_ok = self._refresh_and_persist_if_possible()
                    if refresh_ok:
                        logger.info(
                            "VA API: access_token обновлён через refresh (account_id=%s)",
                            self._account_id,
                        )
                        account = self._get_account()
                        req_headers = self._auth_headers(account.access_token, headers=headers)
                        resp = self._session.request(
                            method=method,
                            url=url,
                            headers=req_headers,
                            timeout=timeout,
                            stream=stream,
                            **kwargs,
                        )
                        if resp.status_code != 401:
                            return resp
                        resp.close()
                        logger.warning(
                            "VA API: 401 после refresh, выполняю login (account_id=%s)",
                            self._account_id,
                        )
                    else:
                        logger.warning(
                            "VA API: refresh не удался, выполняю login (account_id=%s)",
                            self._account_id,
                        )

                    self._login_and_persist()
                    account = self._get_account()
                    req_headers = self._auth_headers(account.access_token, headers=headers)
                    resp = self._session.request(
                        method=method,
                        url=url,
                        headers=req_headers,
                        timeout=timeout,
                        stream=stream,
                        **kwargs,
                    )
                    if resp.status_code != 401:
                        logger.info(
                            "VA API: выполнен login, запрос повторён успешно (account_id=%s)",
                            self._account_id,
                        )
                        return resp

                    resp.close()
                    logger.error(
                        "VA API: 401 даже после login (account_id=%s, attempt=%s/%s)",
                        self._account_id,
                        attempt,
                        self._max_attempts,
                    )
                    self._backoff(attempt)
                    continue

                # Мягкий ретрай на некоторые 5xx
                if resp.status_code in (502, 503, 504):
                    resp.close()
                    logger.warning(
                        "VA API: %s от сервера, retry (account_id=%s, attempt=%s/%s)",
                        resp.status_code,
                        self._account_id,
                        attempt,
                        self._max_attempts,
                    )
                    self._backoff(attempt)
                    continue

                return resp

            except (requests.Timeout, requests.ConnectionError) as exc:
                last_exc = exc
                logger.warning(
                    "VA API: сетевая ошибка %s, retry (account_id=%s, attempt=%s/%s)",
                    exc.__class__.__name__,
                    self._account_id,
                    attempt,
                    self._max_attempts,
                )
                self._backoff(attempt)
                continue

        assert last_exc is not None
        raise last_exc

    # -----------------
    # Internal helpers
    # -----------------
    def _build_url(self, path: str) -> str:
        if path.startswith('http://') or path.startswith('https://'):
            return path
        if not path.startswith('/'):
            path = '/' + path
        return f"{self._base_url}{path}"

    def _auth_headers(
        self,
        access_token: Optional[str],
        *,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        merged: Dict[str, str] = {}
        if headers:
            merged.update(headers)
        if access_token:
            merged['Authorization'] = f"Bearer {access_token}"
        return merged

    def _get_account(self) -> AccountVideoAnalytics:
        # Всегда читаем актуальные токены из БД (избегаем гонок между воркерами).
        return AccountVideoAnalytics.objects.get(pk=self._account_id)

    def _backoff(self, attempt: int) -> None:
        # Экспоненциальный backoff с небольшим джиттером.
        base = self._base_backoff_sec * (2 ** max(0, attempt - 1))
        delay = base + random.random() * 0.1
        time.sleep(delay)

    def _refresh_and_persist_if_possible(self) -> bool:
        account = self._get_account()
        if not account.refresh_token:
            logger.info(
                "VA API: refresh_token отсутствует (account_id=%s)",
                self._account_id,
            )
            return False

        url = self._build_url('/oauth2/v1/auth/refresh')
        payload = {"refresh_token": account.refresh_token}

        try:
            resp = self._session.post(url, json=payload, timeout=self._timeout)
        except (requests.Timeout, requests.ConnectionError) as exc:
            logger.warning(
                "VA API: refresh — сетевая ошибка %s (account_id=%s)",
                exc.__class__.__name__,
                self._account_id,
            )
            return False

        if resp.status_code == 401:
            logger.info(
                "VA API: refresh отклонён (401) (account_id=%s)",
                self._account_id,
            )
            return False

        if resp.status_code != 200:
            logger.info(
                "VA API: refresh неуспешен (status=%s) (account_id=%s)",
                resp.status_code,
                self._account_id,
            )
            return False

        try:
            data = resp.json()
        except ValueError:
            logger.info(
                "VA API: refresh вернул не-JSON (account_id=%s)",
                self._account_id,
            )
            return False

        access_token = data.get('access_token')
        refresh_token = data.get('refresh_token')
        if not access_token:
            logger.info(
                "VA API: refresh вернул пустой access_token (account_id=%s)",
                self._account_id,
            )
            return False

        self._persist_tokens(
            TokenPair(access_token=access_token, refresh_token=refresh_token),
            keep_old_refresh_if_missing=True,
        )
        return True

    def _login_and_persist(self) -> None:
        account = self._get_account()
        url = self._build_url('/oauth2/v1/auth/authenticate')
        payload = {
            "name": account.name,
            "password": account.password,
            "rememberme": True,
        }

        resp = self._session.post(url, json=payload, timeout=self._timeout)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Login failed for account_id={self._account_id}: status={resp.status_code}"
            )

        data = resp.json()
        access_token = data.get('access_token')
        refresh_token = data.get('refresh_token')
        if not access_token:
            raise RuntimeError(
                f"Login response missing access_token for account_id={self._account_id}"
            )

        self._persist_tokens(TokenPair(access_token=access_token, refresh_token=refresh_token))

    def _persist_tokens(
        self,
        tokens: TokenPair,
        *,
        keep_old_refresh_if_missing: bool = False,
    ) -> None:
        """Атомарно обновляет токены в БД одним коммитом."""
        with transaction.atomic():
            acc = AccountVideoAnalytics.objects.select_for_update().get(pk=self._account_id)
            acc.access_token = tokens.access_token
            if tokens.refresh_token is not None:
                acc.refresh_token = tokens.refresh_token
            elif not keep_old_refresh_if_missing:
                acc.refresh_token = None
            acc.save(update_fields=["access_token", "refresh_token"])

