from __future__ import annotations
from enum import Enum
from pydantic import BaseModel


class EsiAccessType(str, Enum):
    unspecified = "Unspecified"
    allow = "allow"
    blocked = "blocked"


class EsiCharacterEntry(BaseModel):
    character_id: int
    access: EsiAccessType = EsiAccessType.unspecified


class EsiCorporationEntry(BaseModel):
    corporation_id: int
    access: EsiAccessType = EsiAccessType.unspecified


class EsiAllianceEntry(BaseModel):
    alliance_id: int
    access: EsiAccessType = EsiAccessType.unspecified


class EsiMembership(BaseModel):
    allow_everyone: bool = False
    characters: list[EsiCharacterEntry] = []
    corporations: list[EsiCorporationEntry] = []
    alliances: list[EsiAllianceEntry] = []


class AclEntryType(str, Enum):
    character = "character"
    corporation = "corporation"
    alliance = "alliance"


class AclEntryDTO(BaseModel):
    eve_id: int
    entry_type: AclEntryType
    access: EsiAccessType


class AccessListDTO(BaseModel):
    id: int
    name: str
    allow_everyone: bool
    entries: list[AclEntryDTO]

    @classmethod
    def from_esi_response(cls, data: dict) -> AccessListDTO:
        membership = EsiMembership(**data.get("membership", {}))
        entries: list[AclEntryDTO] = []
        for c in membership.characters:
            entries.append(AclEntryDTO(eve_id=c.character_id, entry_type=AclEntryType.character, access=c.access))
        for corp in membership.corporations:
            entries.append(AclEntryDTO(eve_id=corp.corporation_id, entry_type=AclEntryType.corporation, access=corp.access))
        for a in membership.alliances:
            entries.append(AclEntryDTO(eve_id=a.alliance_id, entry_type=AclEntryType.alliance, access=a.access))
        return cls(id=data["id"], name=data["name"], allow_everyone=membership.allow_everyone, entries=entries)


class WandererMemberDTO(BaseModel):
    eve_id: int
    entry_type: AclEntryType
    role: str

    @classmethod
    def from_wanderer_response(cls, data: dict) -> WandererMemberDTO:
        if "eve_character_id" in data:
            return cls(eve_id=int(data["eve_character_id"]), entry_type=AclEntryType.character, role=data["role"])
        if "eve_corporation_id" in data:
            return cls(eve_id=int(data["eve_corporation_id"]), entry_type=AclEntryType.corporation, role=data["role"])
        if "eve_alliance_id" in data:
            return cls(eve_id=int(data["eve_alliance_id"]), entry_type=AclEntryType.alliance, role=data["role"])
        raise ValueError(f"Cannot determine entity type from member data: {data}")


class WandererAclDTO(BaseModel):
    id: str
    members: list[WandererMemberDTO]
