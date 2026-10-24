import json
import logging
import math
import re
import time
import uuid

from openai import APIConnectionError, APITimeoutError, OpenAI, RateLimitError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import CommodityGroup
from app.schemas.extraction import AdjustmentData, ExtractedVendorData, ExtractionResponse
from app.schemas.organizations import OrganizationSettings
from app.services.commodity_overrides import OverrideMatch, apply_commodity_override

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a data extraction and classification specialist. Always respond with "
    "valid JSON only, no markdown or code blocks. Extract all information accurately "
    "from US business documents and classify into the appropriate commodity "
    "group based on the full context of the offer. The buyer/customer is not the vendor."
)

_LINE_TYPES = {"item", "alternative", "shipping", "discount", "fee"}


def _build_prompt(pdf_text: str, commodity_groups_list: str) -> str:
    return f"""You are a data extraction and classification specialist. Extract procurement/vendor offer information from the following US document text AND classify it into the appropriate commodity group.

Document Text:
{pdf_text}

Please extract the following information:
1. Title/Short Description (A brief summary of what this procurement is for - e.g., "Office Equipment Purchase", "Software Licenses", etc.)
2. Vendor Name (Vendor/Supplier/Seller/Company name; never the buyer, bill-to, or ship-to party)
3. Tax ID (Employer Identification Number/EIN/Federal Tax ID/TIN/VAT ID)
4. Department/Customer (Department/Customer/Bill To/Customer name)
5. Quote Lines (Classify every commercial row with a lineType):
   - item: a purchasable item or service that belongs in orderLines
   - alternative: an optional replacement, never included in orderLines
   - shipping: delivery or freight adjustment
   - discount: a negative price adjustment
   - fee: tax, recycling, installation, or other non-item charge
   Only lineType=item belongs in orderLines. Put all other rows in adjustments.
   For each item line:
   - Position Description (Description/Product/Item/Article)
   - Unit Price (Unit Price/Price/Rate/Price per unit)
   - Amount (Qty/Quantity/Count)
   - Unit (Unit/UoM - e.g., "licenses", "pieces", "ea.", "items")
   - Total Price (Total/Ext. Price/Amount)
6. Total Cost (Grand Total/Total Cost/Total Offer Cost)
7. Currency (ISO code such as USD or EUR, if shown)
8. Commodity Group Classification - Select the SINGLE most appropriate commodity group from the list below

Available Commodity Groups (ID | Category | Name):
{commodity_groups_list}

IMPORTANT INSTRUCTIONS:
- For the title, create a SHORT (2-5 words) description based on the main items being purchased
- Extract all commercial rows, but classify alternatives, shipping, discounts, and fees using lineType
- Identify the seller/vendor from the seller, vendor, or supplier block; the buyer/customer is the bill-to or ship-to party and must never be returned as vendorName
- For unit prices and totals, extract only the numeric value (remove currency symbols)
- For the unit field, write out abbreviations in plain English (e.g., "ea." -> "pieces", "lic." -> "licenses")
- Be careful with US number formatting (e.g., "1,438.00" = 1438.00)
- If a field is absent, return null rather than inventing a value
- For classification, consider the PRIMARY nature and purpose of the items:
  * Choose the closest entry from the supplied catalog by its category and name
  * Use the full procurement context, not a single keyword
  * If genuinely uncertain, set commodityGroupId to null

Return ONLY valid JSON in this exact format (no markdown, no code blocks):
{{
  "title": "Short description of procurement",
  "vendorName": "Company Name",
  "vatId": "12-3456789",
  "department": "Department Name",
  "orderLines": [
    {{
      "positionDescription": "Item description",
      "unitPrice": 150.00,
      "amount": 10,
      "unit": "licenses",
      "totalPrice": 1500.00,
      "lineType": "item"
    }}
  ],
  "adjustments": [
    {{"description": "Shipping", "amount": 25.00, "lineType": "shipping"}}
  ],
  "alternatives": [],
  "totalCost": 1500.00,
  "currency": "USD",
  "commodityGroupId": null,
  "commodityGroupName": null
}}"""


def _strip_markdown_fences(content: str) -> str:
    cleaned = content.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned.removeprefix("```json").strip()
    elif cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```").strip()
    if cleaned.endswith("```"):
        cleaned = cleaned.removesuffix("```").strip()
    return cleaned


