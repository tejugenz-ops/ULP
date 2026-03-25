import uuid
from typing import Union

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    File,
    FileStatus,
    Job,
    JobStatus,
    JobType,
    User,
    async_session,
)

_ID = Union[uuid.UUID, str]


def _uuid(v: _ID) -> uuid.UUID:
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))


# ── helpers ──────────────────────────────────────────────────────────


def _session() -> AsyncSession:
    return async_session()


# ── User ─────────────────────────────────────────────────────────────


async def get_or_create_user(
    user_id: int,
    username: str | None = None,
    first_name: str | None = None,
) -> User:
    async with _session() as s:
        user = await s.get(User, user_id)
        if user is None:
            user = User(id=user_id, username=username, first_name=first_name)
            s.add(user)
            await s.commit()
            await s.refresh(user)
        return user


# ── File ─────────────────────────────────────────────────────────────


async def create_file(
    user_id: int,
    original_name: str,
    size_bytes: int | None = None,
    mime_type: str | None = None,
    parent_id: uuid.UUID | None = None,
) -> File:
    async with _session() as s:
        f = File(
            user_id=user_id,
            original_name=original_name,
            size_bytes=size_bytes,
            mime_type=mime_type,
            parent_id=parent_id,
        )
        s.add(f)
        await s.commit()
        await s.refresh(f)
        return f


async def update_file(
    file_id: _ID, **kwargs
) -> None:
    fid = _uuid(file_id)
    async with _session() as s:
        await s.execute(update(File).where(File.id == fid).values(**kwargs))
        await s.commit()


async def get_file(file_id: _ID) -> File | None:
    fid = _uuid(file_id)
    async with _session() as s:
        return await s.get(File, fid)


async def list_user_files(user_id: int) -> list[File]:
    async with _session() as s:
        result = await s.execute(
            select(File)
            .where(File.user_id == user_id, File.status == FileStatus.READY)
            .order_by(File.created_at.desc())
        )
        return list(result.scalars().all())


async def list_all_ready_files() -> list[File]:
    async with _session() as s:
        result = await s.execute(
            select(File)
            .where(File.status == FileStatus.READY)
            .order_by(File.created_at.desc())
        )
        return list(result.scalars().all())


async def delete_file(file_id: _ID) -> None:
    fid = _uuid(file_id)
    async with _session() as s:
        f = await s.get(File, fid)
        if f:
            await s.delete(f)
            await s.commit()


# ── Job ──────────────────────────────────────────────────────────────


async def create_job(
    user_id: int,
    job_type: JobType,
    file_id: _ID | None = None,
) -> Job:
    async with _session() as s:
        j = Job(user_id=user_id, job_type=job_type, file_id=_uuid(file_id) if file_id else None)
        s.add(j)
        await s.commit()
        await s.refresh(j)
        return j


async def update_job(job_id: _ID, **kwargs) -> None:
    jid = _uuid(job_id)
    async with _session() as s:
        await s.execute(update(Job).where(Job.id == jid).values(**kwargs))
        await s.commit()


async def get_job(job_id: _ID) -> Job | None:
    jid = _uuid(job_id)
    async with _session() as s:
        return await s.get(Job, jid)


async def list_active_jobs(user_id: int) -> list[Job]:
    async with _session() as s:
        result = await s.execute(
            select(Job)
            .where(
                Job.user_id == user_id,
                Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
            .order_by(Job.created_at.desc())
        )
        return list(result.scalars().all())


async def count_active_jobs(user_id: int) -> int:
    jobs = await list_active_jobs(user_id)
    return len(jobs)


async def get_db_stats() -> dict:
    """Return total files, total size, and counts per status."""
    async with _session() as s:
        # Total ready files + size
        row = (await s.execute(
            select(func.count(), func.coalesce(func.sum(File.size_bytes), 0))
            .where(File.status == FileStatus.READY)
        )).one()
        ready_count, ready_bytes = int(row[0]), int(row[1])

        # Downloading files
        dl_count = (await s.execute(
            select(func.count()).where(File.status == FileStatus.DOWNLOADING)
        )).scalar() or 0

        # All files total
        total = (await s.execute(select(func.count()).select_from(File))).scalar() or 0

        return {
            "total_files": total,
            "ready_count": ready_count,
            "ready_bytes": ready_bytes,
            "downloading": dl_count,
        }


async def list_all_active_jobs() -> list[Job]:
    async with _session() as s:
        result = await s.execute(
            select(Job)
            .where(Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
            .order_by(Job.created_at.desc())
        )
        return list(result.scalars().all())
