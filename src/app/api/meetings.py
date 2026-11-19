from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import FileResponse

from ..db.models import MeetingStatus
from ..services.errors import Invalid, NotFound, ServiceError
from ..services.meeting_service import MeetingService, action_item_dto, meeting_dto
from .deps import Authenticated, QueueDep, SessionDep, StorageDep, http_error
from .schemas import ActionItemDTO, CreateMeetingBody, JobDTO, MeetingDTO, TranscriptDTO

log = logging.getLogger(__name__)

router = APIRouter(prefix="/meetings", tags=["meetings"], dependencies=[Authenticated])

# 4 hours of 64 kbps mono AAC is roughly 115 MB; anything larger is a mistake.
MAX_AUDIO_BYTES = 512 * 1024 * 1024

# The client writes AAC/MP4. Sniffing the first bytes stops an arbitrary payload from
# being handed to ffmpeg by the worker, even if its Content-Type claims audio.
_AUDIO_MAGIC: dict[str, tuple[int, bytes]] = {
    "audio/mp4": (4, b"ftyp"),
    "audio/x-m4a": (4, b"ftyp"),
    "audio/m4a": (4, b"ftyp"),
    "audio/wav": (0, b"RIFF"),
    "audio/x-wav": (0, b"RIFF"),
    "audio/mpeg": (0, b"ID3"),
    "audio/ogg": (0, b"OggS"),
    "audio/webm": (0, b"\x1aE\xdf\xa3"),
}


def _is_audio(content_type: str, head: bytes) -> bool:
    base = content_type.split(";", 1)[0].strip().lower()
    # MPEG audio and ADTS AAC start with a frame sync rather than a container header.
    if base in ("audio/mpeg", "audio/mp3", "audio/aac"):
        return head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)
    signature = _AUDIO_MAGIC.get(base)
    if signature is None:
        return False
    offset, magic = signature
    return head[offset:offset + len(magic)] == magic


@router.get("", response_model=list[MeetingDTO])
async def list_meetings(session: SessionDep, storage: StorageDep,
                        limit: int = Query(default=100, ge=1, le=1000),
                        q: str | None = None) -> list[MeetingDTO]:
    service = MeetingService(session, storage)
    meetings = await service.list(limit=limit, query=q)
    return [meeting_dto(m) for m in meetings]


@router.post("", response_model=MeetingDTO, status_code=status.HTTP_201_CREATED)
async def create_meeting(body: CreateMeetingBody, session: SessionDep,
                         storage: StorageDep) -> MeetingDTO:
    try:
        meeting = await MeetingService(session, storage).create(body)
    except ServiceError as error:
        raise http_error(error) from error
    return meeting_dto(meeting)


@router.get("/{meeting_id}", response_model=MeetingDTO)
async def get_meeting(meeting_id: str, session: SessionDep, storage: StorageDep,
                      includeTranscript: bool = True) -> MeetingDTO:
    service = MeetingService(session, storage)
    try:
        meeting = await service.get(meeting_id)
        job = await service.latest_job(meeting_id)
        transcript = None
        if includeTranscript:
            try:
                transcript = await service.transcript(meeting_id)
            except ServiceError:
                transcript = None
    except ServiceError as error:
        raise http_error(error) from error
    return meeting_dto(meeting, transcript=transcript, job=job, include_transcript=includeTranscript)


