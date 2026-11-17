from fastapi import APIRouter, Body
from app.services import close_service as svc

router = APIRouter(prefix="/close", tags=["close"])


@router.get("/preview")
def preview(business_day: str | None = None):
    return svc.preview(business_day)


@router.post("/seal")
def seal(body: dict = Body(...)):
    return svc.seal(body)


@router.get("/batches")
def batches():
    return svc.list_batches()


@router.get("/variances")
def variances():
    return svc.list_variances()


@router.get("/integrity")
def integrity():
    """归属 / 合计自检：落库日必须等于发生日，批次合计必须等于落库日合计。"""
    return svc.integrity()
