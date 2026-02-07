import logging
import os
import random
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import certifi
import requests
from django.db import transaction

from agromash.models import AccountVideoAnalytics


logger = logging.getLogger(__name__)


class VAAuthError(RuntimeError):
    """Ошибка аутентификации в VideoAnalytics.

    Используется, чтобы верхний уровень мог отличить auth-проблемы
    (неверные креды/429 rate limit) от прочих ошибок.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        retry_after_sec: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_sec = retry_after_sec


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
        # SSL verify behaviour:
        #   - None  -> auto (use system CA bundle if available, else requests default)
        #   - True  -> requests default verification
        #   - False -> disable verification (НЕ рекомендовано)
        #   - str   -> path to CA bundle
        verify: Optional[object] = None,
        max_attempts: int = 4,
        base_backoff_sec: float = 0.4,
        timeout: Tuple[float, float] = (7.0, 30.0),
    ) -> None:
        self._account_id = account_id
        self._base_url = base_url.rstrip('/')
        self._session = session or requests.Session()
        self._verify = verify
        self._max_attempts = max(1, max_attempts)
        self._base_backoff_sec = max(0.0, base_backoff_sec)
        self._timeout = timeout

        # Cache for combined CA bundle path (per process).
        self._combined_ca_bundle_path: Optional[str] = None

    def _build_combined_ca_bundle(self) -> str:
        """Создаёт (один раз на процесс) объединённый CA bundle: system + certifi.

        Зачем:
        - в некоторых окружениях `certifi` в venv может быть неполным/устаревшим
        - в некоторых окружениях системный bundle может быть не синхронизирован с Python
        - объединение повышает шанс найти issuer/intermediate при неполной цепочке.
        """

        if self._combined_ca_bundle_path and os.path.exists(self._combined_ca_bundle_path):
            return self._combined_ca_bundle_path

        system_bundle = "/etc/ssl/certs/ca-certificates.crt"
        certifi_bundle = certifi.where()
        # Сервер периодически (или всегда) не отдаёт intermediate в цепочке.
        # Добавляем нужный intermediate в наш bundle, чтобы валидация проходила.
        bundled_intermediate = os.path.join(
            os.path.dirname(__file__),
            "certs",
            "globalsign_gcc_r6_alphassl_ca_2025.pem",
        )

        parts: List[str] = []
        for p in (system_bundle, certifi_bundle, bundled_intermediate):
            try:
                if p and os.path.exists(p):
                    with open(p, "r", encoding="utf-8", errors="ignore") as f:
                        parts.append(f.read())
            except Exception:
                # Не валим процесс, если не смогли прочитать один из bundle.
                continue

        if not parts:
            # Fallback: пусть requests сам выберет.
            self._combined_ca_bundle_path = ""
            return ""

        # Пишем в /tmp, чтобы не требовать прав на запись в проект/venv.
        fd, out_path = tempfile.mkstemp(prefix="va_ca_bundle_", suffix=".pem")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.write("\n\n".join(parts))
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            raise

        self._combined_ca_bundle_path = out_path
        return out_path

    def _resolve_verify(self, *, verify: Optional[object] = None) -> object:
        """Подбирает CA bundle для requests.

        Наблюдаемая проблема: периодически падаем с
        SSLCertVerificationError: unable to get local issuer certificate.

        По запросу делаем поведение предсказуемым: по умолчанию используем
        bundle из `certifi` (внутри venv), а не системный.
        """

        effective = verify if verify is not None else self._verify
        if effective is not None:
            return effective

        # Default: combined bundle (system + certifi) for maximum compatibility.
        combined = self._build_combined_ca_bundle()
        if combined:
            return combined

        # Last resort: certifi bundle
        return certifi.where()

    def _verify_candidates(self) -> List[object]:
        """Список кандидатов verify для ретраев при SSL ошибках."""

        candidates: List[object] = []
        candidates.append(self._resolve_verify())

        system_bundle = "/etc/ssl/certs/ca-certificates.crt"
        if os.path.exists(system_bundle):
            candidates.append(system_bundle)

        candidates.append(certifi.where())
        candidates.append(True)

        # Удаляем дубликаты, сохраняя порядок.
        uniq: List[object] = []
        for c in candidates:
            if c not in uniq:
                uniq.append(c)
        return uniq

    def _session_request(self, *, verify: Optional[object] = None, **kwargs: Any) -> requests.Response:
        # requests expects verify: bool | str
        return self._session.request(verify=self._resolve_verify(verify=verify), **kwargs)

    # -----------------
    # Public API
    # -----------------
    def ensure_authenticated(self) -> None:
        """Гарантирует, что в БД есть валидная пара токенов (как минимум access_token).

        Если access_token отсутствует — делает login.
        """
        account = self._get_account()
        if account.access_token:
            logger.debug(
                "VA API: ensure_authenticated ok (access_token уже есть) (account_id=%s)",
                self._account_id,
            )
            return
        logger.info(
            "VA API: access_token отсутствует, выполняю login (account_id=%s)",
            self._account_id,
        )
        self._login_and_persist()
        logger.info(
            "VA API: login выполнен, токены сохранены (account_id=%s)",
            self._account_id,
        )

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

        # Критично для первичного запуска сервиса: если токенов ещё нет в БД,
        # делаем login ДО первого HTTP-запроса, чтобы сразу сохранить токены.
        # (Иначе первый запрос уйдёт без Authorization и будет лишний 401.)
        self.ensure_authenticated()

        last_exc: Optional[BaseException] = None

        for attempt in range(1, self._max_attempts + 1):
            verify_override: Optional[object] = None
            try:
                account = self._get_account()
                req_headers = self._auth_headers(account.access_token, headers=headers)

                # Если verify не задан явно — при SSL-ошибках попробуем разные bundle.
                verify_override = kwargs.get("verify", None)
                if "verify" not in kwargs:
                    verify_candidates = self._verify_candidates()
                    idx = min(attempt - 1, len(verify_candidates) - 1)
                    verify_override = verify_candidates[idx]

                resp = self._session_request(
                    method=method,
                    url=url,
                    headers=req_headers,
                    timeout=timeout,
                    stream=stream,
                    verify=verify_override,
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
                        resp = self._session_request(
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
                    resp = self._session_request(
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
                    last_exc = VAAuthError(
                        f"401 даже после login (account_id={self._account_id})",
                        status_code=401,
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
                    last_exc = RuntimeError(
                        f"VA API: сервер вернул {resp.status_code} (account_id={self._account_id})"
                    )
                    self._backoff(attempt)
                    continue

                return resp

            except (requests.Timeout, requests.ConnectionError, requests.exceptions.SSLError) as exc:
                last_exc = exc
                if isinstance(exc, requests.exceptions.SSLError):
                    logger.warning(
                        "VA API: SSL ошибка при verify=%r (account_id=%s, attempt=%s/%s)",
                        verify_override,
                        self._account_id,
                        attempt,
                        self._max_attempts,
                    )
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
            resp = self._session_request(method="POST", url=url, json=payload, timeout=self._timeout)
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
        logger.info(
            "VA API: refresh успешен, токены обновлены (account_id=%s)",
            self._account_id,
        )
        return True

    def _login_and_persist(self) -> None:
        # Делаем login под DB-lock'ом (select_for_update), чтобы при параллельном старте
        # нескольких воркеров не было «шторма» логинов.
        with transaction.atomic():
            account = AccountVideoAnalytics.objects.select_for_update().get(pk=self._account_id)

            # Если другой процесс уже успел залогиниться — ничего не делаем.
            if account.access_token:
                logger.debug(
                    "VA API: login не требуется (токен уже выставлен другим процессом) (account_id=%s)",
                    self._account_id,
                )
                return

            logger.info(
                "VA API: выполняю POST authenticate (account_id=%s)",
                self._account_id,
            )

            url = self._build_url('/oauth2/v1/auth/authenticate')
            payload = {
                "name": account.name,
                "password": account.password,
                "rememberme": True,
            }

            resp = self._session_request(method="POST", url=url, json=payload, timeout=self._timeout)
            if resp.status_code != 200:
                retry_after = None
                if resp.status_code == 429:
                    try:
                        retry_after = int(resp.headers.get('Retry-After') or 0) or None
                    except Exception:
                        retry_after = None
                logger.warning(
                    "VA API: authenticate вернул status=%s (account_id=%s)",
                    resp.status_code,
                    self._account_id,
                )
                raise VAAuthError(
                    f"Login failed for account_id={self._account_id}: status={resp.status_code}",
                    status_code=resp.status_code,
                    retry_after_sec=retry_after,
                )

            data = resp.json()
            access_token = data.get('access_token')
            refresh_token = data.get('refresh_token')
            if not access_token:
                raise RuntimeError(
                    f"Login response missing access_token for account_id={self._account_id}"
                )

            account.access_token = access_token
            account.refresh_token = refresh_token
            account.save(update_fields=["access_token", "refresh_token"])

            logger.info(
                "VA API: authenticate ok, access_token сохранён (account_id=%s)",
                self._account_id,
            )

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
