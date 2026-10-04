import asyncio
from decimal import Decimal
from pathlib import Path

from anki_telegram_bot.images import ImageService
from anki_telegram_bot.models import CardDraft, ImageEvaluation, ImageSource, WikimediaImageCandidate


class FakePolza:
    def __init__(self) -> None:
        self.generated = False
        self.variant_calls: list[list[str]] = []
        self.evaluated_urls: list[str] = []

    async def build_wikimedia_query_variants(self, draft: CardDraft, failed_queries: list[str], **kwargs) -> list[str]:
        self.variant_calls.append(failed_queries)
        return ["pool table", "Billard"]

    async def evaluate_image(self, draft: CardDraft, image_url: str, **kwargs) -> ImageEvaluation:
        self.evaluated_urls.append(image_url)
        return ImageEvaluation(is_match=image_url.endswith("match.jpg"), reason="test")

    async def generate_image(self, draft: CardDraft, media_dir: Path, **kwargs) -> Path:
        self.generated = True
        return media_dir / "generated.jpg"


def test_attach_image_generates_without_wikimedia_search(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        polza = FakePolza()
        service = ImageService(polza=polza, media_dir=tmp_path)
        searches: list[str] = []

        async def fake_search(query: str, limit: int = 3) -> list[WikimediaImageCandidate]:
            searches.append(query)
            return []

        monkeypatch.setattr(service, "search_wikimedia", fake_search)
        draft = CardDraft(
            id=1,
            user_id=42,
            source_text="Бильярд",
            image_query="Billard spielen",
            word="Billard",
            word_or_phrase_de="Billard",
        )

        updated = await service.attach_image(draft)
        await service.close()

        assert searches == []
        assert polza.variant_calls == []
        assert polza.evaluated_urls == []
        assert updated.image_source == ImageSource.POLZA_GENERATED
        assert updated.image_path == tmp_path / "generated.jpg"
        assert polza.generated is True

    asyncio.run(run())


def test_attach_image_adds_generation_cost_only(tmp_path, monkeypatch) -> None:
    async def run() -> None:
        class CostPolza(FakePolza):
            async def generate_image(self, draft, media_dir, cost_tracker=None):
                cost_tracker.record_response(
                    {"id": "image", "usage": {"cost_rub": 2.00}},
                    event="image_generation.sync",
                )
                self.generated = True
                return media_dir / "generated.jpg"

        service = ImageService(polza=CostPolza(), media_dir=tmp_path)
        draft = CardDraft(
            id=1,
            user_id=42,
            source_text="schweigen",
            word="schweigen",
            polza_cost_rub=Decimal("1.00"),
            polza_cost_available=True,
        )

        updated = await service.attach_image(draft)
        await service.close()

        assert updated.image_source == ImageSource.POLZA_GENERATED
        assert updated.polza_cost_rub == Decimal("3.00")
        assert updated.polza_cost_complete is True
        assert updated.polza_cost_available is True

    asyncio.run(run())
