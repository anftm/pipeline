import asyncio
import hashlib
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator, Awaitable, Callable
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from brotli_asgi import BrotliMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope
from .config import APP_ROOT
from .data_loader import initialize_search_tokenizer
from . import bootstrap_api, parse_api, preview_api, proofread_api, reindex_api, runtime, search_api, source_files


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    await asyncio.to_thread(initialize_search_tokenizer)
    reindex_api.resume_pending_snapshot_publish()
    asyncio.create_task(runtime.initialize_when_ready())
    asyncio.create_task(runtime.auxiliary_warmup_loop())
    asyncio.create_task(source_files.source_mirror_warmup_loop())
    yield
    await source_files.source_client.aclose()


app = FastAPI(title="BHA Search Lite", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(
    BrotliMiddleware,
    quality=5,
    minimum_size=500,
    gzip_fallback=True,
    excluded_handlers=[r"^/api/(?:download/|preview/source(?:-page)?/)"],
)

@app.middleware("http")
async def track_user_activity(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    return await runtime.track_request(request, call_next)

app.include_router(runtime.router)
app.include_router(search_api.router)
app.include_router(bootstrap_api.router)
app.include_router(preview_api.router)
app.include_router(parse_api.router)
app.include_router(proofread_api.router)
app.include_router(reindex_api.router)
app.include_router(source_files.router)
static_dir = APP_ROOT / "static"
versioned_assets = {
    name: hashlib.sha256((static_dir / name).read_bytes()).hexdigest()[:16]
    for name in ("app.js", "style.css", "tts-client.js")
}
index_html = (static_dir / "index.html").read_text(encoding="utf-8")
for asset_name, version in versioned_assets.items():
    index_html = index_html.replace(
        f'/static/{asset_name}"', f'/static/{asset_name}?v={version}"'
    )
# The Reader/TTS client is lazily injected on the first preview, so it has no
# script tag to rewrite; expose its content hash for the loader instead.
index_html = index_html.replace(
    "__BHA_TTS_CLIENT_VERSION__", versioned_assets["tts-client.js"]
)
index_etag = f'"{hashlib.sha256(index_html.encode()).hexdigest()}"'
# The shell is identical for every visitor and revalidated by ETag. A short
# shared cache lets repeat navigation skip the HF-proxy round trip; the ETag
# still invalidates it immediately after a deploy.
index_cache_control = "public, max-age=60, must-revalidate"


class VersionedStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if path == "sw.js":
            response.headers["Service-Worker-Allowed"] = "/"
        query = parse_qs(scope.get("query_string", b"").decode("ascii", errors="ignore"))
        if versioned_assets.get(path) in query.get("v", []):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response


def index_response(request: Request) -> Response:
    headers = {"Cache-Control": index_cache_control, "ETag": index_etag}
    if request.headers.get("if-none-match") == index_etag:
        return Response(status_code=304, headers=headers)
    return HTMLResponse(index_html, headers=headers)


app.mount("/static", VersionedStaticFiles(directory=static_dir), name="static")
@app.get("/")

def root(request: Request) -> Response:
    return index_response(request)
@app.get("/{path:path}")

def spa_fallback(path: str, request: Request) -> Response:
    if path == "api" or path.startswith("api/"):
        return JSONResponse({"error": "not found"}, status_code=404)
    file_path = (static_dir / path).resolve()
    if file_path.is_relative_to(static_dir.resolve()) and file_path.exists() and file_path.is_file():
        return FileResponse(file_path)
    return index_response(request)
