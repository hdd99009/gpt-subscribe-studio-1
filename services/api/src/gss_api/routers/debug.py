"""Runtime switch and local HAR downloads for complete HTTP diagnostics."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool

from gss_core.http_debug import debug_store

router = APIRouter(prefix="/api/settings/debug", tags=["settings"])


class DebugSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool


@router.get("")
def get_debug_settings():
    try:
        return debug_store().status()
    except (OSError, ValueError, KeyError):
        raise HTTPException(500, "调试记录读取失败，请检查目录权限")


@router.put("")
def set_debug_settings(values: DebugSettings):
    try:
        return debug_store().set_enabled(values.enabled)
    except (OSError, ValueError, KeyError):
        raise HTTPException(500, "调试模式设置失败，请检查磁盘空间和目录权限")


@router.get("/captures/{capture_id}/har")
def download_har(capture_id: str):
    try:
        return JSONResponse(
            debug_store().har(capture_id),
            headers={"Content-Disposition": f'attachment; filename="http-debug-{capture_id}.har"',
                     "Cache-Control": "no-store"},
        )
    except ValueError:
        raise HTTPException(400, "调试记录编号格式不正确")
    except FileNotFoundError:
        raise HTTPException(404, "调试记录不存在")
    except OSError:
        raise HTTPException(500, "调试记录读取失败，请检查目录权限")
