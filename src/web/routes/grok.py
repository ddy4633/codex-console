"""
Grok/xAI 注册路由
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from ...config.constants import EmailServiceType
from ...config.settings import get_settings
from ...core.grok import GrokRegistrationEngine
from ...database import crud
from ...database.models import Account, EmailService as EmailServiceModel, RegistrationTask
from ...database.session import get_db
from ...services import EmailServiceFactory
from ..task_manager import task_manager

logger = logging.getLogger(__name__)
router = APIRouter()

grok_batches: Dict[str, dict] = {}


class GrokTaskCreate(BaseModel):
    email_service_type: str = "tempmail"
    email_service_id: Optional[int] = None
    proxy: Optional[str] = None
    password: Optional[str] = None
    user_data_dir: str = ""
    cf_clearance: str = ""
    cf_bm: str = ""
    cf_cookie_header: str = ""
    user_agent: str = ""


class GrokBatchCreate(GrokTaskCreate):
    count: int = 1
    interval_min: int = 20
    interval_max: int = 60


class GrokTaskResponse(BaseModel):
    id: int
    task_uuid: str
    status: str
    email_service_id: Optional[int] = None
    proxy: Optional[str] = None
    logs: List[str] = []
    result: Optional[dict] = None
    error_message: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


class GrokBatchResponse(BaseModel):
    batch_id: str
    count: int
    tasks: List[GrokTaskResponse]


def task_to_response(task: RegistrationTask) -> GrokTaskResponse:
    logs = task.logs.splitlines() if task.logs else []
    return GrokTaskResponse(
        id=task.id,
        task_uuid=task.task_uuid,
        status=task.status,
        email_service_id=task.email_service_id,
        proxy=task.proxy,
        logs=logs,
        result=task.result,
        error_message=task.error_message,
        created_at=task.created_at.isoformat() if task.created_at else None,
        started_at=task.started_at.isoformat() if task.started_at else None,
        completed_at=task.completed_at.isoformat() if task.completed_at else None,
    )


def _build_service(request: GrokTaskCreate):
    try:
        service_type = EmailServiceType(request.email_service_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"无效的邮箱服务类型: {request.email_service_type}") from exc

    settings = get_settings()
    service_name = None

    if service_type == EmailServiceType.TEMPMAIL:
        config = {
            "base_url": settings.tempmail_base_url,
            "timeout": settings.tempmail_timeout,
            "max_retries": settings.tempmail_max_retries,
        }
        if request.proxy:
            config["proxy_url"] = request.proxy
        return service_type, None, config, service_name

    with get_db() as db:
        query = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == service_type.value,
            EmailServiceModel.enabled == True,
        )
        if request.email_service_id:
            query = query.filter(EmailServiceModel.id == request.email_service_id)
        db_service = query.order_by(EmailServiceModel.priority.asc()).first()

        if not db_service:
            raise HTTPException(status_code=400, detail=f"没有可用的 {service_type.value} 邮箱服务")

        config = (db_service.config or {}).copy()
        if request.proxy and "proxy_url" not in config:
            config["proxy_url"] = request.proxy
        service_name = db_service.name
        return service_type, db_service.id, config, service_name


def _run_sync_grok_task(task_uuid: str, request: GrokTaskCreate, batch_id: str = "", log_prefix: str = ""):
    with get_db() as db:
        try:
            if task_manager.is_cancelled(task_uuid):
                logger.info("Grok 任务 %s 已取消，跳过执行", task_uuid)
                return

            task = crud.update_registration_task(
                db,
                task_uuid,
                status="running",
                started_at=datetime.utcnow(),
            )
            if not task:
                logger.error("Grok 任务不存在: %s", task_uuid)
                return

            task_manager.update_status(task_uuid, "running")

            service_type, resolved_service_id, config, service_name = _build_service(request)
            if resolved_service_id:
                crud.update_registration_task(db, task_uuid, email_service_id=resolved_service_id)

            email_service = EmailServiceFactory.create(service_type, config, name=service_name)
            log_callback = task_manager.create_log_callback(task_uuid, prefix=log_prefix, batch_id=batch_id)

            engine = GrokRegistrationEngine(
                email_service=email_service,
                proxy_url=request.proxy,
                password=request.password,
                user_data_dir=request.user_data_dir,
                cf_clearance=request.cf_clearance,
                cf_bm=request.cf_bm,
                cf_cookie_header=request.cf_cookie_header,
                user_agent=request.user_agent,
                callback_logger=log_callback,
                task_uuid=task_uuid,
            )
            result = engine.run()

            if result.success:
                crud.update_registration_task(
                    db,
                    task_uuid,
                    status="completed",
                    completed_at=datetime.utcnow(),
                    result=result.to_dict(),
                )
                task_manager.update_status(task_uuid, "completed", email=result.email)
            else:
                crud.update_registration_task(
                    db,
                    task_uuid,
                    status="failed",
                    completed_at=datetime.utcnow(),
                    error_message=result.error_message,
                    result=result.to_dict(),
                )
                task_manager.update_status(task_uuid, "failed", error=result.error_message)
        except HTTPException as exc:
            logger.error("Grok 任务配置错误: %s", exc.detail)
            crud.update_registration_task(
                db,
                task_uuid,
                status="failed",
                completed_at=datetime.utcnow(),
                error_message=exc.detail,
            )
            task_manager.update_status(task_uuid, "failed", error=exc.detail)
        except Exception as exc:
            logger.exception("Grok 任务异常: %s", task_uuid)
            crud.update_registration_task(
                db,
                task_uuid,
                status="failed",
                completed_at=datetime.utcnow(),
                error_message=str(exc),
            )
            task_manager.update_status(task_uuid, "failed", error=str(exc))


async def run_grok_task(task_uuid: str, request: GrokTaskCreate, batch_id: str = "", log_prefix: str = ""):
    loop = task_manager.get_loop()
    if loop is None:
        loop = asyncio.get_event_loop()
        task_manager.set_loop(loop)

    task_manager.update_status(task_uuid, "pending")
    task_manager.add_log(task_uuid, f"{log_prefix} [系统] Grok 任务 {task_uuid[:8]} 已加入队列" if log_prefix else f"[系统] Grok 任务 {task_uuid[:8]} 已加入队列")

    await loop.run_in_executor(
        task_manager.executor,
        _run_sync_grok_task,
        task_uuid,
        request,
        batch_id,
        log_prefix,
    )


async def run_grok_batch(batch_id: str, task_uuids: List[str], request: GrokBatchCreate):
    task_manager.add_batch_log(batch_id, f"[系统] Grok 批量任务启动，共 {len(task_uuids)} 个任务")

    completed = 0
    success = 0
    failed = 0

    for index, task_uuid in enumerate(task_uuids, start=1):
        if task_manager.is_batch_cancelled(batch_id):
            task_manager.add_batch_log(batch_id, "[系统] 检测到批量取消信号，停止继续创建 Grok 账号")
            break

        prefix = f"[任务{index}]"
        await run_grok_task(task_uuid, request, batch_id=batch_id, log_prefix=prefix)

        with get_db() as db:
            task = crud.get_registration_task_by_uuid(db, task_uuid)
            if task and task.status == "completed":
                success += 1
            else:
                failed += 1
        completed += 1
        task_manager.update_batch_status(
            batch_id,
            completed=completed,
            success=success,
            failed=failed,
            current_index=index,
        )

        if index < len(task_uuids):
            wait_seconds = random.randint(
                min(request.interval_min, request.interval_max),
                max(request.interval_min, request.interval_max),
            )
            task_manager.add_batch_log(batch_id, f"[系统] 下一次 Grok 注册前等待 {wait_seconds} 秒")
            await asyncio.sleep(wait_seconds)

    final_status = "cancelled" if task_manager.is_batch_cancelled(batch_id) else "completed"
    task_manager.update_batch_status(
        batch_id,
        status=final_status,
        completed=completed,
        success=success,
        failed=failed,
        finished=True,
    )
    task_manager.add_batch_log(batch_id, f"[系统] Grok 批量任务结束，成功 {success}，失败 {failed}")


@router.post("/start", response_model=GrokTaskResponse)
async def start_grok_registration(request: GrokTaskCreate, background_tasks: BackgroundTasks):
    task_uuid = str(uuid.uuid4())

    with get_db() as db:
        task = crud.create_registration_task(
            db,
            task_uuid=task_uuid,
            email_service_id=request.email_service_id,
            proxy=request.proxy,
        )

    background_tasks.add_task(run_grok_task, task_uuid, request)
    return task_to_response(task)


@router.post("/batch-start", response_model=GrokBatchResponse)
async def start_grok_batch(request: GrokBatchCreate, background_tasks: BackgroundTasks):
    if request.count < 1 or request.count > 100:
        raise HTTPException(status_code=400, detail="批量数量必须在 1 到 100 之间")

    task_responses = []
    task_uuids = []

    with get_db() as db:
        for _ in range(request.count):
            task_uuid = str(uuid.uuid4())
            task = crud.create_registration_task(
                db,
                task_uuid=task_uuid,
                email_service_id=request.email_service_id,
                proxy=request.proxy,
            )
            task_uuids.append(task_uuid)
            task_responses.append(task_to_response(task))

    batch_id = str(uuid.uuid4())
    task_manager.init_batch(batch_id, len(task_uuids))
    grok_batches[batch_id] = {
        "task_uuids": task_uuids,
        "created_at": datetime.utcnow().isoformat(),
        "request": request.model_dump(),
    }
    background_tasks.add_task(run_grok_batch, batch_id, task_uuids, request)
    return GrokBatchResponse(batch_id=batch_id, count=request.count, tasks=task_responses)


@router.get("/tasks/{task_uuid}", response_model=GrokTaskResponse)
async def get_grok_task(task_uuid: str):
    with get_db() as db:
        task = crud.get_registration_task_by_uuid(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        return task_to_response(task)


@router.post("/tasks/{task_uuid}/cancel")
async def cancel_grok_task(task_uuid: str):
    task_manager.cancel_task(task_uuid)
    with get_db() as db:
        task = crud.get_registration_task_by_uuid(db, task_uuid)
        if task and task.status in {"pending", "running"}:
            crud.update_registration_task(db, task_uuid, status="cancelled", completed_at=datetime.utcnow())
    return {"success": True, "task_uuid": task_uuid}


@router.get("/batches/{batch_id}")
async def get_grok_batch(batch_id: str):
    status = task_manager.get_batch_status(batch_id)
    if not status:
        raise HTTPException(status_code=404, detail="批量任务不存在")

    task_uuids = grok_batches.get(batch_id, {}).get("task_uuids", [])
    tasks = []
    with get_db() as db:
        for task_uuid in task_uuids:
            task = crud.get_registration_task_by_uuid(db, task_uuid)
            if task:
                tasks.append(task_to_response(task).model_dump())

    return {
        "batch_id": batch_id,
        **status,
        "logs": task_manager.get_batch_logs(batch_id),
        "tasks": tasks,
    }


@router.post("/batches/{batch_id}/cancel")
async def cancel_grok_batch(batch_id: str):
    status = task_manager.get_batch_status(batch_id)
    if not status:
        raise HTTPException(status_code=404, detail="批量任务不存在")

    task_manager.cancel_batch(batch_id)

    for task_uuid in grok_batches.get(batch_id, {}).get("task_uuids", []):
        task_manager.cancel_task(task_uuid)
        with get_db() as db:
            task = crud.get_registration_task_by_uuid(db, task_uuid)
            if task and task.status in {"pending", "running"}:
                crud.update_registration_task(db, task_uuid, status="cancelled", completed_at=datetime.utcnow())

    return {"success": True, "batch_id": batch_id}


@router.get("/recent-accounts")
async def get_recent_grok_accounts(limit: int = 20):
    with get_db() as db:
        accounts = (
            db.query(Account)
            .filter(Account.email_service == "grok")
            .order_by(Account.created_at.desc())
            .limit(limit)
            .all()
        )

    return {
        "accounts": [account.to_dict() for account in accounts],
    }
