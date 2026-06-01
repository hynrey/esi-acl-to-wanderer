import logging

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.schemas import AccessListDTO

logger = logging.getLogger(__name__)

ESI_BASE = "https://esi.evetech.net"


def _should_retry(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (420, 429, 500, 502, 503, 504)
    return isinstance(exc, httpx.TransportError)


class EsiClient:
    def __init__(self, user_agent: str, compatibility_date: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=ESI_BASE,
            headers={
                "User-Agent": user_agent,
                "Accept": "application/json",
                "X-Compatibility-Date": compatibility_date,
            },
            timeout=30,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "EsiClient":
        return self

    async def __aexit__(self, *args) -> None:
        await self.aclose()

    def _check_error_limit(self, response: httpx.Response) -> None:
        remain = response.headers.get("X-ESI-Error-Limit-Remain")
        if remain and int(remain) == 0:
            reset = int(response.headers.get("X-ESI-Error-Limit-Reset", 60))
            logger.warning("ESI error limit reached, backing off %ds", reset)
            raise httpx.HTTPStatusError("ESI error limit reached", request=response.request, response=response)

    @retry(
        retry=retry_if_exception(_should_retry),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    async def get_access_list(
        self,
        character_id: int,
        access_list_id: int,
        token: str,
        etag: str | None = None,
    ) -> tuple[AccessListDTO | None, str | None]:
        """
        Returns (AccessListDTO, new_etag) or (None, old_etag) on 304.
        """
        headers = {"Authorization": f"Bearer {token}"}
        if etag:
            headers["If-None-Match"] = etag

        resp = await self._client.get(
            f"/characters/{character_id}/access-lists/{access_list_id}",
            headers=headers,
        )

        if resp.status_code == 304:
            return None, etag

        self._check_error_limit(resp)
        resp.raise_for_status()

        new_etag = resp.headers.get("ETag")
        return AccessListDTO.from_esi_response(resp.json()), new_etag
