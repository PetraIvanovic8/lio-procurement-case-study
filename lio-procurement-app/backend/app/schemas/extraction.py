from pydantic import BaseModel, ConfigDict, Field, field_validator


class PdfParseResponse(BaseModel):
    text: str


class ExtractionRequest(BaseModel):
    text: str


# camelCase on the wire — matches the AI's JSON output format and what the
# frontend's extraction flow already consumes.
class OrderLineData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    position_description: str = Field(default="", alias="positionDescription")
    unit_price: float = Field(default=0, alias="unitPrice")
    amount: float = 0
    unit: str = ""
    total_price: float = Field(default=0, alias="totalPrice")
    line_type: str = Field(default="item", alias="lineType")

    @field_validator("position_description", "unit_price", "amount", "unit", "total_price", mode="before")
    @classmethod
    def none_to_default(cls, value, info):
        """Keep one malformed nullable field from invalidating the whole quote."""
        if value is not None:
            return value
        return {"position_description": "", "unit_price": 0, "amount": 0, "unit": "", "total_price": 0}[info.field_name]


class AdjustmentData(BaseModel):
    """A quote amount that is not a purchasable item line."""

    model_config = ConfigDict(populate_by_name=True)

    description: str = ""
    amount: float = 0
    line_type: str = Field(default="fee", alias="lineType")

    @field_validator("description", "amount", mode="before")
    @classmethod
    def none_to_default(cls, value, info):
        """Normalize absent adjustment values to safe defaults."""
        if value is not None:
            return value
        return "" if info.field_name == "description" else 0


class ExtractedVendorData(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    title: str | None = ""
    vendor_name: str | None = Field(default="", alias="vendorName")
    vat_id: str | None = Field(default="", alias="vatId")
    department: str | None = ""
    order_lines: list[OrderLineData] = Field(default_factory=list, alias="orderLines")
    adjustments: list[AdjustmentData] = Field(default_factory=list)
    alternatives: list[AdjustmentData] = Field(default_factory=list)
    total_cost: float | None = Field(default=0, alias="totalCost")
    currency: str | None = None
    commodity_group_id: int | None = Field(default=None, alias="commodityGroupId")
    commodity_group_name: str | None = Field(default=None, alias="commodityGroupName")


class ExtractionResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    success: bool
    data: ExtractedVendorData | None = None
    error: str | None = None
    missing_fields: list[str] | None = Field(default=None, alias="missingFields")
    override_applied: bool = Field(default=False, alias="overrideApplied")
    override_reason: str | None = Field(default=None, alias="overrideReason")
    override_conflict: bool = Field(default=False, alias="overrideConflict")
    override_group_ids: list[int] = Field(default_factory=list, alias="overrideGroupIds")
    warnings: list[str] | None = None
