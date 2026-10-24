import math
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.core.deps import get_current_membership, require_buyer_or_admin
from app.db.session import get_db
from app.models import (
    CommodityGroup,
    Article,
    OrderLine,
    Organization,
    OrganizationMember,
    ProcurementRequest,
    RequestActivity,
    RequestDocument,
)
from app.schemas.requests import (
    ApprovalDecision,
    RequestActivityOut,
    RequestCreate,
    RequestOut,
    RequestUpdate,
    StatusUpdate,
)
from app.schemas.organizations import OrganizationSettings
from app.services.commodity_overrides import matching_overrides

router = APIRouter(prefix="/requests", tags=["requests"])

MAX_PDF_BYTES = 10 * 1024 * 1024

_UNIT_ALIASES = {
    "ea": "piece",
    "each": "piece",
    "pc": "piece",
    "pcs": "piece",
    "piece": "piece",
    "pieces": "piece",
    "license": "license",
    "licenses": "license",
}

# Human-readable labels for the fields we track in the activity log.
_FIELD_LABELS = {
    "requestor_name": "requestor",
    "title": "title",
    "vendor_name": "vendor",
    "vat_id": "tax ID",
    "commodity_group_id": "commodity group",
    "total_cost": "total cost",
    "department": "department",
    "order_lines": "order lines",
}


def _load_request_options():
    return (
        selectinload(ProcurementRequest.order_lines),
        selectinload(ProcurementRequest.commodity_group),
    )


def _get_org_request(
    db: Session, request_id: uuid.UUID, organization_id: uuid.UUID
) -> ProcurementRequest:
    request = db.scalar(
        select(ProcurementRequest)
        .options(*_load_request_options())
        # Refresh already-loaded relationships (e.g. after an update commit).
        .execution_options(populate_existing=True)
        .where(
            ProcurementRequest.id == request_id,
            ProcurementRequest.organization_id == organization_id,
        )
    )
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Request not found or you do not have permission to access it",
        )
    return request


def _validate_commodity_group(db: Session, commodity_group_id: int) -> None:
    if db.get(CommodityGroup, commodity_group_id) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown commodity group id: {commodity_group_id}",
        )


def _canonical_unit(value: str) -> str:
    """Normalize common quote/catalog unit spellings before comparison."""
    normalized = value.casefold().strip()
    return _UNIT_ALIASES.get(normalized, normalized)


def _validate_catalog_articles(
    db: Session, organization_id: uuid.UUID, payload: RequestCreate | RequestUpdate
) -> None:
    """Validate catalog ownership and negotiated values supplied by the client."""
    article_ids = {
        line.catalog_article_id for line in payload.order_lines if line.catalog_article_id
    }
    if not article_ids:
        return
    articles = db.scalars(
        select(Article).where(
            Article.id.in_(article_ids), Article.organization_id == organization_id
        )
    ).all()
    articles_by_id = {article.id: article for article in articles}
    invalid_ids = article_ids - articles_by_id.keys()
    if invalid_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="One or more selected catalog articles are not in your organization catalog.",
        )
    for line in payload.order_lines:
        if not line.catalog_article_id:
            continue
        article = articles_by_id[line.catalog_article_id]
        if not math.isclose(float(line.unit_price), float(article.unit_price), abs_tol=0.01):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Line '{line.position_description}' must use the negotiated catalog "
                    f"price of {article.unit_price}."
                ),
            )
        if _canonical_unit(line.unit) != _canonical_unit(article.unit):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Line '{line.position_description}' must use catalog unit "
                    f"'{article.unit}'."
                ),
            )
        if payload.currency and payload.currency.casefold() != article.currency.casefold():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Catalog article currency '{article.currency}' does not match "
                    f"the request currency '{payload.currency}'."
                ),
            )


def _authoritative_total(payload: RequestCreate | RequestUpdate) -> float:
    """Calculate the request total from validated lines and quote adjustments."""
    line_total = 0.0
    for line in payload.order_lines:
        expected = line.unit_price * line.amount
        if not math.isclose(line.total_price, expected, abs_tol=0.02):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Line '{line.position_description}' total must equal unit price "
                    "multiplied by quantity."
                ),
            )
        line_total += expected
    adjustment_total = sum(adjustment.amount for adjustment in payload.adjustments)
    total = round(line_total + adjustment_total, 2)
    if not math.isfinite(total) or total < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The calculated request total must be a non-negative finite number.",
        )
    return total