@router.delete("/{meeting_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_meeting(meeting_id: str, session: SessionDep, storage: StorageDep) -> Response:
    try:
        await MeetingService(session, storage).delete(meeting_id)
    except ServiceError as error:
        raise http_error(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{meeting_id}/audio", response_model=MeetingDTO)
async def upload_audio(meeting_id: str, request: Request, session: SessionDep,
                       storage: StorageDep) -> MeetingDTO:
    """Streams the recording straight into object storage.

    The body is read in chunks rather than buffered, so a long meeting does not have
    to fit in the API process's memory.
    """
    content_type = (request.headers.get("content-type", "audio/mp4").split(";", 1)[0].strip()
                    or "audio/mp4")[:100]
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        raise http_error(Invalid("invalid Content-Length header")) from None
    if declared > MAX_AUDIO_BYTES:
        raise http_error(Invalid(f"recording is too large ({declared} bytes)"))

    total = 0
    checked = False

    async def stream():
        nonlocal total, checked
        pending: list[bytes] = []
        async for chunk in request.stream():
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_AUDIO_BYTES:
                raise Invalid("recording exceeded the maximum size")
            if checked:
                yield chunk
                continue
            pending.append(chunk)
            head = b"".join(pending)
            if len(head) < 16:
                continue
            if not _is_audio(content_type, head):
                raise Invalid(f"unsupported or unrecognised audio format ({content_type})")
            checked = True
            for buffered in pending:
                yield buffered
            pending.clear()
        if pending:
            if not _is_audio(content_type, b"".join(pending)):
                raise Invalid(f"unsupported or unrecognised audio format ({content_type})")
            for buffered in pending:
                yield buffered

    try:
        meeting = await MeetingService(session, storage).store_audio(meeting_id, stream(), content_type)
    except ServiceError as error:
        raise http_error(error) from error
    return meeting_dto(meeting)


@router.get("/{meeting_id}/audio")
async def get_audio(meeting_id: str, session: SessionDep, storage: StorageDep) -> FileResponse:
    """Serves the stored recording so the client can play it back inline."""
    try:
        meeting = await MeetingService(session, storage).get(meeting_id)
        if not meeting.audio_key:
            raise NotFound(f"no audio for meeting {meeting_id}")
        path = await storage.get_path(meeting.audio_key)
    except FileNotFoundError as error:
        raise http_error(NotFound(f"no audio for meeting {meeting_id}")) from error
    except ServiceError as error:
        raise http_error(error) from error
    return FileResponse(
        path,
        media_type=meeting.audio_content_type or "audio/mp4",
        headers={"Content-Disposition": "inline", "Cache-Control": "private, max-age=3600"},
    )


@router.post("/{meeting_id}/process", response_model=JobDTO,
             status_code=status.HTTP_202_ACCEPTED)
async def process_meeting(meeting_id: str, session: SessionDep, storage: StorageDep,
                          queue: QueueDep) -> JobDTO:
    """Creates a job and returns immediately.

    Transcription and analysis take minutes; doing them inside a request would tie up
    a worker and time out the client.
    """
    service = MeetingService(session, storage)
    try:
        # Row-locked so two concurrent PROCESS calls cannot both enqueue a job.
        meeting = await service.lock(meeting_id)
        if not meeting.audio_key:
            raise Invalid("no audio has been uploaded for this meeting")

        existing = await service.latest_job(meeting_id)
        if existing is not None and existing.status.value in ("queued", "running"):
            return JobDTO(id=existing.id, status=existing.status.value, stage=existing.stage,
                          progress=existing.progress, meetingId=meeting_id)

        job = await queue.enqueue(session, kind="process_meeting", meeting_id=meeting_id)
        await service.set_status(meeting_id, MeetingStatus.queued)
    except ServiceError as error:
        raise http_error(error) from error

    return JobDTO(id=job.id, status=job.status.value, stage=job.stage,
                  progress=job.progress, meetingId=meeting_id)


@router.get("/{meeting_id}/transcript", response_model=TranscriptDTO)
async def get_transcript(meeting_id: str, session: SessionDep, storage: StorageDep) -> TranscriptDTO:
    try:
        transcript = await MeetingService(session, storage).transcript(meeting_id)
    except ServiceError as error:
        raise http_error(error) from error
    from .schemas import TranscriptSegmentDTO
    return TranscriptDTO(
        segments=[TranscriptSegmentDTO(id=s.get("id"), start=float(s.get("start", 0)),
                                       end=float(s.get("end", 0)), speaker=s.get("speaker"),
                                       text=str(s.get("text", "")))
                  for s in (transcript.segments or [])],
        language=transcript.language,
        duration=transcript.duration)


@router.get("/{meeting_id}/action-items", response_model=list[ActionItemDTO])
async def get_action_items(meeting_id: str, session: SessionDep,
                           storage: StorageDep) -> list[ActionItemDTO]:
    try:
        items = await MeetingService(session, storage).action_items(meeting_id)
    except ServiceError as error:
        raise http_error(error) from error
    return [action_item_dto(i) for i in items]
