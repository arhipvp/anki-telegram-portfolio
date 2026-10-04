import logging
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from .costs import PolzaCostTracker
from .logging_config import is_full_debug_enabled, log_event
from .models import CardDraft, ImageSource, WikimediaImageCandidate
from .polza import PolzaClient


logger = logging.getLogger(__name__)


class ImageService:
    def __init__(self, polza: PolzaClient, media_dir: Path) -> None:
        self.polza = polza
        self.media_dir = media_dir
        self.client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "User-Agent": "AnkiTelegramBot/0.1 (https://github.com/arhipvp/anki-telegram-portfolio; personal learning bot)"
            },
        )

    async def close(self) -> None:
        await self.client.aclose()
        log_event(logger, logging.INFO, "images.client.closed")

    async def attach_image(self, draft: CardDraft) -> CardDraft:
        started_at = time.perf_counter()
        cost_tracker = PolzaCostTracker(
            amount_rub=draft.polza_cost_rub,
            complete=draft.polza_cost_complete,
            available=draft.polza_cost_available,
        )
        log_event(logger, logging.INFO, "images.attach.start", draft_id=draft.id, draft=draft if is_full_debug_enabled() else None)
        log_event(
            logger,
            logging.INFO,
            "images.attach.generate_start",
            draft_id=draft.id,
            image_query=draft.image_query,
        )
        path = await self.polza.generate_image(draft, self.media_dir, cost_tracker=cost_tracker)
        updated = draft.model_copy(
            update={
                "image_path": path,
                "image_source": ImageSource.POLZA_GENERATED,
                **_cost_fields(cost_tracker),
            }
        )
        log_event(
            logger,
            logging.INFO,
            "images.attach.generated_success",
            draft_id=draft.id,
            image_path=path,
            duration_ms=_duration_ms(started_at),
        )
        return updated

    async def build_wikimedia_queries(
        self,
        draft: CardDraft,
        cost_tracker: PolzaCostTracker,
    ) -> list[str]:
        local_queries = _dedupe_texts([
            draft.image_query,
            draft.word,
            draft.word_or_phrase_de,
        ])
        fallback_local_queries = _dedupe_texts([
            f"{draft.word or draft.word_or_phrase_de} object",
            f"{draft.word or draft.word_or_phrase_de} activity",
        ])
        ai_queries = await self.polza.build_wikimedia_query_variants(
            draft,
            [*local_queries, *fallback_local_queries],
            cost_tracker=cost_tracker,
        )
        all_queries = _dedupe_texts([*local_queries[:2], *ai_queries, *local_queries[2:], *fallback_local_queries])
        log_event(logger, logging.INFO, "images.wikimedia.queries", draft_id=draft.id, queries=all_queries)
        return all_queries

    async def find_matching_candidate(
        self,
        draft: CardDraft,
        candidates: list[WikimediaImageCandidate],
        cost_tracker: PolzaCostTracker,
    ) -> WikimediaImageCandidate | None:
        for candidate in candidates:
            try:
                evaluation = await self.polza.evaluate_image(
                    draft,
                    candidate.image_url,
                    cost_tracker=cost_tracker,
                )
            except httpx.HTTPError:
                log_event(logger, logging.WARNING, "images.evaluate.error", draft_id=draft.id, candidate=candidate, exc_info=True)
                continue
            log_event(
                logger,
                logging.INFO,
                "images.evaluate.result",
                draft_id=draft.id,
                candidate=candidate,
                is_match=evaluation.is_match,
                reason=evaluation.reason,
            )
            if evaluation.is_match:
                return candidate
        return None

    async def search_wikimedia(self, query: str, limit: int = 3) -> list[WikimediaImageCandidate]:
        started_at = time.perf_counter()
        log_event(logger, logging.INFO, "images.wikimedia.search.start", query=query, limit=limit)
        response = await self.client.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": f"filetype:bitmap {query}",
                "gsrnamespace": 6,
                "gsrlimit": limit,
                "prop": "imageinfo|info",
                "iiprop": "url|mime|size",
                "inprop": "url",
                "format": "json",
                "formatversion": 2,
                "origin": "*",
            },
        )
        response.raise_for_status()
        pages = response.json().get("query", {}).get("pages", [])
        candidates: list[WikimediaImageCandidate] = []
        for page in pages:
            image_info = (page.get("imageinfo") or [{}])[0]
            image_url = image_info.get("url")
            if not image_url:
                continue
            candidates.append(
                WikimediaImageCandidate(
                    title=page.get("title", ""),
                    image_url=image_url,
                    page_url=page.get("fullurl"),
                    mime=image_info.get("mime"),
                    width=image_info.get("width"),
                    height=image_info.get("height"),
                )
            )
        log_event(
            logger,
            logging.INFO,
            "images.wikimedia.search.success",
            query=query,
            limit=limit,
            status_code=response.status_code,
            candidate_count=len(candidates),
            candidates=candidates if is_full_debug_enabled() else None,
            duration_ms=_duration_ms(started_at),
        )
        return candidates

    async def download(self, url: str, stem: str) -> Path:
        started_at = time.perf_counter()
        log_event(logger, logging.INFO, "images.download.start", url=url, stem=stem)
        response = await self.client.get(url)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        suffix = ".jpg"
        if "png" in content_type:
            suffix = ".png"
        elif "webp" in content_type:
            suffix = ".webp"
        elif "gif" in content_type:
            suffix = ".gif"
        path = self.media_dir / f"{quote(stem, safe='')}{suffix}"
        path.write_bytes(response.content)
        log_event(
            logger,
            logging.INFO,
            "images.download.success",
            url=url,
            path=path,
            content_type=content_type,
            size_bytes=len(response.content),
            duration_ms=_duration_ms(started_at),
        )
        return path


def _duration_ms(started_at: float) -> int:
    return round((time.perf_counter() - started_at) * 1000)


def _cost_fields(cost_tracker: PolzaCostTracker) -> dict[str, object]:
    return {
        "polza_cost_rub": cost_tracker.amount_rub,
        "polza_cost_complete": cost_tracker.complete,
        "polza_cost_available": cost_tracker.available,
    }


def _dedupe_texts(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = value.strip() if value else ""
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result