def _approval_for_total(total_cost: float) -> tuple[str, datetime | None]:
    """Return the approval state for an authoritative request total.

    The threshold is intentionally strict: a request exactly at the configured
    amount still requires buyer approval because the requirement says below.
    """
    if total_cost < settings.auto_approval_threshold:
        return "approved", datetime.now(timezone.utc)
    return "pending", None


def _enforce_commodity_overrides(
    db: Session,
    organization_id: uuid.UUID,
    payload: RequestCreate | RequestUpdate,
    total_cost: float,
) -> None:
    """Reject a request that violates one of the organization's fixed rules."""
    org = db.get(Organization, organization_id)
    settings = OrganizationSettings.model_validate((org.settings or {}) if org else {})
    text = " ".join(
        [payload.title, *(line.position_description for line in payload.order_lines)]
    )
    matches = matching_overrides(text, total_cost, settings.commodity_group_overrides)
    matched_group_ids = {match.commodity_group_id for match in matches}
    if len(matches) > 1 and payload.commodity_group_id not in matched_group_ids:
        group_names = db.scalars(
            select(CommodityGroup.name).where(CommodityGroup.id.in_(matched_group_ids))
        ).all()
        available_groups = ", ".join(sorted(group_names)) or ", ".join(
            str(group_id) for group_id in sorted(matched_group_ids)
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "This request matches multiple commodity-group rules. "
                f"Select one of: {available_groups}."
            ),
        )
    if len(matches) == 1 and payload.commodity_group_id != matches[0].commodity_group_id:
        group_name = db.scalar(
            select(CommodityGroup.name).where(
                CommodityGroup.id == matches[0].commodity_group_id
            )
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "The selected commodity group violates an organization rule. "
                f"Use {group_name or matches[0].commodity_group_id}."
            ),
        )


def _enforce_required_fields(
    db: Session, organization_id: uuid.UUID, payload: RequestCreate | RequestUpdate
) -> None:
    """Enforce the org's configurable required fields (see OrganizationSettings)."""
    org = db.get(Organization, organization_id)
    required = (org.settings or {}).get("required_fields", []) if org else []
    missing = [field for field in required if not getattr(payload, field, None)]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Missing fields required by your organization: {', '.join(missing)}",
        )


def _log_activity(
    db: Session,
    request_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    action: str,
    summary: str,
    detail: dict | None = None,
) -> None:
    db.add(
        RequestActivity(
            request_id=request_id,
            actor_id=actor_id,
            action=action,
            summary=summary,
            detail=detail,
        )
    )


def _order_lines_signature(lines) -> list[tuple]:
    return [
        (
            line.position_description,
            float(line.unit_price),
            float(line.amount),
            line.unit,
            float(line.total_price),
        )
        for line in lines
    ]


def _changed_fields(request: ProcurementRequest, payload: RequestUpdate) -> list[str]:
    """Return the field keys whose value differs between the request and payload."""
    changed: list[str] = []
    if request.requestor_name != payload.requestor_name:
        changed.append("requestor_name")
    if request.title != payload.title:
        changed.append("title")
    if request.vendor_name != payload.vendor_name:
        changed.append("vendor_name")
    if (request.vat_id or "") != (payload.vat_id or ""):
        changed.append("vat_id")
    if request.commodity_group_id != payload.commodity_group_id:
        changed.append("commodity_group_id")
    if float(request.total_cost or 0) != float(payload.total_cost or 0):
        changed.append("total_cost")
    if (request.department or "") != (payload.department or ""):
        changed.append("department")
    old_lines = _order_lines_signature(
        sorted(request.order_lines, key=lambda line: line.line_order)
    )
    new_lines = _order_lines_signature(payload.order_lines)
    if old_lines != new_lines:
        changed.append("order_lines")
    return changed