def _normalise_party_name(value: str | None) -> str:
    """Normalize a party name for conservative buyer/vendor comparisons."""
    return re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()


def _is_known_buyer(
    vendor_name: str | None,
    organization_name: str | None,
    buyer_aliases: list[str] | None = None,
) -> bool:
    """Return whether an extracted vendor looks like the purchasing organization."""
    vendor = _normalise_party_name(vendor_name)
    if not vendor:
        return False
    buyer_names = {_normalise_party_name(alias) for alias in buyer_aliases or []}
    if organization_name:
        buyer_names.add(_normalise_party_name(organization_name))
    return any(buyer and (buyer in vendor or vendor in buyer) for buyer in buyer_names)


def _normalise_line_types(extracted: ExtractedVendorData) -> list[str]:
    """Separate purchasable items, alternatives, and monetary adjustments."""
    warnings: list[str] = []
    items = []
    adjustments = list(extracted.adjustments)
    alternatives = list(extracted.alternatives)
    for line in extracted.order_lines:
        line_type = (line.line_type or "item").casefold().strip()
        if line_type not in _LINE_TYPES:
            mapped_type = {
                "tax": "fee",
                "installation": "fee",
                "optional": "alternative",
                "alternate": "alternative",
                "replacement": "alternative",
            }.get(line_type)
            if mapped_type is None:
                warnings.append(
                    f"Treated unknown line type '{line_type}' as an item."
                )
                line_type = "item"
            else:
                line_type = mapped_type
        if line_type == "item" and re.search(
            r"(?:^|[\s(])(?:alternative\s+(?:to|option)|alt\.)(?:[\s).,:;-]|$)",
            line.position_description,
            re.I,
        ):
            line_type = "alternative"
        line.line_type = line_type
        if line_type == "item":
            if line.amount == 0 and line.total_price > 0:
                # A lump-sum service often has no explicit quantity. Treat it
                # as one unit so a valid commercial row is not discarded.
                line.amount = 1
                if line.unit_price == 0:
                    line.unit_price = line.total_price
                warnings.append(
                    f"Defaulted missing quantity to 1 for {line.position_description}."
                )
            if (
                line.amount > 0
                and line.unit_price > 0
                and line.total_price > 0
                and line.total_price + 0.02 < line.unit_price * line.amount
            ):
                # Normalize an inline discount to the net unit price so the
                # request API can enforce exact line arithmetic safely.
                line.unit_price = line.total_price / line.amount
                warnings.append(
                    f"Applied inline discount to unit price for {line.position_description}."
                )
            if (
                not line.position_description.strip()
                or not all(
                    math.isfinite(value)
                    for value in (line.unit_price, line.amount, line.total_price)
                )
                or line.unit_price < 0
                or line.amount <= 0
                or line.total_price < 0
                or (line.unit_price == 0 and line.total_price == 0)
            ):
                warnings.append(
                    f"Excluded invalid or unpriced row {line.position_description or 'without a description'}."
                )
                continue
            items.append(line)
            continue
        amount = line.total_price
        if line_type == "discount":
            amount = -abs(amount)
        if line_type == "alternative":
            alternatives.append(
                AdjustmentData(
                    description=line.position_description,
                    amount=amount,
                    line_type=line_type,
                )
            )
            warnings.append(
                f"Excluded {line.position_description or 'an alternative'} from order lines."
            )
        else:
            adjustments.append(
                AdjustmentData(
                    description=line.position_description,
                    amount=amount,
                    line_type=line_type,
                )
            )
    extracted.order_lines = items
    normalized_adjustments = []
    supported_adjustment_types = {"shipping", "discount", "fee", "tax"}
    for adjustment in adjustments:
        line_type = (adjustment.line_type or "fee").casefold().strip()
        if line_type == "alternative":
            alternatives.append(adjustment)
            continue
        if line_type not in supported_adjustment_types:
            warnings.append(f"Mapped unknown adjustment type '{line_type}' to fee.")
            line_type = "fee"
        amount = -abs(adjustment.amount) if line_type == "discount" else adjustment.amount
        description = adjustment.description.strip() or line_type.title()
        normalized_adjustments.append(
            adjustment.model_copy(
                update={
                    "description": description,
                    "amount": amount,
                    "line_type": line_type,
                }
            )
        )
    extracted.adjustments = normalized_adjustments
    extracted.alternatives = alternatives
    return warnings


