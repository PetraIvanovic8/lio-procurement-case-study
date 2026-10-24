import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RequestStatus = Literal["open", "in_progress", "closed"]
ApprovalStatus = Literal["pending", "approved", "rejected"]


# ---------------------------------------------------------------------------
# Inputs (camelCase — matches what the frontend forms already send)
# ---------------------------------------------------------------------------

class OrderLineIn(BaseModel):
    position_description: str = Field(alias="positionDescription")
    unit_price: float = Field(alias="unitPrice", ge=0, allow_inf_nan=False)
    amount: float = Field(gt=0, allow_inf_nan=False)
    unit: str
    total_price: float = Field(alias="totalPrice", ge=0, allow_inf_nan=False)
    catalog_article_id: uuid.UUID | None = Field(default=None, alias="catalogArticleId")


class AdjustmentIn(BaseModel):
    """Validated non-item quote adjustment supplied with a request."""

    model_config = ConfigDict(populate_by_name=True)

    description: str = Field(min_length=1)
    amount: float = Field(allow_inf_nan=False)
    line_type: Literal["shipping", "discount", "fee", "tax"] = Field(
        alias="lineType"
    )


class RequestCreate(BaseModel):
    requestor_name: str = Field(alias="requestorName")
    title: str = Field(alias="titleShortDescription")
    vendor_name: str = Field(alias="vendorName")
    vat_id: str | None = Field(default=None, alias="vatId")
    commodity_group_id: int = Field(alias="commodityGroupId")
    total_cost: float = Field(alias="totalCost", ge=0, allow_inf_nan=False)
    currency: str | None = None
    department: str | None = None
    order_lines: list[OrderLineIn] = Field(alias="orderLines", min_length=1)
    adjustments: list[AdjustmentIn] = Field(default_factory=list)


class RequestUpdate(BaseModel):
    requestor_name: str = Field(alias="requestorName")
    title: str
    vendor_name: str = Field(alias="vendorName")
    vat_id: str | None = Field(default=None, alias="vatId")
    commodity_group_id: int = Field(alias="commodityGroupId")
    total_cost: float = Field(alias="totalCost", ge=0, allow_inf_nan=False)
    currency: str | None = None
    department: str | None = None
    order_lines: list[OrderLineIn] = Field(alias="orderLines")
    adjustments: list[AdjustmentIn] = Field(default_factory=list)


class StatusUpdate(BaseModel):
    status: RequestStatus


class ApprovalDecision(BaseModel):
    decision: Literal["approved", "rejected"]


# ---------------------------------------------------------------------------
# Outputs (snake_case — matches the DB row shapes the frontend renders)
# ---------------------------------------------------------------------------

class CommodityGroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category: str
    name: str


class OrderLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    request_id: uuid.UUID
    position_description: str
    unit_price: float
    amount: float
    unit: str
    total_price: float
    line_order: int
    created_at: datetime
    catalog_article_id: uuid.UUID | None


class RequestActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    request_id: uuid.UUID
    actor_id: uuid.UUID | None
    action: str
    summary: str
    detail: dict | None
    created_at: datetime


class RequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    user_id: uuid.UUID
    organization_id: uuid.UUID | None
    created_by: uuid.UUID | None
    requestor_name: str
    title: str
    vendor_name: str
    vat_id: str | None
    commodity_group_id: int | None
    department: str | None
    status: RequestStatus
    approval_status: ApprovalStatus
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    total_cost: float | None
    currency: str | None
    created_at: datetime
    updated_at: datetime
    order_lines: list[OrderLineOut]
    adjustments: list[dict]
    # Serialized as ``commodity_groups`` (the join key the frontend renders) but
    # read from the ORM's ``commodity_group`` relationship.
    commodity_groups: CommodityGroupOut | None = Field(
        default=None, validation_alias="commodity_group"
    )
