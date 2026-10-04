"""Client HTTP partagé : User-Agent réaliste, délais entre requêtes, retries
exponentiels sur 429/5xx et détection des pages de blocage (captcha)."""
from __future__ import annotations

import logging
import random
import time

import requests

log = logging.getLogger("product_hunter.http")

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
]

RETRY_STATUSES = {429, 500, 502, 503, 504}


class BlockedError(Exception):
    """La source a renvoyé une page de blocage (captcha, 403...)."""


class HttpClient:
    def __init__(self, http_cfg: dict | None = None):
        cfg = http_cfg or {}
        self.timeout = cfg.get("timeout_s", 20)
        self.retries = cfg.get("retries", 3)
        self.backoff = cfg.get("backoff_s", 2.0)
        self.delay = cfg.get("delay_between_requests_s", 1.5)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": random.choice(USER_AGENTS),
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        })
        self._last_request = 0.0

    def _throttle(self) -> None:
        """Respecte un délai minimum (avec un peu d'aléa) entre deux requêtes."""
        wait = self.delay * random.uniform(0.8, 1.3) - (time.time() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.time()

    def get(self, url: str, **kwargs) -> requests.Response:
        """GET avec retries. Lève BlockedError (403/captcha) ou requests.RequestException."""
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:   # réseau, DNS, timeout
                last_exc = exc
                log.warning("GET %s échec réseau (%s), tentative %d", url, exc, attempt + 1)
            else:
                if resp.status_code == 403:
                    raise BlockedError(f"403 Forbidden sur {url}")
                if resp.status_code in RETRY_STATUSES:
                    last_exc = requests.HTTPError(f"HTTP {resp.status_code} sur {url}", response=resp)
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        time.sleep(min(int(retry_after), 60))
                    log.warning("GET %s -> %s, tentative %d", url, resp.status_code, attempt + 1)
                else:
                    resp.raise_for_status()
                    return resp
            if attempt < self.retries:
                time.sleep(self.backoff * (2 ** attempt) + random.uniform(0, 1))
            # Change d'User-Agent entre deux tentatives
            self.session.headers["User-Agent"] = random.choice(USER_AGENTS)
        assert last_exc is not None
        raise last_exc