@router.get("", response_model=list[RequestOut])
def list_requests(
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> list[RequestOut]:
    requests = db.scalars(
        select(ProcurementRequest)
        .options(*_load_request_options())
        .where(ProcurementRequest.organization_id == membership.organization_id)
        .order_by(ProcurementRequest.created_at.desc())
    )
    return [RequestOut.model_validate(r) for r in requests]


@router.get("/{request_id}", response_model=RequestOut)
def get_request(
    request_id: uuid.UUID,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> RequestOut:
    return RequestOut.model_validate(
        _get_org_request(db, request_id, membership.organization_id)
    )


@router.post("", response_model=RequestOut, status_code=status.HTTP_201_CREATED)
def create_request(
    payload: RequestCreate,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> RequestOut:
    _validate_commodity_group(db, payload.commodity_group_id)
    _validate_catalog_articles(db, membership.organization_id, payload)
    total_cost = _authoritative_total(payload)
    _enforce_commodity_overrides(db, membership.organization_id, payload, total_cost)
    _enforce_required_fields(db, membership.organization_id, payload)
    approval_status, approved_at = _approval_for_total(total_cost)

    # Vendor fields are already cleaned up during extraction, so we can persist
    # them directly here.
    request = ProcurementRequest(
        user_id=membership.user_id,
        requestor_name=payload.requestor_name,
        title=payload.title,
        vendor_name=payload.vendor_name,
        vat_id=payload.vat_id,
        commodity_group_id=payload.commodity_group_id,
        total_cost=total_cost,
        currency=payload.currency,
        adjustments=[adjustment.model_dump(by_alias=True) for adjustment in payload.adjustments],
        department=payload.department,
        organization_id=membership.organization_id,
        created_by=membership.user_id,
        status="open",
        approval_status=approval_status,
        approved_at=approved_at,
    )
    request.order_lines = [
        OrderLine(
            position_description=line.position_description,
            unit_price=line.unit_price,
            amount=line.amount,
            unit=line.unit,
            total_price=line.total_price,
            line_order=index + 1,
            catalog_article_id=line.catalog_article_id,
        )
        for index, line in enumerate(payload.order_lines)
    ]
    db.add(request)
    db.flush()
    _log_activity(db, request.id, membership.user_id, "created", "Request created")
    if approval_status == "approved":
        _log_activity(
            db,
            request.id,
            None,
            "auto_approved",
            "Request auto-approved because it is below the approval threshold",
        )
    db.commit()

    request = _get_org_request(db, request.id, membership.organization_id)
    return RequestOut.model_validate(request)


@router.put("/{request_id}", response_model=RequestOut)
def update_request(
    request_id: uuid.UUID,
    payload: RequestUpdate,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> RequestOut:
    request = _get_org_request(db, request_id, membership.organization_id)
    _validate_commodity_group(db, payload.commodity_group_id)
    _validate_catalog_articles(db, membership.organization_id, payload)
    total_cost = _authoritative_total(payload)
    _enforce_commodity_overrides(db, membership.organization_id, payload, total_cost)
    _enforce_required_fields(db, membership.organization_id, payload)

    changed = _changed_fields(request, payload)
    total_changed = not math.isclose(
        float(request.total_cost or 0), total_cost, abs_tol=0.01
    )

    request.requestor_name = payload.requestor_name
    request.title = payload.title
    request.vendor_name = payload.vendor_name
    request.vat_id = payload.vat_id
    request.commodity_group_id = payload.commodity_group_id
    request.total_cost = total_cost
    request.currency = payload.currency
    if "adjustments" in payload.model_fields_set:
        request.adjustments = [
            adjustment.model_dump(by_alias=True) for adjustment in payload.adjustments
        ]
    request.department = payload.department
    if total_changed and request.approval_status in {"pending", "approved"}:
        request.approval_status, request.approved_at = _approval_for_total(total_cost)
        request.approved_by = None

    # Replace order lines (same semantics as the original delete-and-insert).
    request.order_lines = [
        OrderLine(
            position_description=line.position_description,
            unit_price=line.unit_price,
            amount=line.amount,
            unit=line.unit,
            total_price=line.total_price,
            line_order=index + 1,
            catalog_article_id=line.catalog_article_id,
        )
        for index, line in enumerate(payload.order_lines)
    ]

    if changed:
        labels = [_FIELD_LABELS.get(f, f) for f in changed]
        _log_activity(
            db,
            request.id,
            membership.user_id,
            "updated",
            f"Updated {', '.join(labels)}",
            {"changed_fields": changed},
        )
    db.commit()

    request = _get_org_request(db, request_id, membership.organization_id)
    return RequestOut.model_validate(request)


@router.patch("/{request_id}/status", response_model=RequestOut)
def update_status(
    request_id: uuid.UUID,
    payload: StatusUpdate,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> RequestOut:
    request = _get_org_request(db, request_id, membership.organization_id)

    if request.status != payload.status:
        _log_activity(
            db,
            request.id,
            membership.user_id,
            "status_changed",
            f"Status changed from {request.status} to {payload.status}",
            {"from": request.status, "to": payload.status},
        )
        request.status = payload.status
        db.commit()

    request = _get_org_request(db, request_id, membership.organization_id)
    return RequestOut.model_validate(request)


@router.patch("/{request_id}/approval", response_model=RequestOut)
def decide_approval(
    request_id: uuid.UUID,
    payload: ApprovalDecision,
    membership: OrganizationMember = Depends(require_buyer_or_admin),
    db: Session = Depends(get_db),
) -> RequestOut:
    """Approve or reject a pending request (buyers and admins only)."""
    request = _get_org_request(db, request_id, membership.organization_id)

    if request.approval_status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Request has already been {request.approval_status}.",
        )

    request.approval_status = payload.decision
    request.approved_by = membership.user_id
    request.approved_at = datetime.now(timezone.utc)
    _log_activity(
        db,
        request.id,
        membership.user_id,
        payload.decision,
        f"Request {payload.decision}",
    )
    db.commit()

    request = _get_org_request(db, request_id, membership.organization_id)
    return RequestOut.model_validate(request)


@router.get("/{request_id}/activity", response_model=list[RequestActivityOut])
def get_activity(
    request_id: uuid.UUID,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> list[RequestActivityOut]:
    _get_org_request(db, request_id, membership.organization_id)  # scope check
    activity = db.scalars(
        select(RequestActivity)
        .where(RequestActivity.request_id == request_id)
        .order_by(RequestActivity.created_at.desc())
    )
    return [RequestActivityOut.model_validate(a) for a in activity]


@router.put("/{request_id}/document", status_code=status.HTTP_204_NO_CONTENT)
async def upload_document(
    request_id: uuid.UUID,
    file: UploadFile,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> None:
    """Attach (or replace) the original PDF for a request."""
    request = _get_org_request(db, request_id, membership.organization_id)

    if file.content_type not in ("application/pdf", "application/x-pdf"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Please upload a PDF file"
        )
    content = await file.read()
    if len(content) > MAX_PDF_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="PDF exceeds the 10MB limit",
        )

    document = db.scalar(
        select(RequestDocument).where(RequestDocument.request_id == request.id)
    )
    if document is None:
        document = RequestDocument(request_id=request.id)
        db.add(document)
    document.filename = file.filename or "document.pdf"
    document.content_type = "application/pdf"
    document.content = content

    _log_activity(
        db,
        request.id,
        membership.user_id,
        "document_attached",
        f"Attached document {document.filename}",
    )
    db.commit()


@router.get("/{request_id}/document")
def get_document(
    request_id: uuid.UUID,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> Response:
    _get_org_request(db, request_id, membership.organization_id)  # scope check
    document = db.scalar(
        select(RequestDocument).where(RequestDocument.request_id == request_id)
    )
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No document attached"
        )
    return Response(
        content=document.content,
        media_type=document.content_type,
        headers={"Content-Disposition": f'inline; filename="{document.filename}"'},
    )


@router.delete("/{request_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_request(
    request_id: uuid.UUID,
    membership: OrganizationMember = Depends(get_current_membership),
    db: Session = Depends(get_db),
) -> None:
    request = _get_org_request(db, request_id, membership.organization_id)
    db.delete(request)  # order lines, activity, document cascade
    db.commit()