def _reconcile_totals(extracted: ExtractedVendorData) -> list[str]:
    """Compare item and adjustment totals without changing extracted values."""
    if extracted.total_cost is None:
        return ["Quote total is missing; item and adjustment totals could not be reconciled."]
    item_total = sum(line.total_price for line in extracted.order_lines)
    non_discount_adjustments = sum(
        adjustment.amount
        for adjustment in extracted.adjustments
        if adjustment.line_type not in {"alternative", "discount"}
    )
    discount_adjustments = sum(
        adjustment.amount
        for adjustment in extracted.adjustments
        if adjustment.line_type == "discount"
    )
    calculated_total = item_total + non_discount_adjustments
    calculated_total += discount_adjustments
    tolerance = max(0.05, abs(extracted.total_cost) * 0.001)
    if abs(calculated_total - extracted.total_cost) > tolerance:
        return [
            (
                "Quote totals do not reconcile within a 0.1% rounding tolerance "
                f"(calculated {calculated_total:.2f} vs extracted {extracted.total_cost:.2f}); "
                "review items, discounts, taxes, and fees."
            )
        ]
    return []


def _recover_vendor_from_document(
    client: OpenAI,
    pdf_text: str,
    organization_name: str | None,
    buyer_aliases: list[str],
) -> tuple[str | None, str | None]:
    """Use a targeted second pass when the first pass selected the buyer."""
    response = client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {
                "role": "system",
                "content": (
                    "Extract only the seller/vendor from this quote. The buyer, "
                    "bill-to, and ship-to party is not the vendor. Return JSON only."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Known buyer names: {organization_name}, {buyer_aliases}.\n"
                    "Return {\"vendorName\": string|null, \"buyerName\": string|null}.\n"
                    f"Document text:\n{pdf_text}"
                ),
            },
        ],
        max_completion_tokens=300,
        response_format={"type": "json_object"},
        temperature=0,
    )
    content = response.choices[0].message.content
    if not content:
        return None, None
    result = json.loads(_strip_markdown_fences(content))
    vendor = result.get("vendorName")
    department = result.get("buyerName")
    if _is_known_buyer(vendor, organization_name, buyer_aliases):
        return None, None
    return vendor, department


