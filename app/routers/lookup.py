from fastapi import APIRouter, Query

from app.services.aggregator import IdentifierKind, detect, lookup

router = APIRouter(prefix="/api/lookup", tags=["lookup"])


@router.get("")
async def lookup_identifier(
    q: str = Query(..., min_length=1, description="ISBN-10, ISBN-13, or ComicVine issue ID"),
) -> dict:
    candidates = await lookup(q)
    kind: IdentifierKind = detect(q.replace("-", "").replace(" ", "").strip())
    return {"identifier": q, "kind": kind.value, "candidates": [c.model_dump() for c in candidates]}
