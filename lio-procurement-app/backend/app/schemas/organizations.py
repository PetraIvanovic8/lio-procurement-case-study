import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

MemberRole = Literal["admin", "buyer", "requester"]

# Request fields an organization may optionally mark as required. (The other
# request fields are always required by the app.)
ConfigurableRequiredField = Literal["vat_id", "department"]


class OrganizationSettings(BaseModel):
    """Per-organization configuration. Stored as JSONB, validated here.

    This is the reference pattern for tenant config: keep the storage flexible,
    but validate the shape at the edge so it can't drift into a free-for-all.
    """

    model_config = ConfigDict(extra="forbid")

    required_fields: list[ConfigurableRequiredField] = Field(default_factory=list)
    buyer_aliases: list[str] = Field(default_factory=list)
    commodity_group_overrides: list["CommodityGroupOverride"] = Field(
        default_factory=list
    )

class CommodityGroupOverride(BaseModel):
    """A tenant rule that maps procurement text to a commodity group."""

    required_keywords: list[str] = Field(default_factory=list)
    alternative_keywords: list[str] = Field(default_factory=list)
    excluded_keywords: list[str] = Field(default_factory=list)
    commodity_group_id: int = Field(gt=0)
    max_total_cost: float | None = Field(default=None, ge=0)

    @field_validator("required_keywords", "alternative_keywords", "excluded_keywords")
    @classmethod
    def normalize_keywords(cls, values: list[str]) -> list[str]:
        """Trim configured keywords and reject blank entries."""
        normalized = [value.strip().casefold() for value in values]
        if any(not value for value in normalized):
            raise ValueError("Override keywords cannot be empty")
        return normalized

    @model_validator(mode="after")
    def require_match_keywords(self) -> "CommodityGroupOverride":
        """Prevent an override that would match every procurement request."""
        if not self.required_keywords and not self.alternative_keywords:
            raise ValueError("At least one override keyword is required")
        return self


class OrganizationCreate(BaseModel):
    name: str
    slug: str


class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    created_at: datetime
    updated_at: datetime


class MyOrganizationOut(OrganizationOut):
    role: MemberRole


class MemberProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str | None = None
    avatar_url: str | None = None


class MemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    user_id: uuid.UUID
    role: MemberRole
    created_at: datetime
    # Named to match the Supabase join key the frontend already renders.
    profiles: MemberProfileOut | None = None


class InviteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    email: str
    role: str
    invited_by: uuid.UUID
    expires_at: datetime
    accepted_at: datetime | None
    created_at: datetime


class MembersResponse(BaseModel):
    members: list[MemberOut]
    invites: list[InviteOut]


class InviteCreate(BaseModel):
    email: EmailStr
    role: MemberRole = "requester"


class InviteAccept(BaseModel):
    token: str
