import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import recorder

app = FastAPI()

ASPECT_RATIOS = set(recorder.VIEWPORTS)
SPEEDS = set(recorder.SPEEDS)


def _validate_common(url: str, aspect_ratio: str):
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(400, "URL має починатися з http:// або https://")
    if aspect_ratio not in ASPECT_RATIOS:
        raise HTTPException(400, "Невідоме співвідношення сторін")


class JobRequest(BaseModel):
    url: str
    aspect_ratio: str
    speed: str
    block_ads: bool = False


@app.post("/jobs")
def create_job(body: JobRequest):
    _validate_common(body.url, body.aspect_ratio)
    if body.speed not in SPEEDS:
        raise HTTPException(400, "Невідома швидкість")
    job_id = recorder.create_job(body.url, body.aspect_ratio, body.speed, body.block_ads)
    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    job = recorder.get_job(job_id)
    if not job:
        raise HTTPException(404, "not found")
    return job


@app.get("/jobs/{job_id}/file")
def job_file(job_id: str):
    path = recorder.job_file_path(job_id)
    if not path or not os.path.exists(path):
        raise HTTPException(404, "not ready")
    return FileResponse(path, media_type="video/mp4", filename="scroll-recording.mp4")


@app.delete("/jobs/{job_id}")
def cancel_job(job_id: str):
    recorder.request_cancel(job_id)
    return {"ok": True}


class PreviewRequest(BaseModel):
    url: str
    aspect_ratio: str
    block_ads: bool = False


@app.post("/preview")
def create_preview(body: PreviewRequest):
    _validate_common(body.url, body.aspect_ratio)
    try:
        session_id, screenshot, width, height = recorder.create_preview(
            body.url, body.aspect_ratio, body.block_ads
        )
    except RuntimeError as exc:
        raise HTTPException(429, str(exc))
    except Exception as exc:
        raise HTTPException(400, f"Не вдалося відкрити сторінку: {exc}")
    return {"session_id": session_id, "screenshot": screenshot, "width": width, "height": height}


class PointRequest(BaseModel):
    x: float
    y: float


@app.post("/preview/{session_id}/remove")
def remove_element(session_id: str, body: PointRequest):
    try:
        screenshot = recorder.remove_at_point(session_id, body.x, body.y)
    except KeyError:
        raise HTTPException(404, "session not found")
    return {"screenshot": screenshot}


@app.post("/preview/{session_id}/undo")
def undo_element(session_id: str):
    try:
        screenshot = recorder.undo_last(session_id)
    except KeyError:
        raise HTTPException(404, "session not found")
    return {"screenshot": screenshot}


class RecordFromPreviewRequest(BaseModel):
    speed: str


@app.post("/preview/{session_id}/record")
def record_from_preview(session_id: str, body: RecordFromPreviewRequest):
    if body.speed not in SPEEDS:
        raise HTTPException(400, "Невідома швидкість")
    try:
        job_id = recorder.start_recording_from_preview(session_id, body.speed)
    except KeyError:
        raise HTTPException(404, "session not found")
    return {"job_id": job_id}


@app.delete("/preview/{session_id}")
def cancel_preview(session_id: str):
    recorder.close_preview(session_id)
    return {"ok": True}
