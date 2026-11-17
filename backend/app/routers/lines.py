from fastapi import APIRouter, Body
from app import attribution
from app.services import line_service as svc

router = APIRouter(prefix="/lines", tags=["lines"])


@router.get("")
def list_lines(business_day: str | None = None):
    """列表过滤只认落库 business_day；每行另带 occurred_day 与校正标记。"""
    return svc.list_lines(business_day)


@router.get("/corrections")
def list_corrections(business_day: str | None = None):
    """归属校正留痕；按落库 business_day 过滤，与封账合计同源。"""
    return svc.list_corrections(business_day)


@router.get("/attribution")
def attribution_policy():
    return attribution.describe()


@router.post("")
def create_line(body: dict = Body(...)):
    return svc.create_line(body)


@router.post("/import")
def import_lines(body: dict = Body(...)):
    return svc.import_lines(body)


@router.patch("/{line_id}")
def patch_line(line_id: int, body: dict = Body(...)):
    return svc.patch_line(line_id, body)


@router.delete("/{line_id}")
def delete_line(line_id: int):
    return svc.remove_line(line_id)
