"""On-disk formats, also used to validate model responses."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Asset(Model):
    path: str
    sha256: str
    media_type: str = "image/png"
    label: str


class SourceUnit(Model):
    id: str
    location: str
    text: str
    images: list[Asset] = Field(default_factory=list)
    heading: str = ""
    heading_level: int = 0


class TocEntry(Model):
    title: str = Field(min_length=1)
    level: int = Field(ge=1)
    target: str
    source_id: str | None = None
    selected: bool = True
    reason: str = ""


class SourceToc(Model):
    kind: Literal["none", "pdf_outline", "epub_nav", "epub_ncx"] = "none"
    source_title: str = ""
    entries: list[TocEntry] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Book(Model):
    schema_version: int = 1
    source_sha256: str
    title: str
    author: str = "Unknown author"
    format: Literal["pdf", "epub"]
    units: list[SourceUnit]
    cover: Asset | None = None
    warnings: list[str] = Field(default_factory=list)
    toc: SourceToc = Field(default_factory=SourceToc)


class Segment(Model):
    kind: Literal["heading", "paragraph", "figure", "table", "equation", "code", "footnote"]
    text: str = Field(min_length=1, description="Only the words to be spoken.")
    source_ids: list[str] = Field(min_length=1)
    display_title: str = ""
    heading_level: int = Field(default=0, ge=0, le=6)
    continues_previous: bool = False

    @model_validator(mode="after")
    def valid_heading(self):
        if self.kind == "heading" and (not self.display_title or self.heading_level == 0):
            raise ValueError("Headings need an original display_title and a heading_level of 1–6")
        if self.kind != "heading" and (self.display_title or self.heading_level):
            raise ValueError("Only headings may have display_title or heading_level")
        if self.continues_previous and self.kind != "paragraph":
            raise ValueError("Only paragraphs may continue a preceding paragraph")
        return self


class Coverage(Model):
    source_id: str
    disposition: Literal["narrated", "omitted"]
    reason: str = ""


class Draft(Model):
    segments: list[Segment]
    coverage: list[Coverage]
    uncertainties: list[str] = Field(default_factory=list)


class Finding(Model):
    severity: Literal["error", "warning"]
    source_ids: list[str]
    description: str = Field(min_length=1)


class Review(Model):
    approved: bool
    findings: list[Finding] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_verdict(self):
        if self.approved and any(f.severity == "error" for f in self.findings):
            raise ValueError("A review with errors cannot be approved")
        if not self.approved and not self.findings:
            raise ValueError("A rejected review must explain what needs repair")
        return self


class Transcript(Model):
    schema_version: int = 1
    title: str
    author: str
    source_sha256: str
    segments: list[Segment] = Field(min_length=1)
    coverage: list[Coverage]
    warnings: list[str] = Field(default_factory=list)
    cover: Asset | None = None
