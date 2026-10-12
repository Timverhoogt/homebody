"""Owner/CSRF protection is supplied by the existing dashboard middleware."""

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator


class SpeakerChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    volume: StrictInt | None = Field(default=None, ge=0, le=100)
    muted: StrictBool | None = None

    @model_validator(mode="after")
    def exactly_one_change(self):
        if (self.volume is None) == (self.muted is None):
            raise ValueError("Supply exactly one of volume or muted")
        return self


def register(app: FastAPI, runtime: Callable[[], Any]) -> None:
    def ready():
        value = runtime()
        if value is None:
            raise HTTPException(409, "Reachy is still starting up")
        return value

    @app.get("/api/audio/volume")
    def current(response: Response) -> dict[str, object]:
        response.headers["Cache-Control"] = "no-store"
        try:
            return ready().speaker_volume()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc), headers={"Cache-Control": "no-store"}) from exc

    @app.post("/api/audio/volume")
    def change(payload: SpeakerChange, response: Response) -> dict[str, object]:
        response.headers["Cache-Control"] = "no-store"
        try:
            return ready().set_speaker_volume(volume=payload.volume, muted=payload.muted)
        except ValueError as exc:
            raise HTTPException(422, str(exc), headers={"Cache-Control": "no-store"}) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc), headers={"Cache-Control": "no-store"}) from exc