def extract_vendor_data(
    pdf_text: str,
    db: Session,
    organization_id: uuid.UUID | None = None,
    organization_settings: dict | None = None,
    organization_name: str | None = None,
) -> ExtractionResponse:
    """Extract vendor data and classify commodity group in one AI call.

    Args:
        pdf_text: Text extracted from the uploaded vendor PDF.
        db: Database session used to load valid commodity groups.
        organization_id: Authenticated organization used for tenant scoping.
        organization_settings: Validated organization configuration.

    Returns:
        A validated extraction response with missing-field warnings.

    Port of ``src/actions/extraction.ts`` — same prompt, model and validation.
    """
    if not pdf_text or not pdf_text.strip():
        return ExtractionResponse(success=False, error="No text provided for extraction")

    if not settings.openai_api_key:
        return ExtractionResponse(
            success=False,
            error="OPENAI_API_KEY is not configured on the server",
        )

    commodity_groups = list(
        db.scalars(select(CommodityGroup).order_by(CommodityGroup.id))
    )
    if not commodity_groups:
        return ExtractionResponse(
            success=False,
            error="Failed to load commodity groups for classification",
        )

    # Give the model the full commodity catalog so it has complete context to
    # pick the most accurate match in a single pass.
    commodity_groups_list = "\n".join(
        f"{g.id} | {g.category} | {g.name}" for g in commodity_groups
    )

    try:
        client = OpenAI(api_key=settings.openai_api_key, timeout=30.0)
        for attempt in range(2):
            try:
                response = client.chat.completions.create(
                    model=settings.openai_model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": _build_prompt(pdf_text, commodity_groups_list)},
                    ],
                    max_completion_tokens=4000,
                    response_format={"type": "json_object"},
                    temperature=0,
                )
                break
            except (APIConnectionError, APITimeoutError, RateLimitError):
                if attempt == 1:
                    raise
                logger.warning("Transient extraction provider failure; retrying once")
                time.sleep(0.5)

        content = response.choices[0].message.content
        if not content:
            return ExtractionResponse(success=False, error="Empty response from AI")

        extracted = ExtractedVendorData.model_validate(
            json.loads(_strip_markdown_fences(content))
        )

        try:
            settings_model = OrganizationSettings.model_validate(
                organization_settings or {}
            )
        except ValueError:
            logger.exception("Invalid organization settings; skipping overrides")
            settings_model = OrganizationSettings()

        warnings = _normalise_line_types(extracted)
        if _is_known_buyer(
            extracted.vendor_name, organization_name, settings_model.buyer_aliases
        ):
            try:
                recovered_vendor, recovered_department = _recover_vendor_from_document(
                    client,
                    pdf_text,
                    organization_name,
                    settings_model.buyer_aliases,
                )
            except Exception:  # noqa: BLE001 - recovery must not fail extraction
                logger.exception("Targeted vendor recovery failed")
                recovered_vendor, recovered_department = None, None
            if recovered_vendor:
                logger.info("Recovered vendor with targeted extraction pass")
                extracted.vendor_name = recovered_vendor
                if recovered_department:
                    extracted.department = recovered_department
            else:
                warnings.append(
                    "Extracted vendor matches the purchasing organization; verify the seller/vendor field."
                )
        warnings.extend(_reconcile_totals(extracted))

        original_group_id = extracted.commodity_group_id
        try:
            override = apply_commodity_override(
                extracted, settings_model.commodity_group_overrides
            )
        except Exception:  # noqa: BLE001 - configuration must not break extraction
            logger.exception("Commodity override evaluation failed; preserving AI classification")
            override = OverrideMatch()
            warnings.append("Commodity override rules could not be evaluated; verify the group manually.")
        if override.commodity_group_id is not None or override.conflict:
            logger.info(
                "Evaluated organization commodity override",
                extra={
                    "organization_id": str(organization_id) if organization_id else None,
                    "original_group_id": original_group_id,
                    "applied_group_id": override.commodity_group_id,
                    "conflict": override.conflict,
                },
            )
        if override.conflict:
            warnings.append(
                "Multiple commodity-group rules matched; resolve the commodity group before submitting."
            )
    except Exception:
        logger.exception("Error extracting vendor data")
        return ExtractionResponse(
            success=False,
            error="Unable to extract the quote. Please try again or review the PDF manually.",
        )

    # Check which fields are missing or empty (same rules as the original).
    missing_fields: list[str] = []
    if not (extracted.title or "").strip():
        missing_fields.append("Title/Short Description")
    if not (extracted.vendor_name or "").strip():
        missing_fields.append("Vendor Name")
    if not (extracted.vat_id or "").strip():
        missing_fields.append("Tax ID")
    if not (extracted.department or "").strip():
        missing_fields.append("Department")
    if not extracted.order_lines:
        missing_fields.append("Order Lines")
    if not extracted.total_cost:
        missing_fields.append("Total Cost")
    # Validate that the returned commodity group id actually exists.
    if extracted.commodity_group_id is not None:
        valid_ids = {g.id for g in commodity_groups}
        if extracted.commodity_group_id not in valid_ids:
            logger.warning(
                "Invalid commodity group ID returned: %s", extracted.commodity_group_id
            )
            extracted.commodity_group_id = None
            extracted.commodity_group_name = None
        else:
            # Keep the display name consistent with the canonical database row,
            # especially when a deterministic override replaced the AI value.
            group_by_id = {g.id: g for g in commodity_groups}
            extracted.commodity_group_name = group_by_id[
                extracted.commodity_group_id
            ].name

    if not extracted.commodity_group_id or not extracted.commodity_group_name:
        missing_fields.append("Commodity Group")

    return ExtractionResponse(
        success=True,
        data=extracted,
        missing_fields=missing_fields or None,
        override_applied=override.commodity_group_id is not None,
        override_reason=override.reason,
        override_conflict=override.conflict,
        override_group_ids=list(override.group_ids),
        warnings=warnings or None,
    )
