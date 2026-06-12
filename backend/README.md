[简体中文](README_ZH.md) | [日本語](README_JA.md)

### FoxAIHub

FoxAIHub focuses on delivering efficient and reliable AI model API services, covering text-to-image, text-to-video, image-to-video, and music generation API, helping you stay ahead at the intersection of creativity and technology.

[FoxAIHUb](https://foxaihub.com)


### Unofficial API

This is an unofficial API based on Python and FastAPI. It currently supports generating songs, lyrics, etc.  
It comes with a built-in token maintenance and keep-alive feature, so you don't have to worry about the token expiring.

### Features

- Automatic token maintenance and keep-alive
- Fully asynchronous, fast, suitable for later expansion
- Simple code, easy to maintain, convenient for secondary development


### Usage

#### Configuration

Create `.env` and fill in the Suno session ID and cookie. Album planning and
image generation also require a Gemini API key.

```dotenv
BASE_URL=https://studio-api.prod.suno.com
SUNO_MODEL=chirp-fenix
SESSION_ID=...
COOKIE=...
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-2.5-flash
GEMINI_IMAGE_MODEL=gemini-3.1-flash-image-preview
CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
CORS_ORIGIN_REGEX=https?://(localhost|127\.0\.0\.1|10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})(:\d+)?
```

`chirp-fenix` is the internal model key currently used by the Suno web
application for the v5.5 model. Access still depends on the signed-in Suno plan.

Legacy models use the original repository transport: `/api/generate/v2/`,
`Content-Type: text/plain;charset=UTF-8`, Bearer JWT only, and no browser Cookie
header. `chirp-fenix` uses `/api/generate/v2-web/`, matching the successful v5.5
requests recorded by this project.

On Windows, `start_suno_server.py` automatically reuses the Gemini API key
stored by the ContentProduction desktop application's settings. A
`GEMINI_API_KEY` environment variable or `.env` value takes precedence.

These are initially obtained from the browser, and will be automatically kept alive later.

![cookie](./images/cover.png)


#### Run

Install dependencies 

```bash
pip3 install -r requirements.txt
```

Start the integrated Suno and album-production backend:

```bash
python start_suno_server.py
```

Runtime diagnostics are written to both the console and:

```text
logs/suno-backend.log
```

The rotating log records job IDs, track IDs, model names, input lengths,
upstream HTTP status/error bodies, retry stages, and tracebacks. Cookies,
session IDs, JWTs, and full lyrics are not logged.

The server provides both the existing Suno-compatible routes and the new
album-production routes under `/api/v1`.

Main workflow:

```text
POST /api/v1/albums
POST /api/v1/albums/{album_id}/plan
PUT  /api/v1/tracks/{track_id}/lyrics
PUT  /api/v1/tracks/{track_id}/style
POST /api/v1/tracks/{track_id}/generate
GET  /api/v1/jobs/{job_id}
POST /api/v1/albums/{album_id}/covers/generate
POST /api/v1/albums/{album_id}/videos/render
```

#### Docker

```bash
docker compose build && docker compose up
```

#### Documentation

After setting up the service, visit /docs

![docs](./images/docs.png)
