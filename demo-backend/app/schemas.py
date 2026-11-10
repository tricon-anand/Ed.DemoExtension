from typing import Literal

from pydantic import BaseModel, Field


class RegisterIn(BaseModel):
    org_name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=128)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: int
    org_id: int
    email: str
    role: str

    model_config = {"from_attributes": True}


class ScenarioIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    target_host: str = ""


class ScenarioOut(BaseModel):
    id: int
    name: str
    target_host: str
    version: int

    model_config = {"from_attributes": True}


class RuleIn(BaseModel):
    selector: str = Field(min_length=1)
    type: Literal["text", "hide", "image", "html", "api"] = "text"
    value: str = ""
    host: str = Field(default="", max_length=255)    # "" = the scenario's target host; may be a list or *.example.com
    page: str = Field(default="", max_length=255)    # "" = every page; or a path pattern such as /patients* or /reports/*
    disabled: bool = False
    position: int | None = None      # None = append at the end


class RuleOut(BaseModel):
    id: int
    selector: str
    type: str
    value: str
    host: str = ""
    page: str = ""
    disabled: bool
    position: int

    model_config = {"from_attributes": True}


class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    variables: dict[str, str] = Field(default_factory=dict)


class ProfileOut(BaseModel):
    id: int
    name: str
    variables: dict[str, str]

    model_config = {"from_attributes": True}
