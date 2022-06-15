from fastapi import APIRouter
from starlette.responses import PlainTextResponse


router = APIRouter(tags=['private'])


@router.get('/not_gonna_happen')
async def lmao(_) -> PlainTextResponse:
    return PlainTextResponse(
        "you don't have a token, ask VJ. tokens are one time use, so if you had one and used it you'll need to apply for another"
    )
