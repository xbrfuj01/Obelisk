import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import recorder

app = FastAPI()

ASPECT_RATIOS = set(recorder.VIEWPORTS)
SPEEDS = set(recorder.SPEEDS)


class JobRequest(BaseModel):
    url: str
    aspect_ratio: str
    speed: str


@app.post("/jobs")
def create_job(body: JobRequest):
    if not (body.url.startswith("http://") or body.url.startswith("https://")):
        raise HTTPException(400, "URL має починатися з http:// або https://")
    if body.aspect_ratio not in ASPECT_RATIOS:
        raise HTTPException(400, "Невідоме співвідношення сторін")
    if body.speed not in SPEEDS:
        raise HTTPException(400, "Невідома швидкість")
    job_id = recorder.create_job(body.url, body.aspect_ratio, body.speed)
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
